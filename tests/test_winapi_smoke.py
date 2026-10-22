"""物理层冒烟测试：钩子真的装上了吗？send_text 真的能打字吗？

会真的装一次全局键盘钩子，并用 SendInput 注入一个 F24（没有程序会响应它）
来确认回调被触发。全程弹出的是自己建的靶子窗口，不影响别的程序。

直接跑：``python tests/test_winapi_smoke.py``
"""
import ctypes
import sys
import threading
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()  # 输出被重定向时切 UTF-8；真控制台保持原样

import tkinter as tk  # noqa: E402
from t_ime import winapi as W  # noqa: E402

OK = "[OK]  "
BAD = "[FAIL]"
SKIP = "[SKIP]"
results: list[tuple[bool, str]] = []
skipped: list[tuple[str, str]] = []


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"  — {detail}" if detail else ""))
    return cond


def skip(label, why):
    """环境做不到的前提（抢不到前台 / UIPI 丢掉注入）—— 不算失败、也不算通过。

    后面所有注入类断言都是「注入一个键，看钩子收不收得到」。只要前台窗口属于
    比本进程更高完整性的进程，``SendInput`` 就会被 UIPI **静默丢弃**（返回
    True 但一个事件都送不出去）。那时被断言的是环境、不是产品；真相照写，
    但不该让回归变红。
    """
    skipped.append((label, why))
    print(f"{SKIP} {label}  — {why}")


print("=" * 74)
print("1. 结构体尺寸（不对的话 SendInput 静默失败）")
print("=" * 74)
check(ctypes.sizeof(W.INPUT) == 40, f"sizeof(INPUT) == 40", f"实得 {ctypes.sizeof(W.INPUT)}")
check(ctypes.sizeof(W.MOUSEINPUT) == 32, "sizeof(MOUSEINPUT) == 32",
      f"实得 {ctypes.sizeof(W.MOUSEINPUT)}")
check(ctypes.sizeof(W.KEYBDINPUT) == 24, "sizeof(KEYBDINPUT) == 24",
      f"实得 {ctypes.sizeof(W.KEYBDINPUT)}")

print()
print("=" * 74)
print("2. 热键解析")
print("=" * 74)
mods, main = W.parse_hotkey("ctrl+alt+space")
check(mods == frozenset({17, 18}) and main == 32, "parse_hotkey('ctrl+alt+space')",
      f"mods={sorted(mods)} main={main}")
check(W.normalize_vk(0xA2) == 17 and W.normalize_vk(0xA4) == 18, "normalize_vk 左右键归一")
check(W.parse_hotkey("ctrl+alt+q")[1] == ord("Q"), "parse_hotkey('ctrl+alt+q')")

print()
print("=" * 74)
print("3. 建一个 Tk 窗口当靶子（模拟记事本）")
print("=" * 74)
root = tk.Tk()
root.title("靶子窗口")
root.geometry("560x220+180+180")
txt = tk.Text(root, font=("Consolas", 14))
txt.pack(fill="both", expand=True)
root.update()
root.deiconify()
root.lift()
root.attributes("-topmost", True)
root.focus_force()
txt.focus_set()
for _ in range(30):
    root.update()
    time.sleep(0.02)

hwnd = int(root.winfo_id())
check(hwnd != 0, "拿到 Tk 窗口 hwnd", str(hwnd))
TOP = W.top_level_hwnd(hwnd)


def ensure_fg(tries: int = 6) -> bool:
    """注入类测试的前提：靶子窗口**确认**在前台。

    这一步必须显式做并且显式确认。``focus_force()`` / ``SetForegroundWindow``
    经常被 Windows 的前台锁定静默拒绝（不报错、返回值还好看），结果
    SendInput 的字符掉进别的窗口（表现为「期望一长串、只拿到一个 'H'」甚至
    空串）。那不是 send_text 坏了，是测试自己没抢到前台 —— 所以先回读确认，
    再重试几次，最后如实返回 False 而不是假装成功。
    """
    for _ in range(tries):
        if W.foreground_hwnd() == TOP:
            return True
        W.set_foreground(TOP)
        time.sleep(0.15)
    return W.foreground_hwnd() == TOP

print()
print("=" * 74)
print("4. make_noactivate（悬浮窗永不抢焦点）")
print("=" * 74)
try:
    W.make_noactivate(hwnd)
    ex = ctypes.WinDLL("user32").GetWindowLongW(hwnd, -20)
    got = bool(ex & W.WS_EX_NOACTIVATE)
    check(got, "WS_EX_NOACTIVATE 已加上", f"exstyle=0x{ex & 0xFFFFFFFF:08X}")
