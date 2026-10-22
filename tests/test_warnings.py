# -*- coding: utf-8 -*-
"""坏消息要说给用户听：启动引导播完之后才发生的问题，也必须出现在候选窗上。

背景（第 14 轮实测出来的真 bug）：``App._add_warning`` 把「用户会以为程序坏了」的消息
攒起来，但**只有启动引导播完那一瞬间**（``_welcome_tip``）才会冲刷一次。而这个程序是
**常驻**的 —— 真正需要告诉用户的坏消息，几乎都不是启动时发生的：

* 钩子被 Windows 悄悄摘掉、程序自己重装成功（可能是常驻半小时之后）；
* 重装之后还是收不到键，程序决定**放弃**自动重装（此时打字是真坏了，用户更要知道）；
* 托盘图标被 shell 拒绝（启动时发生，这条以前倒是能显示）。

老版本的实际后果：这些消息只进了 stderr / 日志，而常驻时**根本没有控制台** ——
用户屏幕上什么都没有，只能看到「打字突然没反应」（或者几十秒后才恢复），
完全不知道发生过什么、也不知道原因在哪。

这个文件验五件事：

1. 引导**之前**攒下的坏消息，仍然由引导尾巴照常冲刷（老行为不能坏）；
2. 引导**之后**攒下的坏消息，会被主循环下一拍弹出来（本轮修的正是这个）；
3. 用户正在打字（``core.buffer`` 非空）时不弹，等他不打了再弹 —— 不能拿提示打断输入；
4. 多条坏消息合并成一条提示（不是一拍弹一条刷屏），弹过就清空、不会重复弹；
5. 真实触发路径：钩子漏键 → 自动重装 → 那条提醒真的能在引导之后出现在候选窗上。

直接跑：``python tests/test_warnings.py``
"""
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime import daemon  # noqa: E402
from t_ime.app import HOOK_MISS_CONFIRM_MS, App  # noqa: E402
from t_ime.config import load as load_config  # noqa: E402
from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

OK = "[OK]  "
BAD = "[FAIL]"
results: list[tuple[bool, str]] = []


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"  — {detail}" if detail else ""))


print("=" * 74)
cfg, cfg_path = load_config()
print(f"配置来自 {cfg_path}")
print("=" * 74)

# 这个测试会碰状态文件 / 命令收件箱，别把用户正在用的那个实例的痕迹弄丢
backup: dict[Path, bytes] = {}
for p in (daemon.status_path(PROJ), daemon.ctl_path(PROJ)):
    if p.exists():
        backup[p] = p.read_bytes()
daemon.take_commands(PROJ)


class FakeHook:
    """只够 App 用来「判断钩子还在不在」的最小假钩子。"""

    error = None

    def __init__(self, total=100, taps=()):
        self.total = total
        self.taps = tuple(taps)
        self.stopped = False

    def stats(self):
        return {"installed": True, "alive": True, "error": None, "total": self.total,
                "real": 0, "injected": 0, "self_injected": 0, "foreign_injected": 0,
                "effective": 0, "swallowed": 0, "probes": 0, "probe_seen": 0,
                "probe_failures": 0, "probe_send_failures": 0, "probe_error": None,
                "last_real_age": None, "last_effective_age": None}

    def sample_keydowns(self):
        return self.taps

    def stop(self):
        self.stopped = True


app = App(cfg, PROJ, verbose=False, install_hook=False)
toasts: list[tuple[str, float]] = []
app.core.show_toast = lambda text, seconds=3.0: toasts.append((text, seconds))
confirm = HOOK_MISS_CONFIRM_MS / 1000.0

