"""``main.py --hook-check`` 的**成功路径**：钩子真的看见按键时，它会说「这一层是通的」。

``tests/test_diagnostics.py`` 已经覆盖了「没人敲键 → 如实说没收到 → 退出码 6」这条
分支。可退出码 0 那条（真收到按键）在 ``tests/`` 里一次都没走过 —— 而它才是用户
嘴里「打字有反应」的直接证据。

这里用注入来模拟，分两个变体（判据正是产品代码里区分两类注入的那条线）：

* **A. 别的程序转发的按键**（``dwExtraInfo`` 不是本程序的标记）：钩子必须把它当成
  真实按键处理 —— 2026 年的改键器 / 宏 / 软键盘就是这么进来的，全丢掉的话症状就是
  「打字完全没反应」。期望：退出码 0、结论段说「当成真实按键处理」。
* **B. 本程序自己的回灌**（``dwExtraInfo`` 打的是 ``T_IME_INJECT_TAG``）：必须被
  忽略，不能算成用户按键，否则自激。期望：退出码 6、结论段说「没收到**真实**按键」，
  并且不再自相矛盾地说「一个按键都没收到」（自检按键明明回来了）。

两轮都要求：先看到它自己开的前台小窗口（注入式自检**必须**有前台窗口才准，
这是 Low 完整性级别下踩出来的经验）、``[2] 钩子安装 成功``、``[2b] 自检按键 回来了``。

直接跑：``python tests/test_hook_check_live.py``（约 12 秒，会弹一个自检小窗口）
"""
import ctypes
import re
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime.console import fix_console_encoding  # noqa: E402
from t_ime import winapi as W  # noqa: E402

fix_console_encoding()

u32 = ctypes.windll.user32
u32.GetForegroundWindow.restype = ctypes.c_void_p
u32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
u32.FindWindowW.restype = ctypes.c_void_p
u32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]

TITLE = "translate-ime 键盘钩子自检"
COUNT_RE = re.compile(
    r"真实按键 (\d+) 个 · 别的程序转发的 (\d+) 个 · 我们自己注入的 (\d+) 个"
)

OK = "[OK]  "
BAD = "[FAIL]"
results: list[tuple[bool, str]] = []


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"  — {detail}" if detail else ""))
    return cond


def fg_title():
    hwnd = u32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    u32.GetWindowTextW(hwnd, buf, 256)
    return buf.value, hwnd


def inject(vk, extra_info):
    W._send([W._kb_input(vk, 0, 0, extra_info),
             W._kb_input(vk, 0, W.KEYEVENTF_KEYUP, extra_info)])


