# -*- coding: utf-8 -*-
"""双击入口：``run.bat`` 本身有没有被真的执行过。

用户日常动作不是敲 ``python main.py``，而是**双击 ``run.bat``**。可在这一轮之前，
``tests/`` 里跟 ``run.bat`` 有关的断言只有一条：

    tests/test_single_instance.py:188-189  check("--background" in source_of("run.bat"), ...)

也就是说，全仓库 1360 项测试里，没有任何一项**真的把 run.bat 跑一遍**——只读了它的源码。
于是这几件事从来没被机器验证过：

1. cmd.exe 读 .bat 用的是控制台 OEM 码页（中文 Windows 上是 936）。文件里只要有一行
   UTF-8 中文，双击时就是乱码；如果那行在 ``( ... )`` 块里，cmd 甚至会把括号当命令拆掉，
   整个脚本直接崩。所以「run.bat 必须保持纯 ASCII」是一条只有跑起来才看得见的约束。
2. ``cd /d "%~dp0"``：项目路径里有中文（``D:\\dsh-work\\杂活\\translate-ime``），
   从别处（比如桌面）双击时工作目录不对，``main.py`` 就找不到 config.json 和 venv。
3. 无参数运行等于「把常驻实例拉起来，然后启动器自己退出」——用户关掉那个黑框，
   输入法还得在。这条承诺以前只能在 test_resident 里用 ``python main.py --background``
   近似，而不是真的走 bat。
4. ``--foreground`` 必须真的能压过 bat 里写死的 ``--background``。
5. 参数写错时（argparse 退出码 2），bat 会打印 ``[run.bat] exit code 2`` 再 ``pause``，
   好让人看见错误；``pause`` 不能把自动化/无人值守卡死。

直接跑：``python tests/test_launcher.py``
"""
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime import daemon  # noqa: E402
from t_ime import winapi as W  # noqa: E402
from t_ime.config import load as load_config  # noqa: E402
from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

OK = "[OK]  "
BAD = "[FAIL]"
results: list[tuple[bool, str]] = []

BAT = PROJ / "run.bat"
status_path = daemon.status_path(PROJ)
ctl_path = daemon.ctl_path(PROJ)
log_path = PROJ / "translate-ime.log"


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"  （{detail}）" if detail else ""))
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


def run_bat(args: list[str], cwd: Path | None = None, timeout: float = 120.0):
    """真的用 cmd 跑 run.bat（跟双击走的是同一个解释器）。"""
    proc = subprocess.run(
        ["cmd.exe", "/c", str(BAT)] + args,
        cwd=str(cwd or PROJ), capture_output=True, text=True,
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
        timeout=timeout,
    )
    return proc


def kill(pid: int) -> None:
    if not pid:
        return
    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                   capture_output=True, text=True, timeout=30)


# ============================================================
# 一、run.bat 自己：纯 ASCII、锚定目录、参数转交、失败可见
# ============================================================
print("=" * 74)
print("一、run.bat 本身")
print("=" * 74)

raw = BAT.read_bytes()
bad_bytes = [(i, b) for i, b in enumerate(raw) if b >= 0x80]
check(bool(raw), "run.bat 存在", f"{len(raw)} 字节")
check(not bad_bytes, "纯 ASCII（任何 >=0x80 的字节都会被 936 码页读成乱码）",
      "" if not bad_bytes else f"第一处在第 {bad_bytes[0][0]} 字节：{raw[max(0, bad_bytes[0][0] - 10):bad_bytes[0][0] + 10]!r}")

text = raw.decode("ascii", errors="replace")
check('cd /d "%~dp0"' in text, '锚定到脚本所在目录（cd /d "%~dp0"）')
check("%~dp0" in text, "用了 %~dp0（双击时 cwd 是 C:\\Windows\\System32，必须自己 cd）")
check("--background" in text, "默认传 --background（双击常驻、不挂控制台）")
check("%*" in text, "把用户参数原样转交（%*）")
check('"%PY%" main.py --background --panel %*' in text,
      "命令行是 python main.py --background --panel %*（参数在最后，用户传的还在后面）")