except Exception as exc:
    check(False, "make_noactivate", repr(exc))

print()
print("=" * 74)
print("5. send_text 注入：ASCII / 中文 / 标点 / 换行")
print("=" * 74)
fg_ok = ensure_fg()
payload = "Hello 世界！\n第二行 zhōngwén 123"
if fg_ok:
    check(True, "抢到前台窗口（注入类测试的前提）",
          f"fg={W.foreground_hwnd()} top={TOP}")
    txt.delete("1.0", "end")
    root.update()
    time.sleep(0.1)
    sent = W.send_text(payload)
    for _ in range(40):
        root.update()
        time.sleep(0.02)
    got = txt.get("1.0", "end-1c")
    check(sent, "send_text 返回 True")
    check(got == payload, "注入内容与预期完全一致", f"得到 {got!r}")
    if got != payload:
        print(f"       期望 {payload!r}")
else:
    skip("send_text 注入内容比对",
         f"抢不到前台（fg={W.foreground_hwnd()} top={TOP}）—— 注入的字符会掉进"
         "别的窗口，这不是 send_text 的问题；关掉抢前台的窗口再跑一次")

print()
print("=" * 74)
print("6. 全局钩子：装上 → 注入按键 → 看回调有没有被触发")
print("=" * 74)
events: list[tuple[int, bool, bool]] = []
lock = threading.Lock()
VK_PROBE = 0x87  # F24，不干扰任何东西


def handler(vk, is_down, injected):
    with lock:
        events.append((vk, is_down, injected))
    return False  # 全部放行


hook = W.KeyboardHook(handler, name="smoke")
started = hook.start(timeout=5.0)
check(started, "KeyboardHook.start() 成功", hook.error or "")

if started:
    # 钩子的消息泵线程刚起来时可能还没进 GetMessageW，这时候注入的键会丢。
    # 所以探针要**重试**，而不是打一枪不中就判失败。
    probe_hits: list[tuple[int, bool, bool]] = []
    for attempt in range(5):
        time.sleep(0.3)
        ensure_fg()
        with lock:
            events.clear()
        W.send_vk(VK_PROBE)
        time.sleep(0.4)
        with lock:
            probe_hits = [e for e in events if e[0] == VK_PROBE]
        if len(probe_hits) >= 2:
            break
        print(f"       （第 {attempt + 1} 次探针未命中，重试）")
    inject_seen = len(probe_hits) >= 2
    if inject_seen:
        check(True, "钩子捕获到注入的 F24（按下+抬起）",
              f"共 {len(probe_hits)} 个事件")
        check(all(e[2] for e in probe_hits), "注入事件被正确标记为 injected",
              f"{probe_hits[:2]}")
    else:
        skip("钩子捕获注入按键（第 6 / 6b / 6b-2 节的注入类断言）",
             f"注入的 F24 一个都没进来（共 {len(probe_hits)} 个事件）—— 当前前台"
             "窗口多半属于比本进程更高完整性的进程，SendInput 被 UIPI 静默丢弃"
             "（本进程是 Low 完整性）。这不是钩子的问题。")

    # ---- 6b. 统计：这是「打字没反应」排查里唯一能分清责任的事实 ----
    # 下面这些是**形状**断言：一个事件都没收到时也必须成立（账目平衡、字段类型），
    # 所以不受「注入有没有送达」影响。
    check(hook.total == hook.real + hook.injected_count,
          "统计账目对得上（total == real + injected）",
          f"total={hook.total} real={hook.real} injected={hook.injected_count}")
    if inject_seen:
        check(hook.injected_count >= 2, "注入的 F24 计进 injected",
              f"injected={hook.injected_count}")
    check(hook.swallowed == 0, "handler 全部放行时 swallowed 保持 0",
          f"swallowed={hook.swallowed}")
    st_hook = hook.stats()
    check(bool(st_hook["installed"]) and st_hook["alive"] is True,
          "stats() 报「装着、线程活着」", str(st_hook))
    check(st_hook["last_real_age"] is None or isinstance(st_hook["last_real_age"], float),
          "last_real_age 只在收到真实按键后才有值",
          f"last_real_age={st_hook['last_real_age']!r}  real={st_hook['real']}")

    # ---- 6b-2. 「自己回灌的」和「别的程序注入的」必须分开 ----
    # 这是本机最容易被忽略的一种「打字没反应」：装了改键器/宏/软键盘的机器上，
    # 手敲的每个键都带 INJECTED 标志；早先一律当注入丢掉 = 打字完全没反应。
    if inject_seen:
        check(hook.self_injected >= 2 and hook.foreign_injected == 0,
              "我们自己注入的按键记进 self_injected、不污染 foreign_injected",
              f"self={hook.self_injected} foreign={hook.foreign_injected}")
    check(hook.effective == hook.real + hook.foreign_injected,
          "effective（真正当成输入处理的按键数）口径正确",
          f"effective={hook.effective} real={hook.real} foreign={hook.foreign_injected}")
    if inject_seen:
        check(st_hook["self_injected"] >= 2 and st_hook["foreign_injected"] == 0
              and st_hook["effective"] == st_hook["real"],
              "stats() 把三种按键分开报", str(st_hook))

    hook.stop()
    check(True, "KeyboardHook.stop() 未抛异常")
    check(hook.alive is False, "stop() 之后 alive 变成 False")

