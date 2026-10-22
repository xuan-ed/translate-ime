# -*- coding: utf-8 -*-
"""前台窗口卡住时，常驻实例**不能**跟着卡在启动路上（第 19 轮的真 bug）。

用户看到的现象是「双击了没反应」：实例起来了、内存也吃上了，可状态文件永远
不出现、日志停在 ``[overlay] 已对 2 层窗口加 NOACTIVATE``，因为
``SetForegroundWindow`` / ``BringWindowToTop`` 遇到一个**不理消息的前台窗口**
会一直等下去。

蹲坑换来的三条事实（都在真机上量过，写在这里省得下次再摸）：

* ``IsHungAppWindow`` 要等系统那 **~5 秒**超时才会翻成 True（+0s/+4s 都是
  False、+8s 起才是 True）—— 卡死的那 5 秒里它救不了我们。
* 但 ``SendMessageTimeoutW(HWND, WM_NULL, ..., SMTO_ABORTIFHUNG|SMTO_BLOCK, 300)``
  **立刻**就能问出来：不理人的窗口耗满 300ms 返回 0，之后系统已标记、0ms 返回。
* 「谁不理人」经常是**要还回去的那个窗口**（``_real_fg``，用户卡住的程序），
  不是 ``cur`` —— Tk 建悬浮窗时会把前台从那个卡住的窗口抢过来，所以 ``cur``
  是我们自己的窗口、理人得很；只查 ``cur`` 会把日志写成「还原失败」，怪错人。

这个测试不碰真桌面，除了第 3、4 节要**故意卡住**的子进程窗口（起两个：一个不抢
前台、一个抢到前台，分别覆盖 ``set_foreground`` 里 target 与「当前前台」两条
守卫；收尾用 ``taskkill /T /F``，因为 ``.venv\\Scripts\\pythonw.exe`` 只是个启动器，
真解释器是它的子进程，只 terminate 启动器会留下还开着窗口的孤儿）：

* 第 1 节：三个 API 的形状与「问不出来就当不能用」的保守默认值
* 第 2 节：自己的窗口当然理人（别把守卫做成「什么都不碰」）
* 第 3 节：真·卡住的窗口必须被认出来，而且要在 300ms 级别上认出来，
  顺带当场记下 ``IsHungAppWindow`` 那 ~5 秒滞后
* 第 4 节：``set_foreground`` 面对卡住的前台窗口**必须快速返回 False**，
  不许在 ``BringWindowToTop`` 里吊死（这条就是那个真 bug 的回归测试）
* 第 5 节：``core.on_foreground_changed()`` 的语义（切窗口要清拼音缓冲）
* 第 6 节：``App._poll_foreground`` 的接线（命中就收候选窗；候选窗自己不
  算「切窗口」，不然刚弹出来的候选窗会把自己关掉）
* 第 7 节：源码形状 —— 守卫必须排在 ``AttachThreadInput`` 前面

直接跑：``python tests/test_foreground_guard.py``
"""
import os
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime import daemon  # noqa: E402
from t_ime import winapi as W  # noqa: E402
from t_ime.app import App  # noqa: E402
from t_ime.config import load as load_config  # noqa: E402
from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

OK = "[OK]  "
BAD = "[FAIL]"
SKIP = "[skip]"
results: list[tuple[bool, str]] = []
skipped = 0


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


def skip(label, detail=""):
    """环境不给力（比如桌面锁着抢不到前台）—— 算过，但要看得见。"""
    global skipped
    skipped += 1
    results.append((True, label))
    print(f"{SKIP} {label}" + (f"  — {detail}" if detail else ""))
    return True


print("=" * 74)
cfg, cfg_path = load_config()
print(f"配置来自 {cfg_path}")
print("=" * 74)

status_path = daemon.status_path(PROJ)
ctl_path = daemon.ctl_path(PROJ)
backup: dict[Path, bytes] = {}
for p in (status_path, ctl_path):
    if p.exists():
        backup[p] = p.read_bytes()

