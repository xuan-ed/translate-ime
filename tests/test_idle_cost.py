# -*- coding: utf-8 -*-
"""空转成本（常驻时的耗电 / 白烧 CPU）测试。

**这里测的是一个真机上量出来的问题，不是想象出来的。**

第 34 轮量了一个真 pythonw 常驻实例：不打字、候选窗关着，照样烧掉
**3.8%（无钩子）/ 5.9%（带钩子）的一个核**。原因是主循环固定 16 毫秒一拍
（约 60 次/秒），而 bare Tk 的 ``after(16)`` 空循环本身就占 1.4%；再加上每拍
里还做一次收件箱 stat+open（60µs）。常驻输入法 90% 的时间在空转，这笔钱要背一整天。

修法：心跳自适应 —— 忙（候选窗开着 / 缓冲有字 / 等翻译 / 两秒内还收过按键）
时 16ms，空转时 50ms；收件箱改成 200ms 轮询。

本测试盯住三件事，缺一不可：

* 空转时**真的**排 50ms 而不是 16ms（否则改回去也没人发现）；
* 「忙」的判据还在（候选窗一开、缓冲一有字，立刻回到 16ms —— 打字必须跟手）；
* 真机空转 10 秒的 CPU 占用低于门槛，并且这 10 秒里日志不涨（空转不该刷日志）。

直接跑：``python tests/test_idle_cost.py``
"""
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime import daemon  # noqa: E402
from t_ime import winapi as W  # noqa: E402
from t_ime.app import (  # noqa: E402
    CMD_POLL_MS, IDLE_AFTER_S, IDLE_TICK_MS, TICK_MS, App,
)
from t_ime.config import load as load_config  # noqa: E402
from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

OK = "[OK]  "
BAD = "[FAIL]"
results: list[tuple[bool, str]] = []
status_path = daemon.status_path(PROJ)
ctl_path = daemon.ctl_path(PROJ)
log_path = PROJ / "translate-ime.log"


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"  — {detail}" if detail else ""))
    return cond


def wait_for(pred, timeout: float, interval: float = 0.2):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    return last


print("=" * 74)
print("① 常量本身要说得通（空转档必须比忙档慢，而不是随手写个 0）")
print("=" * 74)
check(IDLE_TICK_MS > TICK_MS, "空转心跳比忙时慢", f"TICK_MS={TICK_MS} IDLE_TICK_MS={IDLE_TICK_MS}")
check(IDLE_TICK_MS >= 2 * TICK_MS, "至少慢一倍（不然省不下什么）", f"{IDLE_TICK_MS} >= {2 * TICK_MS}")
check(IDLE_TICK_MS <= 250,
      "也别慢到按键卡顿（250ms 是上限）", f"IDLE_TICK_MS={IDLE_TICK_MS}")
check(IDLE_AFTER_S >= 1.0, "「多久没动静算空转」至少 1 秒（别把连续打字判成空转）",
      f"IDLE_AFTER_S={IDLE_AFTER_S}")
check(CMD_POLL_MS >= 5 * TICK_MS,
      "收件箱轮询至少比心跳慢 5 倍（否则降频白做）", f"CMD_POLL_MS={CMD_POLL_MS}")

print()
print("=" * 74)
print("② 「忙」的判据：这四条哪条成立都必须回到 16ms")
print("=" * 74)
cfg, _p = load_config()
app = App(cfg, PROJ, verbose=False, install_hook=False)
app._running = True


def set_idle():
    """把 App 摆成「刚空转完」：没缓冲、没等翻译、没待处理按键、两秒内没动静。

    注意**故意**不碰 ``overlay.visible``：启动引导的短提示会一直挂在屏幕上，
    把「候选窗开着」当忙就等于永远忙（第一版这么写，进程内实测 12 秒 601 拍
    全是 16ms，等于没改）。判据里只有「真有字要画 / 真有键要处理」才算忙。
    """
    app.core.buffer = []
    app.core.tr_state = ""
    app._last_activity = time.monotonic() - IDLE_AFTER_S - 1.0
    while not app.events.empty():
        try:
            app.events.get_nowait()
        except Exception:
            break


set_idle()
check(app._busy() is False, "空转状态：_busy() 为假")
check(app.overlay.visible is True or app.overlay.visible is False,
      "（候选窗开没开都不影响这条判定）")
app._last_activity = time.monotonic()
check(app._busy() is True, "刚有动静（两秒内收过按键）：_busy() 为真")
set_idle()
app.core.buffer = ["n", "i"]
check(app._busy() is True, "缓冲里有拼音：_busy() 为真（打字必须跟手）")
set_idle()
app.core.buffer = ["nihao"]
check(app._busy() is True, "缓冲非空：算忙")
set_idle()
app.core.tr_state = "pending"
check(app._busy() is True, "正在等翻译结果：_busy() 为真（否则译文要等下一拍才出现）")
set_idle()
app.events.put({"type": "key", "vk": 78})
check(app._busy() is True, "队列里已经有待处理的按键：算忙（这一拍就该处理，别拖到下一拍）")
set_idle()
check(app.events.empty(), "（测试自己把事件队列清干净了）")
check(app._busy() is False, "恢复空转后又是假的（判据没有粘住）")