check("--no-panel" in text,
      "文档里说明了怎么不要那个窗口（--no-panel）")
check("pause" in text, "失败时 pause（双击时窗口不会一闪就没）")
check("exit code" in text, "失败时打印退出码")

# ============================================================
# 二、run.bat --check：只查环境，不启动输入法
# ============================================================
print()
print("=" * 74)
print("二、run.bat --check（真的执行 bat）")
print("=" * 74)

if W.instance_exists():
    print("已经有一个 translate-ime 在跑，跳过「启动实例」的部分（不打断你正在用的输入法）")
    live_ok = False
else:
    live_ok = True

status_path.unlink(missing_ok=True)
ctl_path.unlink(missing_ok=True)

t0 = time.time()
proc = run_bat(["--check"], timeout=180)
dt = time.time() - t0
out = proc.stdout or ""
err = proc.stderr or ""
check(proc.returncode == 0, "run.bat --check 退出码 0", f"exit={proc.returncode}")
check("全部通过" in out, "输出里是自检的结论句", out.strip().splitlines()[-1][:80] if out.strip() else "")
check(dt < 90, "自检很快（没有掉进常驻分支）", f"{dt:.1f}s")
check(not status_path.exists(), "--check 没有顺手启动常驻实例（不写状态文件）")
check(not W.instance_exists(), "跑完 --check 之后没有实例活着")
check("[app]" not in out, "--check 的输出里没有 [app] 日志（不是启动流程）", (err or "")[:80])

# 从别处（cwd 不是项目目录）敲同一个 bat：双击时就是这样
proc2 = run_bat(["--check"], cwd=Path("C:/"), timeout=180)
check(proc2.returncode == 0, "换一个工作目录（C:\\）跑也退出码 0",
      f"exit={proc2.returncode}" + (f" err={proc2.stderr.strip()[:80]}" if proc2.returncode else ""))
check("全部通过" in (proc2.stdout or ""),
      "换目录跑结果一样（cd /d %~dp0 真的生效了）")

# ============================================================
# 三、参数写错：退出码 2 + [run.bat] exit code 2（pause 不能卡死）
# ============================================================
print()
print("=" * 74)
print("三、run.bat 传错参数")
print("=" * 74)

t0 = time.time()
proc3 = run_bat(["--bogus-flag"], timeout=90)
dt3 = time.time() - t0
check(proc3.returncode == 2, "参数不认识时退出码 2（argparse 的错误码穿过 bat）",
      f"exit={proc3.returncode}")
check("exit code 2" in (proc3.stdout or ""), "bat 把退出码打出来给用户看",
      (proc3.stdout or "").strip()[-120:])
check(dt3 < 60, "stdin 关掉时 pause 不会把脚本卡住", f"{dt3:.1f}s")
check(not W.instance_exists(), "参数写错时没有启动实例")

# ============================================================
# 四、双击等价：run.bat（无参数）拉起的实例，黑框关掉还活着
# ============================================================
print()
print("=" * 74)
print("四、run.bat 无参数 = 双击（真的拉起常驻实例）")
print("=" * 74)

