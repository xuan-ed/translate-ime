# -*- coding: utf-8 -*-
"""崩溃恢复：上一个实例被强杀后留下的「遗嘱」，不能让新实例当场自杀。

真实故障现场（手工复现过，现象是「双击了 run.bat，输入法压根没起来」：

1. 实例正在跑，被 ``taskkill /F``（或崩溃、注销、断电）干掉 —— ``shutdown()``
   没机会执行，命令收件箱里留着一条 ``quit``，状态文件也没清；
2. 用户双击 ``run.bat`` → 新实例起来 → 第一个 tick 把 ``quit`` 读走 → 当场退出。

没有报错、没有窗口，日志里只有一行 ``[app] 收到命令：quit``，所以极难归因。
（命令是发给*上一个进程*的，属于上一个生命周期。）

修法：``App.start()`` 先调 ``daemon.discard_commands()`` 丢掉残留命令、覆盖残留状态
文件，并把丢掉的内容写进日志。本测试把整个现场重演一遍。

如果机器上已经有一个实例在跑，直接跳过（不打断用户的输入法）。

直接跑：``python tests/test_crash_recovery.py``
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
FAKE_PID = 999999
results: list[tuple[bool, str]] = []
status_path = daemon.status_path(PROJ)
ctl_path = daemon.ctl_path(PROJ)
log_path = PROJ / "translate-ime.log"


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"  — {detail}" if detail else ""))
    return cond


def wait_for(pred, timeout: float, interval: float = 0.25):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    return last


def kill(pid: int) -> None:
    if not pid:
        return
    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                   capture_output=True, text=True, timeout=30)


def read_log_tail(since: int) -> str:
    if not log_path.exists():
        return ""
    data = log_path.read_bytes()
    return data[since:].decode("utf-8", errors="replace")


print("=" * 74)
if W.instance_exists():
    print("已经有一个 translate-ime 在跑，跳过崩溃恢复测试（不打断你正在用的输入法）")
    print("=" * 74)
    sys.exit(0)

# 清场，否则分不清哪些痕迹是这次写的
daemon.clear_status(PROJ)
log_size0 = log_path.stat().st_size if log_path.exists() else 0


def plant_leftovers() -> None:
    """伪造「上一个实例被强杀」的现场：残留状态文件 + 收件箱里两条命令。"""
    daemon.write_status(PROJ, {
        "pid": FAKE_PID,
        "root": str(PROJ),
        "started": time.time() - 600,
        "enabled": True,
        "direct": False,
        "active": "zh",
        "tray": False,
        "hook": False,
    })
    ctl_path.write_text("toggle_enabled\nquit\n", encoding="utf-8")


print("现场布置：残留状态文件（假 pid） + 收件箱里放着 toggle_enabled / quit")
plant_leftovers()
check(status_path.exists() and ctl_path.exists(), "现场布置完成（两个文件都在）",
      f"status={status_path.exists()} ctl={ctl_path.exists()}")

pid = 0
print()
print("现在双击 run.bat 的等价路径：python main.py --background --no-hook")
print("=" * 74)
proc = subprocess.run(
    [sys.executable, "main.py", "--background", "--no-hook"],
    cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8",
    errors="replace", timeout=90,
)
check(proc.returncode == 0, "启动器退出码为 0", f"exit={proc.returncode}")

try:
    # 注意不能只等「状态文件存在」—— 布置现场时就有一个残留的状态文件，
    # 那一瞬间读到的是**假 pid**（上一版的测试就栽在这里）。要等的是
    # 「pid 不再是那个假 pid」，也就是新实例真的把它覆盖了。
    def new_instance_status():
        d = daemon.read_status(PROJ)
        if d and int(d.get("pid") or 0) not in (0, FAKE_PID):
            return d
        return None

    st = wait_for(new_instance_status, 25.0)
    pid = int((st or {}).get("pid") or 0)
    check(bool(st) and pid > 0, "新实例写出了状态文件", f"pid={pid}")
    check(pid != FAKE_PID, "残留状态文件被新实例覆盖了（pid 不再是那个假的）",
          f"pid={pid} fake={FAKE_PID}")

    # 关键：给它足够时间把收件箱读一遍 —— 修复前这里就是它自杀的时刻
    time.sleep(4.0)
    alive = W.process_name(pid) != ""
    check(alive, "4 秒后实例还活着（残留的 quit 没有把它带走）",
          f"pid={pid} process_name={W.process_name(pid)!r}")
    check(not ctl_path.exists(), "残留的收件箱已经被丢掉（文件不存在了）")

    st = daemon.read_status(PROJ) or st
    check(bool(st and st.get("enabled")), "残留的 toggle_enabled 没有被执行（仍是启用状态）",
          f"enabled={(st or {}).get('enabled')}")

    if log_path.exists():
        tail = read_log_tail(log_size0)
        check("丢掉了上一个实例残留的 2 条命令" in tail,
              "日志里记下了「丢掉了残留命令」，这件事有据可查")
        check("toggle_enabled" in tail and "quit" in tail,
              "日志里能看到丢掉的具体命令内容")
        check("覆盖了上一个实例残留的状态文件" in tail,
              "日志里说明状态文件是覆盖来的")

    # 正常退出路径仍然可用
    check(daemon.send_command(PROJ, "quit"), "能投出 quit")
    gone = wait_for(lambda: W.process_name(pid) == "", 10.0)
    check(bool(gone), "这次真的收到 quit 并退出了（恢复之后命令通道照常工作）",
          f"pid={pid}")
    check(not status_path.exists(), "退出时把状态文件撤掉了")
finally:
    # 兜底清理：pid 变量可能还是假的（测试中途失败时），所以直接信状态文件
    real = daemon.read_status(PROJ) or {}
    for candidate in (pid, int(real.get("pid") or 0)):
        if candidate and candidate != FAKE_PID and W.process_name(candidate) != "":
            print(f"...兜底清理：结束残留进程 {candidate}")
            kill(candidate)
    daemon.clear_status(PROJ)

print()
print("=" * 74)
passed = sum(1 for ok, _ in results if ok)
print(f"结果：{passed}/{len(results)} 通过")
for ok, label in results:
    if not ok:
        print(f"  未通过：{label}")
print("=" * 74)
sys.exit(0 if passed == len(results) else 1)