children: list[subprocess.Popen] = []
app = None
try:
    app = App(cfg, PROJ, verbose=False, install_hook=False)
    root = app.overlay.root
    root.update()

    # ---- 1. 三个 API 的形状 ----
    for name in ("is_hung", "responds_within", "is_responsive", "set_foreground"):
        check(callable(getattr(W, name, None)), f"winapi.{name}() 存在")
    check(getattr(W, "WM_NULL", None) == 0x0000, "常量 WM_NULL 是 0x0000")
    check(getattr(W, "SMTO_BLOCK", None) == 0x0001, "常量 SMTO_BLOCK 是 0x0001")
    check(getattr(W, "SMTO_ABORTIFHUNG", None) == 0x0002, "常量 SMTO_ABORTIFHUNG 是 0x0002")
    check(bool(getattr(W.user32.SendMessageTimeoutW, "argtypes", None)),
          "SendMessageTimeoutW 声明了 argtypes（64 位下不声明会把句柄截断）")

    t0 = time.monotonic()
    bad = (W.is_hung(0), W.responds_within(0), W.is_responsive(0))
    dt = time.monotonic() - t0
    check(bad == (False, False, False),
          "问不到 / 句柄为 0 时一律答「不能用」（保守默认值是 False，不是 True）",
          f"is_hung/responds_within/is_responsive = {bad}")
    check(dt < 0.5, "句柄为 0 时不浪费时间（0.5 秒内答完）", f"{dt * 1000:.0f}ms")

    # ---- 2. 自己的窗口当然理人 ----
    hwnd_self = int(root.winfo_id())
    t0 = time.monotonic()
    mine = (W.responds_within(hwnd_self), W.is_responsive(hwnd_self), W.is_hung(hwnd_self))
    dt = time.monotonic() - t0
    check(mine == (True, True, False),
          "自己的窗口被认成「理人」（别把守卫做成谁都不碰）", f"{mine} 用了 {dt * 1000:.0f}ms")
    check(dt < 0.5, "问自己的窗口不用等超时", f"{dt * 1000:.0f}ms")

    # ---- 3 & 4. 真·卡住的窗口：认得出，而且不许把 set_foreground 吊死 ----
    # 用两个子进程，把 set_foreground 的两道守卫分开测：
    #   ① `plain`：建窗口、**不抢前台**，然后停止泵消息 → 测 target 那条分支；
    #   ② `steal`：抢到前台之后才停止泵消息 → 测「当前前台窗口不理人」那条分支。
    # 合成一个进程是测不通的：卡住的窗口正好就是前台时，set_foreground 会先走
    # 「已经在前台」的早退（返回 True），target 那条分支根本轮不到 —— 第一版
    # 就是这么误报了一条 FAIL，跟产品无关。
    def frozen_child(steal: bool) -> tuple[subprocess.Popen | None, int, bool]:
        script = f'''
import sys, time
sys.path.insert(0, r"{PROJ}")
import tkinter as tk
from t_ime import winapi as W
root = tk.Tk()
root.title("r19 frozen window")
root.geometry("220x90+90+90")


def pump(sec):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        root.update()
        time.sleep(0.01)


pump(0.25)
hwnd = int(root.winfo_id())
owned = False
if {int(steal)}:
    for _ in range(3):
        W.set_foreground(hwnd)
        pump(0.2)
        if W.same_window(W.foreground_hwnd(), hwnd):
            owned = True
            break
pump(0.2)
print(f"{{hwnd}} {{int(owned)}}", flush=True)
# 从这里开始**不再泵消息** —— 这就是那个「不理人的窗口」
time.sleep(120)
'''
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                text=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        first = proc.stdout.readline().strip() if proc.stdout else ""
        if not first:
            return None, 0, False
        return proc, int(first.split()[0]), first.split()[1] == "1"

    plain, plain_hwnd, _ = frozen_child(steal=False)
    if not plain_hwnd:
        skip("[3] 卡住窗口的隔离实验", "子进程没报出窗口句柄")
    else:
        children.append(plain)
        time.sleep(0.6)  # 让它彻底睡着
        hung0 = W.is_hung(plain_hwnd)
        t0 = time.monotonic()
        early = W.responds_within(plain_hwnd, 300)
        dt = time.monotonic() - t0
        check(early is False and dt < 1.5,
              "[3] 不泵消息的窗口：SendMessageTimeout 当场（不等系统）就答「不理人」",
              f"responds_within={early} 用了 {dt * 1000:.0f}ms")
        check(hung0 is False,
              "[3b] 可它刚睡着时 IsHungAppWindow 还答 False —— 系统要 ~5 秒才标记，"
              "所以只靠它就会在那 5 秒里吊死", f"is_hung={hung0}")

        t0 = time.monotonic()
        flipped = False
        while time.monotonic() - t0 < 12.0:
            if W.is_hung(plain_hwnd):
                flipped = True
                break
            time.sleep(0.3)
        if flipped:
            check(True, "[3c] 上面那次敲门超时之后，系统才把 IsHungAppWindow 标成 True",
                  f"共等了 {time.monotonic() - t0:.1f} 秒")
        else:
            skip("[3c] IsHungAppWindow 最终会翻成 True", "12 秒内没标记，这台机器的策略不同")

        # 刚建出来的窗口常常自己就成了前台（「前台进程启动的进程」有资格设前台），
        # 那时 set_foreground 会先走「已经在前台」的早退、target 那条分支测不到，
        # 所以先把自己的窗口推上去，把它挤下来。
        for _ in range(3):
            if not W.same_window(W.foreground_hwnd(), plain_hwnd):
                break
            W.set_foreground(hwnd_self)
            time.sleep(0.2)
        if W.same_window(W.foreground_hwnd(), plain_hwnd):
            skip("[4] set_foreground 拒绝为不响应的窗口做事（target 分支）",
                 "它始终占着前台，会先走「已经在前台」的早退，这条测不到")
        else:
            t0 = time.monotonic()
            moved = W.set_foreground(plain_hwnd)
            dt = time.monotonic() - t0
            check(moved is False,
                  "[4] set_foreground 拒绝为不响应的窗口做事（target 分支）",
                  f"返回 {moved!r} 用了 {dt * 1000:.0f}ms")
            check(dt < 1.5,
                  "[4b] 而且**快速**返回（这不是走系统超时那条路）", f"{dt * 1000:.0f}ms")
            check(not W.same_window(W.foreground_hwnd(), plain_hwnd),
                  "[4c] 没有偷偷把前台塞给它（失败就老实失败）")

    # ---- 4d/4e. 卡住的窗口**正占着前台**时（这才是那个真 bug 的形状）----
    steal_proc, steal_hwnd, steal_owned = frozen_child(steal=True)
    if not steal_hwnd:
        skip("[4d] 卡住的前台窗口：set_foreground 也不吊死", "子进程没报出窗口句柄")
        skip("[4e] 前台没被我们抢过来", "同上")
    else:
        children.append(steal_proc)
        time.sleep(0.6)
        if not W.same_window(W.foreground_hwnd(), steal_hwnd):
            skip("[4d] 卡住的前台窗口：set_foreground 也不吊死",
                 f"这次它没能占住前台（桌面正被别人的窗口占着）；真机形状由 .tmp 的探针覆盖"
                 f"（子进程自称抢到过：{steal_owned}）")
            skip("[4e] 前台没被我们抢过来", "同上")
        else:
            t0 = time.monotonic()
            moved2 = W.set_foreground(hwnd_self)
            dt = time.monotonic() - t0
            check(moved2 is False and dt < 2.0,
                  "[4d] 卡住的前台窗口：set_foreground 拒绝、也不吊死"
                  "（旧代码在这里挂住 25 秒，用户看到的就是「双击没反应」）",
                  f"返回 {moved2!r} 用了 {dt * 1000:.0f}ms")
            check(not W.same_window(W.foreground_hwnd(), hwnd_self),
                  "[4e] 前台没被我们抢过来（失败就老实失败）")

    # ---- 5. core.on_foreground_changed() 的语义 ----
    core = app.core
    core._reset()
    check(core.on_foreground_changed() is False,
          "缓冲是空的时候切窗口：不报告「需要清候选窗」（省掉一次无意义的 hide）")
    for ch in "nihao":
        core.handle_key(0x41 + (ord(ch) - ord("a")), True, False)
    check(core.buffer == "nihao", "先把半截拼音敲进缓冲", f"buffer={core.buffer!r}")
    active0 = core.active
    check(core.on_foreground_changed() is True, "带着半截拼音切窗口：报告需要清候选窗")
    check(core.buffer == "", "缓冲被清空（半截拼音不许落到别的窗口去）", f"buffer={core.buffer!r}")
    check(core.active == active0, "中文/英文模式**不变**（切窗口不该替你换模式）",
          f"active={core.active!r} 原为 {active0!r}")
    core.handle_key(0x4E, True, False)  # 按着 n 不放
    had = bool(core._swallow_up)
    core.on_foreground_changed()
    check(had and not core._swallow_up,
          "抬手续吞的记账也清掉（否则新窗口里松开这个键会被误吞）",
          f"原是 {had}，现在是 {bool(core._swallow_up)}")

    # ---- 6. App._poll_foreground 的接线 ----
    real_fg, real_pid = W.foreground_hwnd, W.window_pid
    hides: list[int] = []
    real_hide = app.overlay.hide

    def fake_hide(*a, **k):
        hides.append(1)

    class FakeCore:
        def __init__(self):
            self.calls = 0
            self.answer = True

        def on_foreground_changed(self):
            self.calls += 1
            return self.answer

    try:
        app.overlay.hide = fake_hide
        app.core = FakeCore()
        app._fg_hwnd = 100

        W.foreground_hwnd = lambda: 100
        app._poll_foreground()
        check(not hides and app.core.calls == 0,
              "前台没变：连问都不问 core（每次 tick 都问等于每 30ms 清一次缓冲）")

        W.foreground_hwnd = lambda: 200
        W.window_pid = lambda h: os.getpid()
        app._poll_foreground()
        check(not hides and app._fg_hwnd == 100,
              "前台是我们自己的窗口（候选窗）：不算切窗口，也不更新记账")
        check(app.core.calls == 0, "同上：core 根本没被叫")

        W.foreground_hwnd = lambda: 0
        app._poll_foreground()
        check(not hides and app.core.calls == 0, "前台句柄读不到（0）：什么都不做")

        W.foreground_hwnd = lambda: 300
        W.window_pid = lambda h: 4242
        app._poll_foreground()
        check(hides == [1] and app._fg_hwnd == 300,
              "真的换了窗口、且 core 说要清：候选窗收起来、前台记账更新",
              f"hides={len(hides)} _fg_hwnd={app._fg_hwnd}")

        app._poll_foreground()
        check(hides == [1], "同一个窗口不重复收（不然每次都 hide 一遍）")

        app.core.answer = False
        W.foreground_hwnd = lambda: 400
        app._poll_foreground()
        check(hides == [1] and app._fg_hwnd == 400,
              "换窗口但缓冲本来就是空的：不收候选窗，但记账照样更新")
    finally:
        W.foreground_hwnd, W.window_pid = real_fg, real_pid
        app.overlay.hide = real_hide

    # ---- 7. 源码形状：守卫必须排在 attach 前面 ----
    src = (PROJ / "t_ime" / "winapi.py").read_text(encoding="utf-8").splitlines()
    i0 = next(i for i, ln in enumerate(src) if ln.startswith("def set_foreground"))
    body = src[i0:i0 + 70]
    i_resp = next((i for i, ln in enumerate(body) if "is_responsive(" in ln), None)
    i_attach = next((i for i, ln in enumerate(body) if "AttachThreadInput(" in ln), None)
    check(i_resp is not None and i_attach is not None and i_resp < i_attach,
          "set_foreground 里先问「理不理人」，之后才 AttachThreadInput/BringWindowToTop",
          f"is_responsive 在第 {i_resp} 行、AttachThreadInput 在第 {i_attach} 行（相对方法体）")

    ov = (PROJ / "t_ime" / "overlay.py").read_text(encoding="utf-8")
    check("跳过还原" in ov and "is_responsive(" in ov,
          "overlay 的还原失败分支认得「谁不理人」，并且文案是「跳过还原 —— 不碰卡住的窗口」")
    check("要还回去的窗口" in ov,
          "而且会先怀疑**要还回去的**那个窗口（Tk 抢过前台，cur 往往是我们自己）")
finally:
    if app is not None:
        try:
            app.shutdown()
        except Exception:
            pass
    for proc in children:
        if proc.poll() is None:
            # .venv\Scripts\python.exe 是个启动器，杀父不杀子会留下孤儿窗口
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
    for p, data in backup.items():
        p.write_bytes(data)
    for p in (status_path, ctl_path):
        if p not in backup:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

print()
print("=" * 74)
passed = sum(1 for ok, _ in results if ok)
tail = f"（其中 {skipped} 项因环境跳过）" if skipped else ""
print(f"结果：{passed}/{len(results)} 通过{tail}")
for ok, label in results:
    if not ok:
        print(f"  未通过：{label}")
print("=" * 74)
sys.exit(0 if passed == len(results) else 1)
