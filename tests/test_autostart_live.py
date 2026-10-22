# -*- coding: utf-8 -*-
"""开机自启：真的往注册表里写一条、再把它删掉（写不进去时，验它有没有说实话）。

这一轮之前，自启那条路**一次都没在真机上跑过**：``tests/test_autostart.py`` 的 56 项
里，注册表是用内存版 ``winreg`` 假装的（``sys.modules["winreg"]`` 换掉），覆盖面很好，
但它证明不了三件事：

1. 真的 ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` 写不写得进去
   （低完整性、组策略、安全软件都可能拦，而这条路径的报错原文是给用户看的）；
2. 写进去的那条**命令行字符串**长什么样：它会被 Windows 交给 ``CreateProcess``，
   项目路径里有中文（``D:\\dsh-work\\杂活\\translate-ime``），参数必须各自带引号；
3. ``--autostart`` / ``--no-autostart`` / ``--autostart-status`` 三个开关跑起来是什么样，
   以及「关掉」到底有没有真的把值删掉。

做法：先记下用户当前的注册表值（正常情况下没有），测完在 ``finally`` 里原样还原，
所以这个测试不会替你打开开机自启。

这个会话本身跑在低完整性级别下（``--tray-check`` 也是因此退出 2），注册表写入会被
UIPI 拦掉 —— 那正好是要验的另一半：**被拒绝的时候，程序有没有说人话、有没有假装成功**。
写不动的检查标 ``[SKIP]``，只验「拒绝时说了什么」。

直接跑：``python tests/test_autostart_live.py``
"""
import ctypes
import subprocess
import sys
import time
import winreg
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime import daemon  # noqa: E402
from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

OK = "[OK]  "
BAD = "[FAIL]"
SKIP = "[SKIP]"
results: list[tuple[bool, str]] = []
skipped: list[str] = []


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"  （{detail}）" if detail else ""))
    return cond


def skip(label, detail=""):
    skipped.append(label)
    print(f"{SKIP} {label}" + (f"  （{detail}）" if detail else ""))


def parse_cmdline(text: str) -> list[str]:
    """按 Windows 自己的规则把命令行拆回参数（CreateProcess 就是这么拆的）。"""
    shell32 = ctypes.windll.shell32
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    n = ctypes.c_int(0)
    argv = shell32.CommandLineToArgvW(text, ctypes.byref(n))
    out = [argv[i] for i in range(n.value)]
    ctypes.windll.kernel32.LocalFree(argv)
    return out


def reg_read():
    """读「自启」这一条现在是什么（没有就 (None, None)）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, daemon.AUTOSTART_KEY) as k:
            value, typ = winreg.QueryValueEx(k, daemon.AUTOSTART_NAME)
            return value, typ
    except FileNotFoundError:
        return None, None


def reg_write(value: str) -> None:
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, daemon.AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE
    ) as k:
        winreg.SetValueEx(k, daemon.AUTOSTART_NAME, 0, winreg.REG_SZ, value)


def reg_delete() -> None:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, daemon.AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            winreg.DeleteValue(k, daemon.AUTOSTART_NAME)
    except FileNotFoundError:
        pass
    except PermissionError:
        pass   # 低完整性下连删都删不动；没写成功也就没有要删的东西


def reg_names() -> list[str]:
    """Run 键下面的值名（要确认没有写第二条）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, daemon.AUTOSTART_KEY) as k:
            names = []
            i = 0
            while True:
                try:
                    names.append(winreg.EnumValue(k, i)[0])
                except OSError:
                    break
                i += 1
            return names
    except FileNotFoundError:
        return []