def run_variant(extra_info, window_wait=20.0, seconds="4"):
    """起一个 --hook-check，等它抢到前台，往它窗口里注入 n i 两个键，收输出。"""
    proc = subprocess.Popen(
        [sys.executable, "main.py", "--hook-check", "--hook-check-seconds", seconds],
        cwd=str(PROJ), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    t0 = time.time()
    saw_window = False
    while time.time() - t0 < window_wait:
        title, _hwnd = fg_title()
        if title == TITLE:
            saw_window = True
            break
        time.sleep(0.05)
    if saw_window:
        time.sleep(0.3)
        inject(ord("N"), extra_info)
        time.sleep(0.06)
        inject(ord("I"), extra_info)
    raw = proc.communicate(timeout=90)[0]
    out = raw.decode("utf-8", errors="replace")
    return proc.returncode, out, saw_window, time.time() - t0


def counts(out):
    m = COUNT_RE.search(out)
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def main():
    print("=" * 74)
    print("0. 前置：你正在用这个输入法时别抢键盘")
    print("=" * 74)
    if W.instance_exists():
        print("检测到已有实例在跑 —— 跳过（不打断你正在用的那一个）。")
        return 0
    check(True, "没有实例在跑，可以安全地抢一次前台")

    print()
    print("=" * 74)
    print("1. 别的程序转发的按键（改键器/宏/软键盘那种）必须算数 → 退出码 0")
    print("=" * 74)
    rc, out, saw, dt = run_variant(0)
    check(saw, f"它抢到了自己的前台窗口「{TITLE}」（注入式自检的前提）")
    check(rc == 0, "退出码 0（看到按键了）", f"exit={rc}")
    check("[2] 钩子安装" in out and "成功" in out.split("[2] 钩子安装", 1)[1][:40],
          "[2] 报告钩子安装成功")
    check("[2b] 自检按键" in out and "回来了" in out,
          "[2b] 自检按键回来了（钩子此刻确实在生效）")
    check("[4] 结论" in out, "[4] 有结论段")
    c = counts(out)
    check(c is not None, "结论段能读出三个计数", c)
    if c:
        real, foreign, own = c
        check(real == 0, "没有把你机器上真的手敲按键算进来（我们只注入了键）", real)
        check(foreign >= 4, "注入的 2 个键（按下+抬起）= 4 个事件被算成「别的程序转发的」",
              foreign)
        check(own >= 1, "它自己那次自检注入被单独记成「我们自己注入的」", own)
    check("当成真实按键处理" in out, "结论段解释了「别的程序转发也算真实按键」这件事")
    check(dt < 60.0, "整条命令 60 秒内结束", f"{dt:.1f}s")

    print()
    print("=" * 74)
    print("2. 本程序自己的回灌必须被忽略 → 退出码 6（不是 0）")
    print("=" * 74)
    rc2, out2, saw2, dt2 = run_variant(W.T_IME_INJECT_TAG)
    check(saw2, "第二次也抢到了自己的前台窗口")
    check(rc2 == 6, "退出码 6（如实说没收到用户按键）", f"exit={rc2}")
    c2 = counts(out2)
    check(c2 is not None, "结论段能读出三个计数", c2)
    if c2:
        real2, foreign2, own2 = c2
        check(real2 == 0 and foreign2 == 0,
              "自家回灌既不算真实按键、也不算「别的程序转发」", c2)
        check(own2 >= 4, "自家回灌被记到「我们自己注入的」名下（不会被当成用户按键）", own2)
    check("没收到**真实**按键" in out2,
          "结论段如实说「没收到真实按键」（自检按键也是收到的，不能一并否认）")
    check("一个按键都没收到" not in out2,
          "不会一边说「一个按键都没收到」一边说自检按键回来了")
    check("个自检按键" in out2, "把收到的自检按键个数写出来，能和结论行的数字对上")
    check("个事件（真实" in out2 and "注入" in out2,
          "进度行也把「真实」和「注入」分开数（不是只报一个合计数）")
    check("自检按键回来了" in out2, "同时说明钩子装上了、也还在生效")
    check(dt2 < 60.0, "第二次也 60 秒内结束", f"{dt2:.1f}s")

    print()
    print("=" * 74)
    print("3. 副产品：没有状态文件、没有起实例")
    print("=" * 74)
    check(not W.instance_exists(), "--hook-check 不会顺手把常驻实例起来")
    check(not (PROJ / "translate-ime.status.json").exists(), "没留状态文件")
    check(not (PROJ / "translate-ime-doctor.txt").exists(), "没留体检报告")
    # 第 66 轮：结尾那句日志提示以前写死 translate-ime.log，--log 指到别处时
    # 会把用户指向错误的文件（成功路径才会打印，自动测试注入不出「真实按键」）。
    src = (PROJ / "main.py").read_text(encoding="utf-8")
    check("日志 translate-ime.log" not in src and "日志：translate-ime.log" not in src,
          "main.py 不再写死日志文件名（提示跟着 --log / output.log_file 走）")
    # 第 72 轮：Shift 单击能被 input.shift_tap_toggles 关掉，那句「点一下 Shift
    # 会切到英文直通」必须挂在 tap_switch_hint(cfg) 的判断下，关掉时改说 toggle_direct。
    check("if tap_switch_hint(cfg):" in src,
          "hook-check 的 Shift 说明按配置出（tap_switch_hint 守着它）")
    check("切「英文直通」的键现在没有" in src,
          "Shift 单击关掉时 hook-check 会明说没有切直通的键")

    print()
    print("=" * 74)
    passed = sum(1 for ok, _ in results if ok)
    print(f"结果：{passed}/{len(results)} 通过")
    for ok, label in results:
        if not ok:
            print(f"  未通过：{label}")
    print("=" * 74)
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    sys.exit(main())