try:
    # ------------------------------------------------------------------ #
    # 1. 引导之前攒的坏消息：仍然由引导尾巴冲刷（老行为）
    # ------------------------------------------------------------------ #
    print("\n-- 1. 引导之前的坏消息 --")
    check(app._welcome_done is False, "刚构造出来的 App 还没播过引导")
    app._add_warning("启动时的坏消息（托盘图标没能装上）")
    app._flush_warnings()
    check(not toasts, "引导还没播完，主循环这边不会抢先弹（否则会把引导顶掉）")
    app._welcome_tip()
    check(len(toasts) == 1, "引导播完时把它弹出来了", f"toasts={len(toasts)}")
    check("托盘图标没能装上" in toasts[0][0], "弹的就是那条坏消息",
          toasts[0][0].replace("\n", " / ") if toasts else "")
    check(app._welcome_done is True, "顺带记下「引导播完了」")
    check(not app._warnings, "弹过之后队列清空")

    # ------------------------------------------------------------------ #
    # 2. 引导之后的坏消息：本轮修的 bug —— 以前这些只在日志里
    # ------------------------------------------------------------------ #
    print("\n-- 2. 引导之后的坏消息（本轮修的这个） --")
    before = len(toasts)
    app._add_warning("键盘钩子被系统摘掉过，已经自动重装（第 1 次）")
    app._add_warning("多半是别的程序（改键器 / 宏软件 / 安全软件）把按键吃掉了")
    check(len(app._warnings) == 2, "两条坏消息先攒着")
    app._tick_once()
    check(len(toasts) == before + 1, "主循环下一拍就弹出来了（以前永远不会）",
          f"toasts={len(toasts) - before}")
    last = toasts[-1][0]
    check("自动重装" in last and "改键器" in last,
          "两条消息合成**一条**提示（不是一拍一条刷屏）", last.replace("\n", " / "))
    check(toasts[-1][1] >= 5.0, "这种消息留的时间比启动提示长（用户没预期会看到）",
          f"seconds={toasts[-1][1]}")
    check(not app._warnings, "弹过就清空")
    app._tick_once()
    check(len(toasts) == before + 1, "不会重复弹同一条")

    # ------------------------------------------------------------------ #
    # 3. 用户正在打字时不打断
    # ------------------------------------------------------------------ #
    print("\n-- 3. 打字时不打断 --")
    before = len(toasts)
    app._add_warning("打字时来的坏消息")
    app.core.buffer = "nihao"
    app._tick_once()
    check(len(toasts) == before, "正在打拼音时不弹提示（不能拿提示打断输入）")
    check(len(app._warnings) == 1, "但消息还在队列里，不会丢")
    app.core.buffer = ""
    app._tick_once()
    check(len(toasts) == before + 1, "用户打完了，下一拍就弹出来")
    check("打字时来的坏消息" in toasts[-1][0], "弹的正是那条")

    # ------------------------------------------------------------------ #
    # 4. 真实触发路径：钩子漏键 → 自动重装 → 提醒能出现在候选窗上
    # ------------------------------------------------------------------ #
    print("\n-- 4. 钩子重装那条提醒真的说得出口 --")
    before = len(toasts)
    app._hook_reinstalls = 0
    app._hook_ok_since_reinstall = False
    app._hook_giveup = False
    app._hook_misses = 0
    app._miss_pending = False
    app._last_total = -1
    for i in range(2):
        app.hook = FakeHook(total=500 + i, taps=(0x41,))
        app._last_total = -1
        app._check_hook_health(2000.0 + i * 10)
        app._check_hook_health(2000.0 + i * 10 + confirm + 0.01)
    check(app._hook_reinstalls == 1, "连着两次漏键 → 真的重装了一次",
          f"reinstalls={app._hook_reinstalls}")
    check(any("重装" in w for w in app._warnings), "重装时往队列里放了提醒",
          repr(app._warnings[-1:] if app._warnings else ""))
    app._tick_once()
    check(len(toasts) == before + 1, "下一拍就把它说到候选窗上（常驻时用户唯一的可见面）")
    check("重装" in toasts[-1][0], "内容的确实是重装这件事",
          toasts[-1][0].replace("\n", " / "))

    # ------------------------------------------------------------------ #
    # 5. 放弃重装那条更要说得出口（此时打字是真的坏了）
    # ------------------------------------------------------------------ #
    print("\n-- 5. 「放弃自动重装」也要说得出口 --")
    before = len(toasts)
    app._hook_giveup = False
    app._hook_ok_since_reinstall = False
    app._hook_misses = 0
    app._miss_pending = False
    for i in range(2):
        app.hook = FakeHook(total=600 + i, taps=(0x42,))
        app._last_total = -1
        app._check_hook_health(3000.0 + i * 10)
        app._check_hook_health(3000.0 + i * 10 + confirm + 0.01)
    check(app._hook_giveup, "又连着两次漏键 → 放弃自动重装")
    app._tick_once()
    check(len(toasts) == before + 1, "放弃重装也会说到候选窗上")
    check("停止自动重装" in toasts[-1][0], "并且说清楚已经停了", 
          toasts[-1][0].replace("\n", " / "))
    check("重启" in toasts[-1][0], "还给用户一条出路（重启程序）")

    # ------------------------------------------------------------------ #
    # 6. 第 20 轮：Shift 切换的说明文案要跟着时间窗走
    #    第 20 轮真机实测发现「按住 500ms 松手 = 什么都没发生」，于是时间窗
    #    从 300ms 放宽到 600ms。文案里那个秒数以前是各写各的，改默认值的时候
    #    很容易漏掉一处 —— 用户照着「0.3 秒」去按，还是切不了。
    # ------------------------------------------------------------------ #
    print("\n-- 6. Shift 切换的说明文案要跟时间窗一致 --")
    before = len(toasts)
    shown = app.show_help(0.01)
    check(shown == 0.01, "show_help() 返回它停留的秒数", f"{shown}")
    check(len(toasts) == before + 1, "说明是弹在候选窗上的")
    text = toasts[-1][0]
    check("点一下 Shift" in text, "说明里用的是「点一下」这个动作（不是「单击」）",
          text.replace("\n", " / ")[:80])
    win = app.core.shift_tap_ms / 1000.0
    check(f"{win:.1f} 秒" in text, f"文案里的秒数等于当前时间窗（{win:.1f} 秒）")
    check("按住超过 0.3 秒" not in text, "没有残留的旧数字 0.3 秒（时间窗早就是 0.6 了）")
    check(app.core.shift_tap_ms == 600, "当前时间窗就是 600ms", f"{app.core.shift_tap_ms}")
finally:
    for p, data in backup.items():
        p.write_bytes(data)
    # 测试自己造出来的状态文件必须收掉：它记的是**这个测试进程**的 pid，
    # 留着它，用户下次双击 run.bat 就会看到「状态文件超过 10 秒没更新，
    # 那个实例可能已经卡死或被强杀了」——一句纯粹的吓人话。
    for p in (daemon.status_path(PROJ), daemon.ctl_path(PROJ)):
        if p not in backup:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
    daemon.discard_commands(PROJ)
    try:
        app.overlay.destroy()
    except Exception:
        pass

print()
print("=" * 74)
passed = sum(1 for ok, _ in results if ok)
print(f"结果：{passed}/{len(results)} 通过")
for ok, label in results:
    if not ok:
        print(f"  未通过：{label}")
print("=" * 74)
sys.exit(0 if passed == len(results) else 1)