def run_main(args: list[str], timeout: float = 180.0):
    return subprocess.run(
        [sys.executable, str(PROJ / "main.py"), *args],
        cwd=str(PROJ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


print("=" * 74)
print("开机自启：真的动一次 HKCU 注册表，再把它还原")
print("=" * 74)

orig_value, orig_type = reg_read()
orig_script = daemon.startup_script_path().exists()

print()
print("[0] 先看清测之前是什么状态（这决定了最后怎么还原）")
check(True, f"现在的自启项：{orig_value!r}" if orig_value else "现在的自启项：没有（干净）")
check(not orig_script, "启动文件夹里没有 translate-ime.cmd",
      str(daemon.startup_script_path()))
check(daemon.autostart_status() == "none",
      "autostart_status() 说「没开启」", daemon.autostart_status())

if orig_value is not None or orig_script:
    print()
    print("开机自启现在是**开着**的（可能是你之前自己开的）。")
    print("这个测试会写注册表，为了不动你的设置，先跳过；想测的话先跑")
    print("  python main.py --no-autostart")
    print("然后重新运行本文件。")
    print("=" * 74)
    print(f"结果：0/{len(results)} 通过（已跳过：自启本来就开着）")
    print("=" * 74)
    sys.exit(0)

cmdline = daemon.autostart_command(PROJ)

print()
print("[1] 拼出来的那条命令行能不能被 CreateProcess 正确拆开")
check(bool(cmdline), "autostart_command() 拼得出命令行", (cmdline or "")[:100])
if cmdline:
    argv = parse_cmdline(cmdline)
    check(cmdline.startswith('"') and cmdline.endswith('"'),
          "整条命令以引号收尾（路径里有中文也不能裸奔）", cmdline[:40] + " …")
    check(cmdline.count('"') == 8, "四个参数各自带引号（8 个引号）",
          f'引号 {cmdline.count(chr(34))} 个')
    check(len(argv) == 4, "按 Windows 规则拆回 4 段", f"拆出 {len(argv)} 段：{argv}")
    if len(argv) == 4:
        py, main_py, flag1, flag2 = argv
        check(Path(py).name.lower() in ("pythonw.exe", "python.exe"),
              "第一段是解释器", Path(py).name)
        check(Path(py).exists(), "解释器文件真的存在", py)
        venv_py = PROJ / ".venv" / "Scripts" / "pythonw.exe"
        if venv_py.exists():
            check(str(venv_py).lower() in py.lower(),
                  "用的是项目 venv 里的 pythonw（不是系统那份）", py)
        else:
            skip("venv pythonw 归属检查", "这个项目没有 venv pythonw")
        check(Path(main_py) == (PROJ / "main.py") and Path(main_py).exists(),
              "第二段是本项目的 main.py", main_py)
        check(flag1 == "--background" and flag2 == "--detached",
              "带 --background 与 --detached（开机时不弹黑框、也不再自旋）",
              f"{flag1} {flag2}")
        # 用拆出来的 argv 真跑一次 --check：证明这条命令行交给 CreateProcess 之后
        # 解释器和项目路径都是对的（pythonw 没有控制台，只看退出码）。
        t0 = time.time()
        p = subprocess.run([py, main_py, "--check"], cwd=str(PROJ),
                           capture_output=True, timeout=120)
        check(p.returncode == 0, "拿拆出来的 argv 跑 --check 退出 0",
              f"exit {p.returncode}，{time.time() - t0:.1f} 秒")

print()
print("[1b] 注册表写不动时的兜底：启动文件夹里那个 .cmd 的内容")
# 这一步不需要写盘，纯函数：给 cmd.exe 读的文件必须按「本机代码页」编码，
# 且第一件事 chcp 成同一个代码页 —— 否则中文路径会被读成乱码，start 找不到 exe。
try:
    data, cp = daemon._startup_cmd_bytes(cmdline or "")
    text = data.decode(f"cp{cp}")
except Exception as exc:                                     # noqa: BLE001
    check(False, "_startup_cmd_bytes() 编得出字节", f"{type(exc).__name__}: {exc}")
else:
    check(True, f"_startup_cmd_bytes() 编得出字节（代码页 {cp}）")
    check(text.startswith("@echo off\r\n"), "第一行是 @echo off")
    check(f"chcp {cp} >nul" in text, "第二行把代码页设成编码时用的那个", f"chcp {cp}")
    check(f'start "" {cmdline}' in text,
          "start 那一行就是拼出来的命令行，原样没改", text.splitlines()[-1][:70])
    check(text.count("\n") == text.count("\r\n"), "换行全是 CRLF（cmd 才不挑）")
    head = text.split("start ", 1)[0]
    check(all(ord(c) < 128 for c in head),
          "start 之前的注释全是 ASCII（汉字注释对 cmd 只是风险）")
    check("rem Generated by translate-ime --autostart." in text
          and "rem Delete this file to disable autostart." in text,
          "文件里写了「删掉这个文件就是关自启」的说明")

print()
print("[2] 真的写进注册表（写不动时要验的是「有没有说实话」）")
ok, detail = None, ""
try:
    ok, detail = daemon.set_autostart(True, PROJ)
except Exception as exc:                                    # noqa: BLE001
    ok, detail = False, f"{type(exc).__name__}: {exc}"

denied = (not ok) and ("拒绝访问" in detail or "拒绝访问" in str(detail))
if ok:
    check(ok, "set_autostart(True) 成功", detail)
    check("注册表" in detail, "它走的是注册表这条路（不是启动文件夹兜底）", detail)

    value, typ = reg_read()
    check(value == cmdline, "注册表里的值就是拼出来的那条命令行",
          (value or "")[:80] + " …")
    check(typ == winreg.REG_SZ, "值类型是 REG_SZ（不是二进制/多字符串）", str(typ))
    check(daemon.autostart_status() == "registry", "autostart_status() 认出来了",
          daemon.autostart_status())
    check(not daemon.startup_script_path().exists(),
          "注册表成功时不往启动文件夹落 .cmd", str(daemon.startup_script_path()))
    names = reg_names()
    check(names.count(daemon.AUTOSTART_NAME) == 1,
          "Run 键里叫 translate-ime 的值只有一条",
          f"{names.count(daemon.AUTOSTART_NAME)} 条")

    print()
    print("[3] 重复开启不会写成两条、也不会把命令行改掉")
    ok2, detail2 = daemon.set_autostart(True, PROJ)
    check(ok2, "第二次 set_autostart(True) 仍然成功", detail2)
    value2, _ = reg_read()
    check(value2 == cmdline, "值没被改写", (value2 or "")[:60] + " …")
    check(reg_names().count(daemon.AUTOSTART_NAME) == 1, "仍然只有一条")
    check(daemon.autostart_status() == "registry", "状态仍是 registry")

    print()
    print("[4] 三个命令行开关（用户实际敲的就是这些）")
    p = run_main(["--autostart-status"])
    check(p.returncode == 0, "--autostart-status 退出 0", f"exit {p.returncode}")
    check("已开启（注册表）" in p.stdout, "它说「已开启（注册表）」", p.stdout.strip())
    p = run_main(["--autostart"])
    check(p.returncode == 0, "--autostart 退出 0", f"exit {p.returncode}")
    check("已开启" in p.stdout, "--autostart 说「已开启」", p.stdout.strip())
    value3, _ = reg_read()
    check(value3 == cmdline, "CLI 写入的值和 API 一致", (value3 or "")[:60] + " …")
    p = run_main(["--no-autostart"])
    check(p.returncode == 0, "--no-autostart 退出 0", f"exit {p.returncode}")
    check("已关闭" in p.stdout, "--no-autostart 说「已关闭」", p.stdout.strip())
    value4, _ = reg_read()
    check(value4 is None, "注册表里的值真的删掉了", repr(value4))
    p = run_main(["--autostart-status"])
    check(p.returncode == 0, "关掉后 --autostart-status 仍退出 0", f"exit {p.returncode}")
    check("未开启" in p.stdout, "关掉后它说「未开启」", p.stdout.strip())

    print()
    print("[5] 关掉之后没有残留")
    check(daemon.autostart_status() == "none", "autostart_status() 回到 none",
          daemon.autostart_status())
    check(not daemon.startup_script_path().exists(), "启动文件夹里也没有 .cmd")
    check(daemon.AUTOSTART_NAME not in reg_names(), "Run 键里没有 translate-ime 了")
    ok5, detail5 = daemon.set_autostart(False, PROJ)
    check(ok5 and "本来" in detail5, "已经关着的时候再关一次也不报错", detail5)
else:
    # 写不进去（本会话是低完整性级别）：此时唯一该验的是「拒绝有没有被说清楚」
    check(denied, "写入被拒绝（这是低完整性级别下的正常结果）", str(detail)[:120])
    check("完整性" in str(detail) or "拒绝访问" in str(detail),
          "拒绝的理由说得出来（不是一句「失败」）", str(detail)[:120])
    check("注册表" in str(detail) and "启动文件夹" in str(detail),
          "两条路（注册表 / 启动文件夹）各自的原因都报了", str(detail)[:80] + " …")
    check("终端" in str(detail) or "沙箱" in str(detail),
          "它告诉用户该换个环境再试（正常终端 / 双击 run.bat）", str(detail)[-60:])
    p = run_main(["--autostart"])
    check(p.returncode == 1, "--autostart 在写不动时退出 1（绝不假装成功）",
          f"exit {p.returncode}")
    out = (p.stdout or "") + (p.stderr or "")
    check("设置失败" in out, "它明说「开机自启设置失败」", out.strip()[-80:])
    check("拒绝访问" in out, "而且带上真实原因", out.strip()[-60:])
    value, _ = reg_read()
    check(value is None, "失败之后注册表里没有被写进半条值", repr(value))
    check(not daemon.startup_script_path().exists(),
          "失败之后启动文件夹里也没有残留 .cmd", str(daemon.startup_script_path()))
    p = run_main(["--autostart-status"])
    check(p.returncode == 0 and "未开启" in p.stdout,
          "--autostart-status 仍然稳定地说「未开启」", p.stdout.strip()[-30:])
    check("Traceback" not in out, "失败输出里没有 Python traceback（只有人话）",
          out.strip().splitlines()[0][:60] if out.strip() else "")
    p = run_main(["--no-autostart"])
    off = (p.stdout or "") + (p.stderr or "")
    check(p.returncode == 0, "没开启的时候 --no-autostart 退出 0（关不该失败）",
          f"exit {p.returncode}")
    check("本来" in off, "它说「本来就没有开启」", off.strip()[-40:])
    for label in ("注册表里的值就是拼出来的那条命令行",
                  "值类型是 REG_SZ（不是二进制/多字符串）",
                  "autostart_status() 认出来了",
                  "Run 键里叫 translate-ime 的值只有一条",
                  "第二次 set_autostart(True) 仍然成功",
                  "值没被改写", "仍然只有一条", "状态仍是 registry",
                  "它说「已开启（注册表）」", "--autostart 退出 0",
                  "--autostart 说「已开启」", "CLI 写入的值和 API 一致",
                  "--no-autostart 说「已关闭」", "注册表里的值真的删掉了",
                  "关掉后它说「未开启」", "autostart_status() 回到 none",
                  "Run 键里没有 translate-ime 了"):
        skip(label, "本会话低完整性级别，注册表写不动")

print()
print("[6] 还原")
value6, _ = reg_read()
check(value6 == orig_value, "测完之后注册表与测之前一致",
      "无自启项" if orig_value is None else "已恢复原值")
check(daemon.startup_script_path().exists() == orig_script,
      "启动文件夹状态与测之前一致")

print()
print("=" * 74)
passed = sum(1 for ok_, _ in results if ok_)
tail = f"（另有 {len(skipped)} 项因低完整性级别跳过）" if skipped else ""
print(f"结果：{passed}/{len(results)} 通过{tail}")
for ok_, label in results:
    if not ok_:
        print(f"  未通过：{label}")
print("=" * 74)
sys.exit(0 if passed == len(results) else 1)