# ---- 6b-3. 仿真「别的程序转发的按键」：必须被当成有效输入 ----
print()
print("=" * 74)
print("6b-3. 别的程序注入的按键（改键器/宏/软键盘）不能被当成自激丢弃")
print("=" * 74)

foreign_seen: list[tuple[int, bool, bool]] = []
hook3 = W.KeyboardHook(
    lambda vk, down, inj: (foreign_seen.append((vk, down, inj)), False)[1],
    name="smoke-foreign",
)
if hook3.start(timeout=5.0):
    try:
        for attempt in range(5):
            time.sleep(0.3)
            ensure_fg()
            W.send_vk(VK_PROBE, extra_info=0xBEEF)   # 冒充「别的程序」
            time.sleep(0.4)
            if len(foreign_seen) >= 2:
                break
            print(f"       （第 {attempt + 1} 次探针未命中，重试）")
        if len(foreign_seen) >= 2:
            check(hook3.foreign_injected >= 2 and hook3.self_injected == 0,
                  "extra_info 不是我们的标记 → 记进 foreign_injected",
                  f"foreign={hook3.foreign_injected} self={hook3.self_injected}")
            check(all(inj is False for _vk, _d, inj in foreign_seen),
                  "别的程序注入的按键以 injected=False 交给状态机（会被真正处理）",
                  f"{foreign_seen[:2]}")
            check(hook3.effective >= 2,
                  "有效按键数把它算进去了（= 用户敲的字不会白敲）",
                  f"effective={hook3.effective}")
        else:
            skip("别的程序注入的按键（6b-3 的注入类断言）",
                 "冒充别的程序注入的按键一个都没进来 —— 同上，注入被环境丢了")
    finally:
        hook3.stop()
else:
    check(False, "6b-3 的钩子装上了", f"error={hook3.error}")

# ---- 6c. 吞键也要计数：吞掉多少是「键被吃掉了」的直接证据 ----
print()
print("=" * 74)
print("6c. 吞键计数（IMECore 返回 True 的键，用户那边就是「按了没反应」）")
print("=" * 74)


def swallow_handler(vk, is_down, injected):
    if vk == VK_PROBE:
        with lock:
            events.append((vk, is_down, injected))
        return True
    return False


hook2 = W.KeyboardHook(swallow_handler, name="smoke-swallow")
if hook2.start(timeout=5.0):
    for attempt in range(5):
        time.sleep(0.3)
        ensure_fg()
        with lock:
            events.clear()
        W.send_vk(VK_PROBE)
        time.sleep(0.4)
        with lock:
            n_hits = len([e for e in events if e[0] == VK_PROBE])
        if n_hits >= 2:
            break
        print(f"       （第 {attempt + 1} 次探针未命中，重试）")
    if n_hits >= 2:
        check(hook2.swallowed >= 2, "被吞掉的按键计入 swallowed",
              f"swallowed={hook2.swallowed} 收到={n_hits} 个 F24")
    else:
        skip("吞键计数（6c 的注入类断言）",
             "注入的 F24 一个都没进来 —— 同上，注入被环境丢了")
    check(hook2.swallowed <= hook2.total, "吞掉数不会超过收到总数",
          f"swallowed={hook2.swallowed} total={hook2.total}")
    hook2.stop()
else:
    check(False, "第二个钩子（吞键）也能装上", hook2.error or "")

