# -*- coding: utf-8 -*-
"""状态文件要有心跳：闲着不动的实例，不能被体检说成「卡死或被强杀」。

背景（第 15 轮实测出来的真 bug）：``App._publish_status`` 只在**状态有变化**的
时候写盘（这是故意的：不然每秒都要写一次），而外面判断「那个常驻实例还活着吗」
看的是状态文件的 **mtime**（``daemon.status_is_stale``，--doctor 和第二次双击的
控制面板都用它）。于是：

    用户只要十来秒不打字 → 状态文件 mtime 冻住 → 体检报
    「[warn] 状态文件 超过 10 秒没更新，可能卡死或被强杀」

实测证据：起一个常驻实例后空闲 **45 秒里状态文件一次都没被改写**，而它活得好好的；
``--doctor --doctor-quick`` 与第二次双击的面板都在说谎。这不只是难看 —— 它让
「真的卡死」和「闲着没事干」变得无法区分（喊狼来了）。

修法：常驻实例给状态文件**心跳**（``daemon.STATUS_HEARTBEAT_SECONDS``），状态没变
也至少每 5 秒写一次，于是「两个心跳没来」（``STATUS_STALE_SECONDS``）才等于真的不动了。

这个文件验六件事：

1. 两个常量本身自洽（心跳 > 0、两个心跳才等于「旧」、``status_is_stale`` 的默认值
   就是 ``STATUS_STALE_SECONDS``），以及新加的 ``status_age`` 读数正确；
2. 状态**没变**时，心跳到点会写盘，没到点不写（不能变成每拍都写盘）；
3. 写盘失败别装没事：要留下「写不进去」的提醒、也别每拍重试，写成功后恢复；
4. ``--doctor`` 对活着的空闲实例说的是「N 秒前更新过（心跳正常）」，不再报卡死；
5. 第二次双击的控制面板在同样情况下也不再打印「可能已经卡死或被强杀了」；
6. **真机端到端**：起一个真常驻实例，什么都不干，看 mtime 是否自己在走
   （而且状态字段一个都没变）—— 这一条才是这个 bug 的直接反证。

第 4/5/6 节会碰真正的状态文件，所以有实例在跑时自动跳过（不打扰你正在用的输入法）。

直接跑：``python tests/test_status_heartbeat.py``
"""
import inspect
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
results: list[tuple[bool, str]] = []


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


