# -*- coding: utf-8 -*-
"""单实例锁：**「问一句」和「我要用」必须是两个函数。**

第 21 轮验用户反馈第四条（「Console 关掉程序即死」）时，我自己的探针被自己
坑了一次：探针只想问一句「有实例在跑吗」，就调了 ``daemon.already_running()``，
而它当时的实现是 ``not W.single_instance(...)`` —— 那个调用会**把锁据为己有**、
一直挂到进程退出。于是探针自己变成了「那个实例」：随后 ``run.bat`` 里的
``main.py`` 看到锁被占，走了「已经在跑了」那条路，弹出控制面板
（还打印「读不到状态：可能是旧版本在跑，或者状态文件被删了」），
**真正的常驻实例压根没起来**，而用户看到的就是「双击了没反应」。

所以这一节测三件事：

1. ``is_running()`` / 老名字 ``already_running()`` 是**纯查询**：连问 50 次，
   自己也不会变成长着锁的那个进程（本进程 + 跨进程各测一遍）。
2. ``claim_instance()`` 才是「我要用」：它真的把锁拿走，别的进程看得见；
   进程一死锁就还回去。
3. 接线与文案：``main.py`` 里用的必须是 ``claim_instance()``，
   而控制面板那条路（第 166 行附近）用的是纯探测的 ``W.instance_exists()``。

最后一节是**真机**：起一个后台常驻实例，从外面问它的控制台
（``FreeConsole()`` 之后 ``AttachConsole(pid)``）—— 答案必须是「没有控制台」
（err=6）。没有控制台，就没有「关掉控制台即死」这回事。
"""
import ctypes
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

status_path = daemon.status_path(PROJ)
ctl_path = daemon.ctl_path(PROJ)
results: list[tuple[bool, str]] = []
skipped = 0


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{'[OK]  ' if cond else '[FAIL]'} {label}" + (f"  — {detail}" if detail else ""))


def skip(label, detail=""):
    global skipped
    skipped += 1
    results.append((True, label))
    print(f"[SKIP] {label}" + (f"  — {detail}" if detail else ""))


def wait_for(fn, seconds, step=0.25):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(step)
    return None


def kill(pid: int) -> None:
    if pid:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, text=True, timeout=30)


def child(code: str, timeout=30):
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def source_of(rel: str) -> str:
    return (PROJ / rel).read_text(encoding="utf-8", errors="replace")


print("=" * 74)
print("  单实例锁：问一句 vs 我要用")
print("=" * 74)

busy = W.instance_exists("translate-ime")
if busy:
    print("已经有一个 translate-ime 在跑（可能是你自己在用），跳过需要独占锁的检查")

# ---------------------------------------------------------------------- #
# 1. 纯查询：本进程里连问 50 次，自己不会变成持锁人
# ---------------------------------------------------------------------- #
print("\n-- 1. is_running() / already_running() 是纯查询 --")
check(hasattr(daemon, "is_running") and callable(daemon.is_running),
      "daemon.is_running() 存在")
check(hasattr(daemon, "claim_instance") and callable(daemon.claim_instance),
      "daemon.claim_instance() 存在")
check("is_running" in daemon.__all__ and "claim_instance" in daemon.__all__,
      "两个新名字都导出了（__all__）")

if busy:
    skip("连问 50 次之后，本进程仍然没有占着锁", "有人在跑，测不了")
else:
    check(daemon.is_running() is False, "没人跑的时候 is_running() 是 False")
    for _ in range(50):
        daemon.is_running()
    daemon.already_running()
    daemon.already_running()
    check(W.instance_exists("translate-ime") is False,
          "连问 50 次 + 调老名字，本进程依然**没有**把锁拿走",
          "（老实现 single_instance 会据为己有，这就是「双击了没反应」的成因）")

# ---------------------------------------------------------------------- #
# 2. 跨进程：一个只问一句的子进程，退出前也不该占着锁
# ---------------------------------------------------------------------- #
print("\n-- 2. 跨进程：只问一句的子进程不占锁 --")
if busy:
    skip("子进程只调 is_running() / already_running()，不会占住锁", "有人在跑，测不了")