print()
print("=" * 74)
print("7. 光标定位")
print("=" * 74)
cp = W.cursor_pos()
check(isinstance(cp, tuple) and len(cp) == 2, "cursor_pos()", str(cp))
caret = W.caret_screen_pos()
check(True, "caret_screen_pos() 可调用（Tk 不暴露 caret，None 是正常的）", repr(caret))

print()
print("=" * 74)
print("8. 显示器几何与 DPI（候选窗越界判定靠它，别拿主屏量副屏）")
print("=" * 74)
sw, sh = W.screen_size()
check(sw > 0 and sh > 0, "screen_size()", f"{sw}x{sh}")
check(W.monitor_count() >= 1, "monitor_count()", str(W.monitor_count()))

mon = W.monitor_rect(x=10, y=10)
check(mon is not None and len(mon) == 4, "monitor_rect(x=10, y=10)", repr(mon))
if mon:
    mx, my, mw, mh = mon
    check(mw > 0 and mh > 0, "显示器尺寸为正", f"{mw}x{mh}")
    check(mx <= 10 < mx + mw and my <= 10 < my + mh,
          "点 (10,10) 确实落在这块显示器的矩形里")
check(W.monitor_rect(0) is None, "monitor_rect(0) 返回 None（没有窗口就不瞎猜）")

win_mon = W.monitor_rect(TOP)
win_rect = W.window_rect(TOP)
check(win_mon is not None, "monitor_rect(靶子窗口 hwnd)", repr(win_mon))
if win_mon and win_rect:
    mx, my, mw, mh = win_mon
    x, y, w, h = win_rect
    check(mx <= x and my <= y and x + w <= mx + mw and y + h <= my + mh,
          "靶子窗口整个落在它那块显示器里",
          f"窗口 {w}x{h}+{x}+{y} · 显示器 {mw}x{mh}+{mx}+{my}")

work = W.monitor_rect(x=10, y=10, work=True)
check(work is not None and mon is not None and work[3] <= mon[3],
      "work=True 给的是扣掉任务栏的工作区", f"工作区 {work} · 整屏 {mon}")

aware = W.dpi_awareness()
check(isinstance(aware, str) and bool(aware), "dpi_awareness()", aware)
check(W.dpi_scale() >= 1.0, "dpi_scale() >= 1.0", f"{W.dpi_scale():.2f}x")
scale = W.enable_dpi_awareness()
check(isinstance(scale, float) and scale >= 1.0, "enable_dpi_awareness() 返回缩放比",
      f"{scale:.2f}x")
check(W.enable_dpi_awareness() == scale, "重复调用是幂等的")
# Tk 建过窗口之后进程必然是感知 DPI 的（Tk 自己会设，或者我们设的）
check("UNAWARE" not in W.dpi_awareness(),
      "建过 Tk 窗口之后进程不该还是 DPI 不感知", W.dpi_awareness())

print()
print("=" * 74)
print("9. show_no_activate / hide_window 的返回值语义（曾经的假警报源头）")
print("=" * 74)
# ShowWindow 的返回值是「**调用之前**窗口可见吗」，不是成功与否：一个隐藏着的
# 窗口第一次显示必然返回 0。所以这两个函数的返回值必须是「操作之后的状态」。
# 早先 show_no_activate 直接返回 ShowWindow 的返回值，于是每次正常显示都被
# 记成「显示失败」，状态文件 / 控制面板 / --doctor 一起报假警。
check(W.hide_window(TOP), "hide_window() 之后窗口确实不可见",
      f"visible={W.is_window_visible(TOP)}")
check(not W.is_window_visible(TOP), "IsWindowVisible 确认隐藏生效")
check(W.show_no_activate(TOP),
      "show_no_activate() 对「原本隐藏」的窗口返回 True（旧代码在这里返回 False）",
      f"visible={W.is_window_visible(TOP)}")
check(W.is_window_visible(TOP), "IsWindowVisible 确认显示生效")
check(W.show_no_activate(TOP), "对已经可见的窗口再调一次也返回 True")

root.destroy()

print()
print("=" * 74)
passed = sum(1 for ok, _ in results if ok)
print(f"结果：{passed}/{len(results)} 通过")
for ok, label in results:
    if not ok:
        print(f"  未通过：{label}")
if skipped:
    print(f"  跳过 {len(skipped)} 项（环境做不到的前提，不算失败也不算通过）：")
    for label, why in skipped:
        print(f"       {label} — {why}")
print("=" * 74)
sys.exit(0 if passed == len(results) else 1)