def wait_for(pred, timeout: float, interval: float = 0.25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            got = pred()
        except Exception:
            got = None
        if got:
            return got
        time.sleep(interval)
    try:
        return pred()
    except Exception:
        return None


def mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns


def release_mutex(name: str = "translate-ime") -> bool:
    """放掉本进程占着的单实例锁。

    第 5 节要借这把锁让 ``main.py`` 走「已经在跑」那条路（面板分支），
    但第 6 节要真的起一个常驻实例 —— 锁还攥在自己手里的话，那个实例会
    在 ``already_running()`` 那里直接 `return 3`，根本起不来（我第一次跑
    就是这么被自己绊倒的）。
    """
    import ctypes

    handle = W._MUTEX_HANDLES.pop(name, None)      # noqa: SLF001 —— 测试里就是要放锁
    if not handle:
        return False
    return bool(W.kernel32.CloseHandle(ctypes.c_void_p(handle)))


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
daemon.take_commands(PROJ)

tmp_root = PROJ / ".tmp" / "hb-root"
tmp_root.mkdir(parents=True, exist_ok=True)
for p in (daemon.status_path(tmp_root), daemon.ctl_path(tmp_root)):
    if p.exists():
        p.unlink()

busy = W.instance_exists()          # 用户（或我）已经在跑一个实例
app = None
child_pid = 0
try:
    # ------------------------------------------------------------------ #
    # 1. 常量与 status_age
    # ------------------------------------------------------------------ #
    print("\n-- 1. 心跳常量与 status_age --")
    hb = daemon.STATUS_HEARTBEAT_SECONDS
    stale_s = daemon.STATUS_STALE_SECONDS
    check(hb > 0, "心跳间隔是正数", f"{hb}s")
    check(hb * 2 <= stale_s, "「旧」的阈值至少是两个心跳（一个心跳没来不算死）",
          f"心跳 {hb}s × 2 <= {stale_s}s")
    default_age = inspect.signature(daemon.status_is_stale).parameters["max_age"].default
    check(default_age == stale_s, "status_is_stale 的默认阈值就是 STATUS_STALE_SECONDS",
          f"默认 {default_age}")

    missing = tmp_root / "nope.status.json"
    check(daemon.status_age(tmp_root) is None, "状态文件不存在时 status_age 返回 None")
    check(daemon.status_is_stale(tmp_root) is True, "状态文件不存在算「旧」（老行为）")

    daemon.write_status(tmp_root, {"pid": os.getpid(), "updated": time.time()})
    age = daemon.status_age(tmp_root)
    check(age is not None and age < 2.0, "刚写的状态文件 age 接近 0",
          f"age={age:.3f}s" if age is not None else "age=None")
    check(daemon.status_is_stale(tmp_root) is False, "刚写的状态不算「旧」")

    old = time.time() - 60.0
    os.utime(daemon.status_path(tmp_root), (old, old))
    age = daemon.status_age(tmp_root)
    check(age is not None and 59.0 <= age <= 62.0, "把 mtime 拨到 60 秒前，age 读数跟得上",
          f"age={age:.1f}s" if age is not None else "age=None")
    check(daemon.status_is_stale(tmp_root) is True, "60 秒没更新才算「旧」")
    check(daemon.status_is_stale(tmp_root, max_age=120.0) is False,
          "阈值是参数说了算（max_age=120 时还不算旧）")
    check(missing.exists() is False, "那个不存在的路径只是拿来读，没有被创建出来")

    # ------------------------------------------------------------------ #
    # 2. 心跳：状态没变也会写盘，但不会变成每拍都写
    # ------------------------------------------------------------------ #
    print("\n-- 2. 状态没变时的写盘节奏 --")
    app = App(cfg, tmp_root, verbose=False, install_hook=False)
    hb_status = daemon.status_path(tmp_root)
    if hb_status.exists():
        hb_status.unlink()
    app._publish_status(force=True)
    check(hb_status.exists(), "force=True 一定会写盘")
    check(app._status_sig is not None, "写成功之后才记下「这版状态发布过了」")
    check(app._last_write > 0, "记下了这次写盘的时刻（心跳基准）")

    # 翻译层也要落到状态文件里：配了 key 却一直不翻译时，
    # 面板（第二次双击）与体检读的就是这里，而不是去追悬浮窗那行会消失的字。
    st_tr = (daemon.read_status(tmp_root) or {}).get("translate") or {}
    check(bool(st_tr), "状态文件里报了翻译层（谁在翻、翻了几次）", sorted(st_tr))
    check(st_tr.get("provider") in ("mock", "deepseek"),
          "翻译 provider 是个具体的名字", st_tr.get("provider"))
    check("mock" in st_tr and "errors" in st_tr and "last_error" in st_tr,
          "翻译块带着 mock / errors / last_error 三个关键字段", sorted(st_tr))
    check("mock_reason" in st_tr,
          "翻译块带着 mock_reason（离线时说得出「没配 key」还是「你选了 mock」）",
          sorted(st_tr))
    check(isinstance(st_tr.get("requests"), int) and isinstance(st_tr.get("cache_hits"), int),
          "请求数与缓存命中都是整数（面板要直接它们）", st_tr)

    # 第 82 轮：托盘是「配置里关掉的」还是「想装没装上」，状态文件里必须分得清 ——
    # 以前只有 tray 一个结果字段，面板/体检只能猜，于是把用户关掉的托盘写成故障。
    st_top = daemon.read_status(tmp_root) or {}
    check(st_top.get("tray_enabled") is True,
          "状态里报了 tray_enabled（配置里托盘是开着的）", st_top.get("tray_enabled"))
    # 第 86 轮：shows 连提示条也数，候选窗的次数必须单独报 —— 否则面板/体检只能
    # 拿 shows 当「候选窗出现过」的证据，只弹过启动引导的机器会被判成正常。
    ov_top = st_top.get("overlay") or {}
    check("cand_shows" in ov_top and isinstance(ov_top.get("cand_shows"), int),
          "状态里单独报了 cand_shows（候选窗弹过几次，不算提示条）", sorted(ov_top))
    check(ov_top.get("cand_shows") == 0 and isinstance(ov_top.get("shows"), int),
          "这个实例没敲过键：cand_shows 必须是 0（shows 可能已经涨过）", ov_top)

    t1 = mtime_ns(hb_status)
    app._status_sig = dict(app._status_sig)      # 制造「一模一样」的一版状态
    app._last_write = time.monotonic()           # 心跳刚过
    app._publish_status()
    check(mtime_ns(hb_status) == t1, "状态没变、心跳没到点 → 不写盘（不能每拍都写）")

    app._last_write = time.monotonic() - (hb - 0.5)
    app._publish_status()
    check(mtime_ns(hb_status) == t1, "心跳还差半秒 → 仍然不写盘（边界在常量上）",
          f"差 {hb - 0.5:.1f}s")

    app._last_write = time.monotonic() - (hb + 0.5)
    app._publish_status()
    t2 = mtime_ns(hb_status)
    check(t2 > t1, "心跳过了 → 状态没变也写盘（mtime 前进，这就是本轮修的）")
    check(daemon.status_is_stale(tmp_root) is False, "于是它不会被判成「卡死或被强杀」")
    age = daemon.status_age(tmp_root)
    check(age is not None and age < 1.0, "体检读到的 age 是「刚刚」",
          f"age={age:.3f}s" if age is not None else "age=None")

    # ------------------------------------------------------------------ #
    # 3. 写不进去要说话，而且别每拍重试
    # ------------------------------------------------------------------ #
    print("\n-- 3. 状态文件写不进去的时候 --")
    real_write = daemon.write_status
    calls = {"n": 0}

    def failing_write(root_dir, data):
        calls["n"] += 1
        return False

    daemon.write_status = failing_write
    try:
        app._warnings.clear()
        app._status_write_warned = False
        app._last_write = time.monotonic() - (hb + 0.5)
        app._publish_status()
        check(calls["n"] == 1, "确实尝试写了一次")
        check(app._status_write_warned is True, "记下「已经为这件事吵过一次」")
        check(any("状态文件写不进去" in w for w in app._warnings),
              "把「写不进去」说出来了（以前这个返回值被丢掉）",
              " / ".join(w.splitlines()[0] for w in app._warnings)[:60])
        check(not any("写不进去" in w for w in app._warnings[1:]),
              "只排了一条，不会刷屏")

        warned = len(app._warnings)
        app._last_write = time.monotonic() - (hb + 0.5)
        app._publish_status()
        check(calls["n"] == 2, "下一次心跳仍然会重试（不是一次失败就放弃）")
        check(len(app._warnings) == warned, "但不会重复排队同样的提醒")

        app._last_write = time.monotonic() - 0.1
        app._publish_status()
        check(calls["n"] == 2, "失败之后不会每拍都去撞一次（按心跳节流）")
    finally:
        daemon.write_status = real_write

    app._last_write = time.monotonic() - (hb + 0.5)
    app._publish_status()
    check(app._status_write_warned is False, "写成功之后提醒状态复位（下次失败还会说）")
    check(app._status_sig is not None, "失败期间没有把状态记成「已发布」（否则就永久骗下去了）")

    # ------------------------------------------------------------------ #
    # 4/5. 体检与控制面板的文案（要碰真状态文件，有实例在跑就跳过）
    # ------------------------------------------------------------------ #
    fake = {"pid": os.getpid(), "root": str(PROJ), "started": time.time(),
            "enabled": True, "direct": False, "tray": False, "hook": True,
            "hook_info": {"installed": True, "alive": True, "error": None, "total": 0,
                          "real": 0, "injected": 0, "self_injected": 0,
                          "foreign_injected": 0, "effective": 0, "swallowed": 0,
                          "probes": 0, "probe_seen": 0, "probe_failures": 0,
                          "probe_send_failures": 0, "probe_error": None,
                          "last_real_age": None, "last_effective_age": None},
            "overlay": {"shows": 1, "visible": False, "on_monitor": True,
                        "rect": [0, 0, 10, 10], "error": None},
            "tick_errors": 0, "hook_reinstalls": 0, "hook_misses": 0,
            "hook_reconciles": 0, "hook_giveup": False}
    if busy:
        print("\n-- 4/5. 跳过（已经有一个实例在跑，不打扰它，也不和它抢状态文件） --")
    else:
        print("\n-- 4. --doctor 对「活着的空闲实例」怎么说 --")

        def run_doctor():
            return subprocess.run(
                [sys.executable, "main.py", "--doctor", "--doctor-quick"],
                cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=180,
            )

        def status_line(out: str) -> str:
            for line in out.splitlines():
                if "状态文件" in line:
                    return line.strip()
            return ""

        daemon.write_status(PROJ, dict(fake))
        t = time.time()
        os.utime(status_path, (t, t))
        out = run_doctor()
        line = status_line(out.stdout or "")
        check("心跳正常" in line and "秒前更新过" in line,
              "新鲜的心跳 → 「N 秒前更新过（心跳正常）」", line)
        check("可能卡死" not in line, "不再无端报「卡死或被强杀」")

        old = time.time() - 60.0
        os.utime(status_path, (old, old))
        out = run_doctor()
        line = status_line(out.stdout or "")
        check("没更新" in line and "可能卡死" in line,
              "真的 60 秒没心跳 → 照样报「可能卡死或被强杀」", line)
        check(str(int(stale_s)) in line and str(int(hb)) in line,
              "并且把「心跳几秒一次、超过几秒才算」写在话里", line)

        print("\n-- 5. 第二次双击的控制面板 --")
        held = W.single_instance("translate-ime")   # 让 main.py 走「已经在跑」那条路
        check(bool(held) or W.instance_exists(), "本进程拿到了单实例互斥体（面板分支才会走）")

        def run_panel() -> str:
            proc = subprocess.run(
                [sys.executable, "main.py"], cwd=str(PROJ), capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=90,
            )
            return (proc.stdout or "") + (proc.stderr or "")

        t = time.time()
        os.utime(status_path, (t, t))
        text = run_panel()
        check("已经在跑了" in text, "第二次启动确实走了控制面板那条路")
        check("可能已经卡死或被强杀了" not in text,
              "心跳正常时面板不再说「可能已经卡死或被强杀了」",
              [ln.strip() for ln in text.splitlines() if "状态文件" in ln][:1])

        old = time.time() - 60.0
        os.utime(status_path, (old, old))
        text = run_panel()
        check("没更新了" in text, "真的不动了时面板照样提醒", 
              [ln.strip() for ln in text.splitlines() if "没更新" in ln][:1])
        check(str(int(hb)) in text, "并且说清楚心跳是几秒一次")
        check(release_mutex(), "交还单实例锁（下一节要起真实例）")
        check(W.instance_exists("translate-ime") is False,
              "锁交还之后没有实例占着它了")

    # ------------------------------------------------------------------ #
    # 6. 真机：空闲实例的 mtime 自己在走，而状态字段一个都没变
    # ------------------------------------------------------------------ #
    print("\n-- 6. 真机：空闲实例的心跳 --")
    if busy:
        print("   跳过（已经有实例在跑）")
    else:
        if status_path.exists():
            status_path.unlink()
        subprocess.run([sys.executable, "main.py", "--background", "--no-hook"],
                       cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=90)
        st = wait_for(lambda: daemon.read_status(PROJ), 25.0)
        child_pid = int((st or {}).get("pid") or 0)
        check(child_pid > 0, "常驻实例起来了", f"pid={child_pid}")
        # 等启动那一阵子（引导、托盘、欢迎提示）过去，再看空闲时会不会写盘
        wait_for(lambda: daemon.status_age(PROJ) is not None
                 and daemon.status_age(PROJ) < 2.0, 20.0)
        before = daemon.read_status(PROJ) or {}
        t1 = mtime_ns(status_path)
        age1 = daemon.status_age(PROJ)
        time.sleep(hb + 2.0)
        t2 = mtime_ns(status_path)
        age2 = daemon.status_age(PROJ)
        after = daemon.read_status(PROJ) or {}
        check(t2 > t1, "什么都不干，状态文件也被改写了（这就是心跳）",
              f"mtime 前进了 {(t2 - t1) / 1e9:.1f}s")
        check(age2 is not None and age2 < hb + 1.0,
              f"体检读到的 age 保持在心跳间隔以内（< {hb + 1.0:.0f}s）",
              f"age {age1:.1f}s → {age2:.1f}s" if age1 is not None else "age=None")
        check(daemon.status_is_stale(PROJ) is False, "空闲实例不会被判成「卡死或被强杀」")

        # 心跳只该动 mtime，不该把状态里的东西改掉（不然就不是心跳而是乱写）
        keys = ("pid", "enabled", "direct", "hook", "hook_reinstalls", "hook_misses",
                "hook_reconciles", "started")
        same = all(before.get(k) == after.get(k) for k in keys)
        check(same, "两次快照之间这些字段一个都没变（证明是心跳，不是状态变化）",
              f"pid {before.get('pid')} → {after.get('pid')}")
        check(float(after.get("updated") or 0) > float(before.get("updated") or 0),
              "状态文件里的 updated 也跟着前进了")

        if child_pid:
            daemon.send_command(PROJ, "quit")
            gone = wait_for(lambda: W.process_name(child_pid) == "", 20.0)
            check(bool(gone), "收尾：投 quit 之后实例退出了", f"pid={child_pid}")
            child_pid = 0
finally:
    for p, data in backup.items():
        try:
            p.write_bytes(data)
        except OSError:
            pass
    daemon.discard_commands(PROJ)
    release_mutex()
    if child_pid:
        subprocess.run(["taskkill", "/F", "/PID", str(child_pid)],
                       capture_output=True, text=True, timeout=30)
        try:
            status_path.unlink()
        except OSError:
            pass
    if app is not None:
        try:
            app.overlay.destroy()
        except Exception:
            pass

print()
print("=" * 74)
# 体检报告默认写在仓库根 —— 那是产品行为，但测试跑完不该留下它。
(PROJ / "translate-ime-doctor.txt").unlink(missing_ok=True)

passed = sum(1 for ok, _ in results if ok)
print(f"结果：{passed}/{len(results)} 通过" + ("（有实例在跑，跳过 4/5/6 节）" if busy else ""))
for ok, label in results:
    if not ok:
        print(f"  未通过：{label}")
print("=" * 74)
sys.exit(0 if passed == len(results) else 1)