pid = 0
try:
    if not live_ok:
        print("（跳过：已经有实例在跑）")
    else:
        status_path.unlink(missing_ok=True)
        ctl_path.unlink(missing_ok=True)
        log_size0 = log_path.stat().st_size if log_path.exists() else 0

        t0 = time.time()
        # --no-panel：这里要验的是「实例被拉起来了没有」，不是「窗口弹没弹」。
        # 带上它，测试就不会在桌面上留下一堆真的设置窗口。
        proc4 = run_bat(["--no-panel"], timeout=120)
        dt4 = time.time() - t0
        out4 = proc4.stdout or ""
        check(proc4.returncode == 0, "run.bat 启动器退出码 0", f"exit={proc4.returncode}"
              + (f" err={(proc4.stderr or '').strip()[:200]}" if proc4.returncode else ""))
        check("已在后台启动" in out4 or "后台" in out4,
              "启动器明说「已在后台启动」", out4.strip()[:100])
        check(dt4 < 90, "启动器拉起实例后自己就退出了（没在前台死等）", f"{dt4:.1f}s")

        st = wait_for(lambda: daemon.read_status(PROJ), 40.0)
        check(bool(st), "常驻实例写出了状态文件", "")
        pid = int((st or {}).get("pid") or 0)
        check(pid > 0, "状态文件里有进程号", f"pid={pid}")
        name = W.process_name(pid) if pid else ""
        exe = Path(name).name.lower()
        pw = daemon.pythonw_path()
        check(exe == "pythonw.exe",
              "实例是 pythonw.exe（没有控制台窗口）", f"process={name}")
        # 注：venv 里的 pythonw.exe 是 Python 3.11+ 的 venvlauncher 存根，它会
        # 把参数交给基础解释器再起一个进程，所以进程路径落在基础安装里 —— 这是
        # 正常的，不是「没走 venv」。真要确认走的是 venv，看 sys.prefix 而不是路径。
        check(bool(pw) and Path(pw).exists(),
              "venv 里有 pythonw（后台常驻靠它，它就是上面那个进程的父进程）",
              f"venv pythonw={pw}")
        check(bool(pw) and exe == "pythonw.exe" and Path(name).stem.lower() == "pythonw",
              "常驻进程按无窗口方式起来（不是靠 console 的 python.exe 冒充）")
        check(bool(st and st.get("enabled")), "实例默认是启用状态")
        check(bool(st and st.get("hook") is True),
              "默认配置下键盘钩子装着（双击时不传 --no-hook）")
        check(log_path.exists() and log_path.stat().st_size > log_size0,
              "日志写进了项目目录下的 translate-ime.log")

        # 关键：启动器（那个黑框）已经退出了，实例还在 —— 「关掉黑框不影响它」
        check(W.process_name(pid) != "",
              "启动器退出之后实例仍然活着（关黑框杀不掉它）")

        shown = wait_for(
            lambda: ((daemon.read_status(PROJ) or {}).get("overlay") or {}).get("shows", 0) >= 1,
            20.0,
        )
        check(bool(shown), "候选窗真的画出来过（启动引导）",
              str(((daemon.read_status(PROJ) or {}).get("overlay") or {}).get("shows")))

        check(daemon.send_command(PROJ, "quit"), "能投出 quit")
        gone = wait_for(lambda: W.process_name(pid) == "", 15.0)
        check(bool(gone), "收到 quit 后退出", f"pid={pid}")
        check(not status_path.exists(), "退出时撤掉了状态文件")

        if log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace")
            check("收到命令：quit" in tail, "日志里能看到收到 quit")

    # ============================================================
    # 五、run.bat --foreground：真的压过 bat 里写死的 --background
    # ============================================================
    print()
    print("=" * 74)
    print("五、run.bat --foreground")
    print("=" * 74)

    if not live_ok:
        print("（跳过：已经有实例在跑）")
    else:
        status_path.unlink(missing_ok=True)
        ctl_path.unlink(missing_ok=True)
        fg = subprocess.Popen(
            ["cmd.exe", "/c", str(BAT), "--foreground", "--no-hook"],
            cwd=str(PROJ), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL,
        )
        fg_pid = 0
        try:
            st = wait_for(lambda: daemon.read_status(PROJ), 45.0)
            check(bool(st), "前台模式下实例也起来了")
            fg_pid = int((st or {}).get("pid") or 0)
            fg_name = Path(W.process_name(fg_pid)).name.lower() if fg_pid else ""
            check(fg_name == "python.exe",
                  "--foreground 跑的是带控制台的 python.exe（不是 pythonw）",
                  f"process={W.process_name(fg_pid)}")
            check(fg_pid > 0 and fg.poll() is None,
                  "--foreground 没有把自己再拉成一个后台子进程（父 cmd 还在等它）",
                  f"status.pid={fg_pid} cmd.pid={fg.pid}")
            check(bool(st and st.get("hook") is False), "--no-hook 生效")
            check(fg.poll() is None,
                  "前台模式没有自己退到后台（进程还活着、等于在等你 Ctrl+C）")
            check(daemon.send_command(PROJ, "quit"), "能投出 quit")
            try:
                rc = fg.wait(timeout=20)
            except subprocess.TimeoutExpired:
                rc = None
            check(rc == 0, "收到 quit 后前台进程退出码 0", f"rc={rc}")
        finally:
            if fg.poll() is None:
                fg.kill()
                try:
                    fg.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            if fg_pid and W.process_name(fg_pid) != "":
                print(f"...兜底清理：结束残留进程 {fg_pid}")
                kill(fg_pid)
            status_path.unlink(missing_ok=True)
            ctl_path.unlink(missing_ok=True)