else:
    code = (
        "import sys; sys.path.insert(0, r'{proj}')\n"
        "from t_ime import daemon, winapi as W\n"
        "print('is_running=', daemon.is_running())\n"
        "print('after_is=', W.instance_exists('translate-ime'))\n"
        "print('legacy=', daemon.already_running())\n"
        "print('after_legacy=', W.instance_exists('translate-ime'))\n"
    ).format(proj=str(PROJ))
    proc = child(code)
    out = (proc.stdout or "").strip()
    check("after_is= False" in out,
          "子进程调完 is_running()，锁还在原地（没人占）",
          out.replace("\n", " | ")[:160])
    check("after_legacy= False" in out,
          "老名字 already_running() 现在也只问不占",
          out.replace("\n", " | ")[:160])

    code = (
        "import sys, time; sys.path.insert(0, r'{proj}')\n"
        "from t_ime import daemon\n"
        "print('claimed=', daemon.claim_instance(), flush=True)\n"
        "time.sleep(30)\n"
    ).format(proj=str(PROJ))
    holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                              errors="replace")
    try:
        line = holder.stdout.readline().strip()
        check("claimed= True" in line, "子进程 claim_instance() 抢到了锁", line)
        seen = wait_for(lambda: W.instance_exists("translate-ime"), 8.0)
        check(bool(seen), "★ 子进程拿着锁的时候，别的进程问得到「有实例」")
        check(daemon.is_running() is True,
              "而且 is_running()（纯查询）也看得到它 —— 只问不占")
    finally:
        kill(holder.pid)
    freed = wait_for(lambda: not W.instance_exists("translate-ime"), 15.0)
    check(bool(freed), "★ 子进程一死，锁自动还回来（不存在锁泄漏）")

# ---------------------------------------------------------------------- #
# 3. 接线与源码形状
# ---------------------------------------------------------------------- #
print("\n-- 3. 接线：谁该 claim、谁只能问 --")
dae = source_of("t_ime/daemon.py")
main = source_of("main.py")

check("def is_running()" in dae and "def claim_instance()" in dae,
      "daemon.py 里两个函数都在")
check('W.instance_exists("translate-ime")' in dae,
      "is_running() 的实现是纯探测 W.instance_exists（只借句柄看一眼就还）")
check("return W.single_instance" in dae,
      "claim_instance() 的实现是真的抢锁 W.single_instance")
check("if not daemon.claim_instance():" in main,
      "main.py 的常驻分支用的是 claim_instance()（真抢锁）")
check("daemon.already_running()" not in main,
      "main.py 里不再出现「像查询、其实抢锁」的老调用")
check("if not args.detached and W.instance_exists():" in main,
      "控制面板那条路用的是纯探测 W.instance_exists()（它不能带走锁）")

# ---------------------------------------------------------------------- #
# 4. 真机：后台常驻实例根本没有控制台
# ---------------------------------------------------------------------- #
print("\n-- 4. 真机：后台常驻实例没有控制台（所以关控制台碰不到它） --")
cmd = daemon.background_command(PROJ, [])
check(bool(cmd) and "--detached" in cmd,
      "后台启动命令里带 --detached（脱离控制台跑）", " ".join(cmd or [])[:120])
check("0x00000008" in source_of("t_ime/daemon.py"),
      "spawn_background 用了 DETACHED_PROCESS（0x00000008）")
check("--background" in source_of("run.bat"),
      "run.bat 默认就传 --background（用户双击走的这条路不挂控制台）")

if busy:
    skip("真机：常驻实例没有控制台 + is_running() 看得到它", "有人在跑，不打扰")
else:
    for p in (status_path, ctl_path):
        if p.exists():
            p.unlink()
    print("  起一个后台常驻实例：python main.py --background --no-hook")
    starter = subprocess.run([sys.executable, "main.py", "--background", "--no-hook"],
                             cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=120)
    check(starter.returncode == 0, "启动器退出码 0", f"exit={starter.returncode}")
    st = wait_for(lambda: daemon.read_status(PROJ), 40.0)
    pid = int((st or {}).get("pid") or 0)
    try:
        check(bool(st) and pid > 0, "常驻实例起来了", f"pid={pid}")
        if pid:
            check(daemon.is_running() is True,
                  "is_running()（纯查询）在实例跑着的时候是 True")
            probe = (
                "import ctypes, sys\n"
                "k = ctypes.WinDLL('kernel32', use_last_error=True)\n"
                "k.FreeConsole()\n"
                "ok = k.AttachConsole(int(sys.argv[1]))\n"
                "print('attached=%s err=%s' % (bool(ok), ctypes.get_last_error()))\n"
            )
            got = subprocess.run([sys.executable, "-c", probe, str(pid)], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace",
                                 timeout=30).stdout.strip()
            attached = "attached=True" in got
            check(not attached,
                  "★ 从外面 AttachConsole 到它：**没有控制台**（err=6 就是没有）",
                  got + "  ← 有关联控制台的话，关控制台就会杀掉它")
    finally:
        if status_path.exists() or pid:
            daemon.send_command(PROJ, "quit")
            wait_for(lambda: daemon.read_status(PROJ) is None, 20.0)
        if status_path.exists():
            status_path.unlink()
        if ctl_path.exists():
            ctl_path.unlink()
    after = wait_for(lambda: not W.instance_exists("translate-ime"), 15.0)
    check(bool(after), "quit 之后锁还回去了（下次双击才能起来）")

print()
print("=" * 74)
passed = sum(1 for ok, _ in results if ok)
print(f"  结果：{passed}/{len(results)} 通过" +
      (f"（其中 {skipped} 项因环境跳过）" if skipped else ""))
for ok, label in results:
    if not ok:
        print(f"    未通过：{label}")
print("=" * 74)
sys.exit(0 if passed == len(results) else 1)
