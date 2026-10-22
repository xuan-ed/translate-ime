# -*- coding: utf-8 -*-
"""常驻模式（后台留存）的端到端测试 —— 这才是用户双击 ``run.bat`` 真正走的那条路。

在此之前，这条路只有手工验证过（起实例 → 投命令 → 看状态文件 → 退出）。
这里把它固化成回归测试，并且解决一个关键问题：**从外面证明候选窗真的出现过**。

用户反馈里最难查的一条是「打字没反应、候选窗根本不出现」。启动引导会在屏幕
上停 7 秒，所以只要进程正常起来，状态文件里的 ``overlay.shows`` 必然 >= 1：

* ``shows`` 恒为 0  → 窗口根本没出来（跟键盘、跟拼音都无关）
* ``shows`` > 0 但 ``on_monitor`` 为假 → 窗口显示到屏幕外了
* 两者都正常 → 窗口这条链路是好的，问题只能在钩子那一侧

如果机器上已经有一个实例在跑，本测试直接跳过（不打断用户的输入法）。

直接跑：``python tests/test_resident.py``
"""
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime import daemon  # noqa: E402
from t_ime import winapi as W  # noqa: E402
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


def wait_for(pred, timeout: float, interval: float = 0.25):
    """轮询到 pred() 为真；返回 pred 最后一次的值。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    return last


def kill(pid: int) -> None:
    """兜底清理：测试失败时别把实例留在用户机器上。"""
    if not pid:
        return
    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                   capture_output=True, text=True, timeout=30)


print("=" * 74)
if W.instance_exists():
    print("已经有一个 translate-ime 在跑，跳过常驻测试（不打断你正在用的输入法）")
    print("=" * 74)
    sys.exit(0)

# 清掉可能残留的运行痕迹，否则分不清是不是这次写的
for p in (status_path, ctl_path):
    if p.exists():
        p.unlink()
log_size0 = log_path.stat().st_size if log_path.exists() else 0
print("起一个后台常驻实例：python main.py --background --no-hook")
print("=" * 74)

proc = subprocess.run(
    [sys.executable, "main.py", "--background", "--no-hook"],
    cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8",
    errors="replace", timeout=90,
)
check(proc.returncode == 0, "启动器退出码为 0（它只负责把常驻实例拉起来）",
      f"exit={proc.returncode}" +
      (f" stderr={(proc.stderr or '').strip()[:200]}" if proc.returncode else ""))

st = wait_for(lambda: daemon.read_status(PROJ), 25.0)
pid = int((st or {}).get("pid") or 0)
try:
    check(bool(st), "常驻实例写出了状态文件")
    check(pid > 0, "状态文件里有进程号", f"pid={pid}")
    check(bool(st and st.get("hook") is False), "--no-hook 生效：状态里钩子没装")
    check(bool(st and st.get("enabled")), "实例默认是启用状态（不是启动即挂起）")
    check(bool(st and st.get("direct") is False), "实例默认是拼音模式")

    # 真·证据：候选窗（启动引导）确实在屏幕上出现过。
    # 注意不能拿「刚写出状态文件」那一瞬间的快照下结论 —— 它是最早的那次 tick
    # （十几毫秒），而启动引导要 600ms 后才弹、状态文件每秒才刷一次。
    shown = wait_for(
        lambda: ((daemon.read_status(PROJ) or {}).get("overlay") or {}).get("shows", 0) >= 1,
        15.0,
    )
    st = daemon.read_status(PROJ) or st
    ov = st.get("overlay") or {}
    shows = int(ov.get("shows") or 0)
    check(bool(shown), "候选窗至少显示过 1 次（启动引导真的画出来了）",
          f"shows={shows}" +
          ("" if shows else " ← 窗口根本没出来，这是「候选窗不出现」的直接证据"))
    check(bool(ov.get("on_monitor")), "候选窗落在某块显示器上（不是跑到屏幕外）",
          f"rect={ov.get('rect')}")
    check(not ov.get("error"), "候选窗显示没有报错", str(ov.get("error") or ""))
    check(int(st.get("tick_errors") or 0) == 0, "主循环没有异常（tick_errors=0）")

    # 命令通道：投一条命令，状态必须跟着变
    check(daemon.send_command(PROJ, "toggle_direct"), "能把命令写进收件箱")
    flipped = wait_for(
        lambda: (daemon.read_status(PROJ) or {}).get("direct") is True, 8.0
    )
    check(bool(flipped), "投 toggle_direct 之后状态真的变成了英文直通")
    check(not ctl_path.exists(), "命令收件箱被取走清空了（不是每次都重读）")

    # 退出：进程消失 + 状态文件撤掉
    check(daemon.send_command(PROJ, "quit"), "能投出 quit")
    gone = wait_for(lambda: W.process_name(pid) == "", 10.0)
    check(bool(gone), "常驻实例收到 quit 后真的退出了", f"pid={pid}")
    check(not status_path.exists(), "退出时把状态文件撤掉了（下次双击才不会误判还有实例）")

    if log_path.exists():
        tail = log_path.read_text(encoding="utf-8", errors="replace")
        check("收到命令：quit" in tail, "日志里能看到收到 quit")
        check("正在退出" in tail, "日志里能看到退出流程")
        check(log_path.stat().st_size > log_size0, "这次运行确实往日志里写了东西")
finally:
    if pid and W.process_name(pid) != "":
        print(f"...兜底清理：结束残留进程 {pid}")
        kill(pid)
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