finally:
    if pid and W.process_name(pid) != "":
        print(f"...兜底清理：结束残留进程 {pid}")
        kill(pid)
    status_path.unlink(missing_ok=True)
    ctl_path.unlink(missing_ok=True)

# ============================================================
# 七、文档里的命令必须写成 run.bat（第 118 轮）
# ============================================================
print()
print("=" * 74)
print("七、文档里的命令都用 run.bat（第 118 轮）")
print("=" * 74)
for name in ("README.md", "docs/使用流程.md"):
    doc = PROJ / name
    text = doc.read_text(encoding="utf-8")
    bad = [i + 1 for i, line in enumerate(text.split("\n"))
           if line.startswith("python main.py ")]
    check(not bad, f"{name} 里没有行首 `python main.py`（那会用系统 Python）",
          f"第 {bad[:5]} 行还在直接敲 python main.py")
    check("run.bat" in text, f"{name} 里给用户照抄的命令提到了 run.bat")
readme_text = (PROJ / "README.md").read_text(encoding="utf-8")
check("改完之后跑一次 **`run.bat --show-config`**" in readme_text,
      "README 的 --show-config 指引用 run.bat")
check("不要直接敲 `python main.py ...`" in readme_text,
      "README 明确警告过别直接敲 python main.py（解释器坑）")

# ============================================================
# 八、程序自己打印的建议也得用 run.bat（第 119 轮）
# ============================================================
# 第 118 轮只改了文档。可「诊断入口」这类建议是**程序运行时打印的**：
# 面板 / 体检 / 托盘警告 / --check 的出路提示，全都在教用户敲
# `python main.py --tray-check` —— 而用户照做命中的是系统 Python，
# 满屏「依赖没装」。这一节用 AST 把所有**字符串常量**扫一遍，
# 注释和 docstring 不算（前者不打印，后者是给读源码的人看的）。
print()
print("=" * 74)
print("八、程序打印的建议里不许出现 `python main.py`（第 119 轮）")
print("=" * 74)
import ast

ADVICE_FILES = ["main.py", "t_ime/app.py", "t_ime/config.py",
                "t_ime/daemon.py", "t_ime/webpanel.py"]


def _docstring_nodes(tree: ast.AST) -> set:
    """收集所有 docstring 节点的 id（模块 / 类 / 函数的第一条语句）。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


bad_advice = []
for rel in ADVICE_FILES:
    src = (PROJ / rel).read_text(encoding="utf-8")
    tree = ast.parse(src)
    docs = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docs:
                continue
            if "python main.py" in node.value:
                bad_advice.append(f"{rel}:{node.lineno}")
check(not bad_advice,
      "程序自己打印的字符串里没有 `python main.py`（那会让用户敲系统 Python）",
      f"这些地方还在教用户直接敲：{bad_advice[:6]}")
check("跑：run.bat --tray-check" in (PROJ / "t_ime/app.py").read_text(encoding="utf-8"),
      "托盘装不上时的警告改成了 run.bat --tray-check")
check("跑 run.bat --show-config 看" in (PROJ / "t_ime/config.py").read_text(encoding="utf-8"),
      "配置写错时的提示改成了 run.bat --show-config")
check("run.bat --hook-check     # 只查键盘钩子" in (PROJ / "main.py").read_text(encoding="utf-8"),
      "体检结尾的三条建议用的是 run.bat")

print()
print("=" * 74)
passed = sum(1 for ok, _ in results if ok)
print(f"结果：{passed}/{len(results)} 通过")
for ok, label in results:
    if not ok:
        print(f"  未通过：{label}")
print("=" * 74)
sys.exit(0 if passed == len(results) else 1)