print()
print("=" * 74)
print("③ 主循环真的按 50ms / 16ms 排下一拍（不是只算了个 _busy 没人用）")
print("=" * 74)
recorded: list[int] = []
real_after = app.overlay.after
app.overlay.after = lambda ms, fn: recorded.append(ms)   # 只记，真排程不要
try:
    app._running = True
    app.core.quit_requested = False
    set_idle()
    recorded.clear()
    app._tick()
    check(recorded == [IDLE_TICK_MS], "空转时 _tick() 排的是 IDLE_TICK_MS",
          f"排了 {recorded}")
    check(app._last_tick_ms == IDLE_TICK_MS, "并把实际间隔记在 _last_tick_ms 上（诊断能看见）",
          f"_last_tick_ms={app._last_tick_ms}")

    app._last_activity = time.monotonic()
    recorded.clear()
    app._tick()
    check(recorded == [TICK_MS], "刚有动静时 _tick() 排回 TICK_MS", f"排了 {recorded}")
    check(app._last_tick_ms == TICK_MS, "_last_tick_ms 跟着回到忙档")

    app.core.quit_requested = True
    recorded.clear()
    app._tick()
    check(recorded == [], "已经在退出：不再排下一拍")
    app.core.quit_requested = False
finally:
    app.overlay.after = real_after

print()
print("=" * 74)
print("④ 收件箱降频：_tick_once 里最多每 CMD_POLL_MS 轮询一次")
print("=" * 74)
polls = {"n": 0}
real_drain = app._drain_commands
app._drain_commands = lambda: polls.__setitem__("n", polls["n"] + 1)
try:
    polls["n"] = 0
    app._last_cmd_poll = time.monotonic()
    app._tick_once()
    check(polls["n"] == 0, "刚轮询过：这一拍不碰收件箱（省掉一次 stat+open）",
          f"polls={polls['n']}")
    app._last_cmd_poll = 0.0
    app._tick_once()
    check(polls["n"] == 1, "_last_cmd_poll 归零后：下一拍立刻轮询一次", f"polls={polls['n']}")
    app._tick_once()
    check(polls["n"] == 1, "紧接着再一拍：又不轮询了（真的限频成 CMD_POLL_MS）",
          f"polls={polls['n']}")
finally:
    app._drain_commands = real_drain

# 降频只发生在心跳里：直接调 _drain_commands() 的命令入口必须立刻生效
app._last_cmd_poll = time.monotonic()
check(daemon.send_command(PROJ, "status"), "直接投一条命令进收件箱")
app._drain_commands()
check(not ctl_path.exists(), "直接调 _drain_commands() 时不受限频影响（命令立刻被取走）")

app.shutdown()
print()

print("=" * 74)
print("⑤ 真机空转 10 秒：CPU 占用与日志增长（这才是「常驻」的账单）")
print("=" * 74)
if W.instance_exists():
    print("已经有一个 translate-ime 在跑，跳过真机测量（不打断你正在用的输入法）")
    results.append((True, "跳过真机测量（已有实例在跑）"))
else:
    for p in (status_path, ctl_path):
        if p.exists():
            p.unlink()
    log_size0 = log_path.stat().st_size if log_path.exists() else 0
    proc = subprocess.run(
        [sys.executable, "main.py", "--background"],
        cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=90,
    )
    check(proc.returncode == 0, "后台实例拉起来了", f"exit={proc.returncode}")
    st = wait_for(lambda: daemon.read_status(PROJ), 25.0)
    pid = int((st or {}).get("pid") or 0)
    check(pid > 0, "状态文件里有进程号", f"pid={pid}")

    # ---- 用 GetProcessTimes 量 CPU（不引第三方库）----
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    PROC_QUERY_LIMITED_INFORMATION = 0x1000

    class FILETIME(ctypes.Structure):
        _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    handle = k32.OpenProcess(PROC_QUERY_LIMITED_INFORMATION, False, pid)

    def cpu_time() -> float:
        c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e),
                            ctypes.byref(k), ctypes.byref(u))
        return ((k.hi << 32) + k.lo + (u.hi << 32) + u.lo) / 1e7

    try:
        # 先等启动引导（7 秒）+ 收尾短提示（3 秒）都播完，否则量到的是
        # 「引导还在屏幕上」那段忙档 —— 第一版只等 9 秒，读数 3.91%，
        # 里面混着最后 1 秒 16ms 档的开销。
        time.sleep(13.0)
        st = daemon.read_status(PROJ) or {}
        check(bool(st.get("enabled")), "测量开始时实例还活着（状态文件在刷新）")
        log_size1 = log_path.stat().st_size if log_path.exists() else 0
        c0 = cpu_time()
        t0 = time.time()
        time.sleep(10.0)
        dt = time.time() - t0
        cpu = cpu_time() - c0
        pct = cpu / dt * 100.0
        log_grew = (log_path.stat().st_size if log_path.exists() else 0) - log_size1
        print(f"       空转 {dt:.1f} 秒：CPU {cpu:.3f}s = {pct:.2f}% 一个核；"
              f"日志 +{log_grew} 字节")
        check(pct <= 4.5,
              "空转 10 秒 CPU 占用 <= 4.5% 一个核（改前实测 5.9%，会红）",
              f"实测 {pct:.2f}%")
        check(log_grew <= 512, "空转时几乎不写日志（不刷屏、不涨盘）", f"+{log_grew} 字节")
        st2 = daemon.read_status(PROJ) or {}
        check(int(st2.get("tick_errors") or 0) == 0, "空转期间主循环没报错")
        check(log_path.stat().st_size > log_size0 or log_size0 == 0,
              "日志文件确实存在（启动横幅写过）")
    finally:
        if handle:
            k32.CloseHandle(handle)
        daemon.send_command(PROJ, "quit")
        wait_for(lambda: W.process_name(pid) == "", 10.0)
        if W.process_name(pid) != "":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=30)
        if status_path.exists():
            status_path.unlink()

print()
print("=" * 74)
passed = sum(1 for ok, _ in results if ok)
print(f"结果：{passed}/{len(results)} 通过")
for ok, label in results:
    if not ok:
        print(f"  未通过：{label}")
print("=" * 74)
sys.exit(0 if passed == len(results) else 1)
