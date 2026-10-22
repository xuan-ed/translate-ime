# -*- coding: utf-8 -*-
"""托盘图标装不上时，程序还能不能正常用？

托盘图标在**低完整性进程**里必然装不上：``Shell_NotifyIconW(NIM_ADD)`` 是发给
explorer 的跨进程消息，UIPI 直接拒掉（``GetLastError=5``，读作 ACCESS_DENIED）。
这不是罕见情况 —— 从高权限终端、从服务、从还没起来的 explorer 里启动都会撞上，
而且本机（开发会话本身是 Low 完整性）就是现成的例子。

所以「没有托盘」必须有一条**站得住的退路**，这个文件盯四件事：

  1. 坏消息要说出口，而且**说的退出键必须来自配置** —— 用户把 ``hotkeys.quit``
     改成别的组合键之后，提示里再写死 ``Ctrl+Alt+Q`` 就成了假话（这条是真修过的）；
  2. 第二次启动（双击 ``run.bat`` / ``main.py``）要变成控制面板，并且**明说**
     这次没有鼠标入口；
  3. 面板的交互菜单里，三件事（挂起/恢复、换语言、退出）一个都不能少 ——
     它们和托盘共用同一批 item_id、同一个处理器；
  4. 源码里不许再出现写死的 ``Ctrl+Alt+Q``。

用法：``python tests/test_no_tray_fallback.py``（有实例在跑时跳过碰状态文件的那两节）
"""
from __future__ import annotations

import contextlib
import copy
import io
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

import main as main_mod  # noqa: E402
import t_ime.tray as tray_mod  # noqa: E402
from t_ime import daemon  # noqa: E402
from t_ime import winapi as W  # noqa: E402
from t_ime.app import MENU_IDS, App  # noqa: E402
from t_ime.config import load as load_config  # noqa: E402
from t_ime.console import fix_console_encoding  # noqa: E402
from t_ime.overlay import _pretty_hotkey  # noqa: E402

fix_console_encoding()

results: list[tuple[bool, str]] = []
skipped = 0


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  —— {detail}" if detail else ""))
    return bool(ok)


def skip(label: str, why: str) -> None:
    global skipped
    results.append((True, label))
    skipped += 1
    print(f"  [SKIP] {label}  —— {why}")


class FakeTray:
    """只记账的托盘：不碰 Shell_NotifyIconW，所以低完整性环境里也能测。"""

    tray_added = True

    def __init__(self, *args, **kwargs):
        self.added: list[tuple] = []
        self.sets: list[tuple] = []
        self.tooltips: list[str] = []
        self.started = False
        self.last_error = None

    def add_item(self, item_id, label, checked=False, enabled=True,
                 separator_before=False, default=False):
        self.added.append((item_id, label, separator_before))
        return True

    def set_item(self, item_id, checked=None, enabled=None, label=None):
        self.sets.append((item_id, checked, enabled, label))

    def set_tooltip(self, text):
        self.tooltips.append(text)

    def start(self):
        self.started = True

    def stop(self):
        pass


class FakeStdin:
    """假的标准输入：能伪装成终端，并在**每次被读之前**给命令文件拍个快照。

    这样就能看见「菜单里选了 1）挂起」到底往命令文件里写了什么 ——
    真机上那个文件会被常驻实例吃掉；测试里没有实例，它会一条条攒在那里
    （``daemon.send_command`` 是**追加**写，不是覆盖）。
    """

    def __init__(self, lines, tty=True, on_read=None):
        self.lines = list(lines)
        self.tty = tty
        self.on_read = on_read
        self.snapshots: list[str] = []

    def isatty(self):
        return self.tty

    def readline(self):
        if self.on_read:
            self.snapshots.append(self.on_read())
        if self.lines:
            return self.lines.pop(0)
        raise EOFError


def ctl_lines() -> list[str]:
    p = daemon.ctl_path(PROJ)
    try:
        return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []


def ctl_text() -> str:
    return "\n".join(ctl_lines())


def fake_status(tray: bool, tr_info: dict | None = None) -> dict:
    st = {
        "pid": 4242, "enabled": True, "direct": False, "active": "zh",
        "tray": tray, "caps": False, "log": str(PROJ / "translate-ime.log"),
        "tick_errors": 0, "commit_failures": 0,
        "overlay": {"shows": 3, "visible": False, "on_monitor": True,
                    "rect": [0, 0, 780, 220], "error": None},
        "hook_info": {"installed": True},
    }
    if tr_info is None:
        tr_info = {"mock": True, "provider": "mock", "variants": 4, "requests": 5,
                   "cache_hits": 2, "cache_size": 3, "errors": 0, "last_error": None}
    if tr_info:
        st["translate"] = tr_info
    return st


def panel_run(stdin, cfg) -> tuple[int, str]:
    """跑一遍控制面板（替换 sys.stdin），返回 (退出码, 打印出来的东西)。"""
    old = sys.stdin
    sys.stdin = stdin
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = main_mod.run_control_panel(cfg, PROJ, W)
    finally:
        sys.stdin = old
    return rc, buf.getvalue()


print("=" * 74)
print("  托盘装不上时的兜底路径")
print("=" * 74)

cfg, cfg_path = load_config()
cfg_custom = copy.deepcopy(cfg)
cfg_custom["hotkeys"]["quit"] = "ctrl+shift+f12"
# 挂起键也换掉 —— 挂起气泡里那句「按 X 恢复」同样是文案随配置走的一部分。
cfg_custom["hotkeys"]["toggle_enabled"] = "ctrl+shift+f11"
pretty_default = _pretty_hotkey(cfg["hotkeys"]["quit"])
pretty_custom = _pretty_hotkey(cfg_custom["hotkeys"]["quit"])
pretty_toggle_custom = _pretty_hotkey(cfg_custom["hotkeys"]["toggle_enabled"])

# ---------------------------------------------------------------------- #
print("\n-- 1. 提示里的退出键来自配置，不是写死的 --")

check(pretty_default == "Ctrl+Alt+Q", "默认配置拼出来就是 Ctrl+Alt+Q", pretty_default)
check(pretty_custom == "Ctrl+Shift+F12", "改过配置之后拼出来是 Ctrl+Shift+F12", pretty_custom)

app = App(cfg_custom, PROJ, verbose=False, install_hook=False)
# 关掉 Tk 窗口（免得抢焦点），但保留引用 —— 让 Overlay 连同 Tk root 一起被 GC
# 回收，进程退出时会 Tcl_AsyncDelete 直接 abort。
app.overlay.destroy()

made: list[FakeTray] = []
real_tray_cls = tray_mod.TrayIcon


def fake_tray_cls(*args, **kwargs):
    t = FakeTray(*args, **kwargs)
    t.tray_added = False
    t.last_error = "Shell_NotifyIconW(NIM_ADD) 失败，GetLastError=5"
    made.append(t)
    return t


app.tray = None
app._warnings.clear()
err = io.StringIO()
out = io.StringIO()
try:
    tray_mod.TrayIcon = fake_tray_cls
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        app._start_tray()
finally:
    tray_mod.TrayIcon = real_tray_cls

err_txt = err.getvalue()
warn = "\n".join(app._warnings)
check("托盘图标没能装上" in err_txt, "stderr 说了托盘没能装上", err_txt.strip()[:60])
check(pretty_custom in err_txt, "stderr 给的是**配置里的**组合键", pretty_custom)
check(pretty_default not in err_txt, "stderr 里没有写死的默认组合键")
check(pretty_custom in warn, "候选窗那条提醒也用了配置里的组合键")
check("不影响打字" in warn, "提醒里说了「不影响打字」")
check("tray-check" in warn, "提醒里给了诊断入口")
check(len(app._warnings) >= 1, "提醒挂在候选窗上（用户看得见，不是只写日志）",
      f"{len(app._warnings)} 条")
check(made and made[0].started, "托盘线程照样起来了（装不上不等于不跑）")

# 源码扫描放在第 5 节做（要跳过注释与 docstring，见那边的说明）。

# ---------------------------------------------------------------------- #
print("\n-- 5. 别处的提示也随配置走（注释和 docstring 不算数）--")


def code_only(path: Path) -> str:
    """去掉注释行与 docstring 之后的源码。

    为什么要这么麻烦：注释和 docstring **本来就该**举 ``Ctrl+Alt+Q`` 当例子
    （说明「这里为什么不能写死」），把它们一起算进来，测试就永远不可能绿。
    但字符串字面量要留着 —— 写死在 ``print("按 Ctrl+Alt+Q")`` 里的那种提示
    恰恰是这条测试要抓的东西。
    """
    import ast

    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    spans: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                first = body[0]
                spans.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    keep: list[str] = []
    for i, line in enumerate(src.splitlines(), start=1):
        s = line.strip()
        if i in spans or not s or s.startswith("#"):
            continue
        keep.append(line)
    return "\n".join(keep)


FILES = ("t_ime/app.py", "main.py", "t_ime/daemon.py", "t_ime/core.py")
for rel in FILES:
    code = code_only(PROJ / rel)
    check("Ctrl+Alt+Q" not in code, f"{rel} 的可执行代码里没有写死的 'Ctrl+Alt+Q'")
    check("Ctrl+Alt+Z" not in code, f"{rel} 的可执行代码里没有写死的 'Ctrl+Alt+Z'")

code_main = code_only(PROJ / "main.py")
check("_hotkey_epilog" in code_main and "epilog=_hotkey_epilog()" in code_main,
      "main.py 的 --help 快捷键说明是现算的（_hotkey_epilog）")
code_daemon = code_only(PROJ / "t_ime/daemon.py")
check("quit_hotkey_text" in code_daemon and "def quit_hotkey_text" in code_daemon,
      "daemon.py 的退出提示走 quit_hotkey_text()")

# 功能上验一遍：把 config.load 换掉，看这几处提示是不是跟着走。
import t_ime.config as config_mod  # noqa: E402

real_load = config_mod.load
try:
    config_mod.load = lambda *a, **k: (cfg_custom, cfg_path)  # type: ignore[assignment]
    epilog = main_mod._hotkey_epilog()
    dk = daemon.quit_hotkey_text(PROJ)
finally:
    config_mod.load = real_load  # type: ignore[assignment]
check(pretty_custom in epilog, "--help 的快捷键表跟着 config.json 走", epilog)
check("Ctrl+Alt+Q" not in epilog, "--help 里没有默认那份写死的组合键")
check(dk == pretty_custom, "daemon 的退出提示跟着 config.json 走", dk or "(空)")
_missing = daemon.quit_hotkey_text(PROJ, ["--config", str(PROJ / "不存在的配置.json")])
check(_missing == pretty_default,
      "--config 指到不存在的文件时按默认配置报默认键（那次运行真会用默认值，所以不是谎话）",
      _missing or "(空)")

# ---------------------------------------------------------------------- #
print("\n-- 6. 挂起 / 直通的气泡也随配置走 --")
from t_ime.core import IMECore  # noqa: E402

c = IMECore(cfg_custom, None, None, verbose=False)
c.enabled = False
for _ in range(3):
    c._nudge_passthrough()
check(pretty_toggle_custom in c.toast, "挂起气泡里是配置里的挂起键", c.toast)
check("Ctrl+Alt+Z" not in c.toast, "挂起气泡里没有写死的默认挂起键")

c2 = IMECore(cfg_custom, None, None, verbose=False)
c2.enabled = True
for _ in range(3):
    c2._nudge_passthrough()
check("英文直通中" in c2.toast, "直通气泡还是那个前缀（老测试只查这个）", c2.toast)
check("点一下 Shift" in c2.toast, "直通气泡说了怎么切回去", c2.toast)

# ---------------------------------------------------------------------- #
print("\n-- 2. 第二次启动 = 控制面板，并且明说没有鼠标入口 --")
print("\n-- 3. 面板的交互菜单：三件事一个都不能少 --")

busy = W.instance_exists("translate-ime")
if busy:
    for label in ("非交互面板的退出码是 3（main.py 的约定）",
                  "先报「已经在跑了」",
                  "面板里那行明说托盘没装上",
                  "非交互分支打印的是配置里的退出键",
                  "打印里没有写死的默认组合键",
                  "补了一句「连鼠标入口都没有」",
                  "托盘装上时退出码同样是 3",
                  "托盘那行说「已装上」",
                  "面板里有「开机自启」那行",
                  "这时不说「连鼠标入口都没有」",
                  "面板里有「翻译」那行（以前一个字都没提翻译层）",
                  "没配 API key 时面板说用的是离线词表",
                  "面板把请求次数与缓存命中摆出来了",
                  "配了 key 时面板点名是在线 deepseek",
                  "失败过就报次数 + 最近那次的原因",
                  "旧实例没报翻译状态时说清楚而不是崩",
                  "交互菜单选择完返回 0",
                  "菜单里有「挂起 / 恢复输入法」",
                  "菜单里有「换默认上屏语言」",
                  "菜单里有「退出 translate-ime」",
                  "第一次要输入时还没发过命令",
                  "选「1）挂起」之后命令文件里是 toggle_enabled",
                  "最后命令文件里是 quit（退出发出去了）",
                  "面板报了「已退出」"):
        skip(label, "有实例在跑，先不碰状态文件/命令文件")
else:
    status_p = daemon.status_path(PROJ)
    ctl_p = daemon.ctl_path(PROJ)
    backup: dict[Path, bytes | None] = {}
    for p in (status_p, ctl_p):
        try:
            backup[p] = p.read_bytes()
        except OSError:
            backup[p] = None
    try:
        # --- 2a. 没有托盘时的非交互面板 ---
        daemon.write_status(PROJ, fake_status(tray=False))
        ctl_p.unlink(missing_ok=True)
        rc, txt = panel_run(FakeStdin([], tty=False), cfg_custom)
        check(rc == 3, "非交互面板的退出码是 3（main.py 的约定）", f"exit={rc}")
        check("已经在跑了" in txt, "先报「已经在跑了」")
        check("托盘图标" in txt and "没装上" in txt, "面板里那行明说托盘没装上")
        check(pretty_custom in txt, "非交互分支打印的是配置里的退出键", pretty_custom)
        check(pretty_default not in txt, "打印里没有写死的默认组合键")
        check("连鼠标入口都没有" in txt, "补了一句「连鼠标入口都没有」")

        # --- 2b. 托盘装上了就别多嘴 ---
        daemon.write_status(PROJ, fake_status(tray=True))
        rc2, txt2 = panel_run(FakeStdin([], tty=False), cfg_custom)
        check(rc2 == 3, "托盘装上时退出码同样是 3", f"exit={rc2}")
        check("已装上" in txt2, "托盘那行说「已装上」")
        check("开机自启" in txt2, "面板里有「开机自启」那行",
              [ln for ln in txt2.splitlines() if "开机自启" in ln][:1])
        check("连鼠标入口都没有" not in txt2, "这时**不**说「连鼠标入口都没有」")

        # --- 2c. 翻译层状态：配了 key 却不出译文时，面板得能说清是谁的锅 ---
        tr_line = [ln for ln in txt2.splitlines() if "翻译" in ln][:1]
        check(bool(tr_line), "面板里有「翻译」那行（以前一个字都没提翻译层）", tr_line)
        check("离线词表" in txt2, "没配 API key 时面板说用的是离线词表", tr_line)
        check("请求 5 次" in txt2 and "命中缓存 2 次" in txt2,
              "面板把请求次数与缓存命中摆出来了", tr_line)

        daemon.write_status(PROJ, fake_status(tray=True, tr_info={
            "mock": False, "provider": "deepseek", "variants": 4, "requests": 9,
            "cache_hits": 4, "cache_size": 7, "errors": 2,
            "last_error": 'HTTP 401 Unauthorized {"error":"bad key"}'}))
        _rc_a, txt_a = panel_run(FakeStdin([], tty=False), cfg_custom)
        tr_a = [ln for ln in txt_a.splitlines() if "翻译" in ln][:1]
        check("在线 deepseek" in txt_a, "配了 key 时面板点名是在线 deepseek", tr_a)
        check("失败 2 次" in txt_a and "HTTP 401" in txt_a,
              "失败过就报次数 + 最近那次的原因", tr_a)

        daemon.write_status(PROJ, fake_status(tray=True, tr_info={}))
        _rc_b, txt_b = panel_run(FakeStdin([], tty=False), cfg_custom)
        tr_b = [ln for ln in txt_b.splitlines() if "翻译" in ln][:1]
        check("没报翻译状态" in txt_b, "旧实例没报翻译状态时说清楚而不是崩", tr_b)

        # --- 3. 交互菜单（用户双击时真的看到的那个） ---
        daemon.write_status(PROJ, fake_status(tray=False))
        ctl_p.unlink(missing_ok=True)
        fake_in = FakeStdin(["1\n", "7\n"], tty=True, on_read=ctl_text)
        rc3, txt3 = panel_run(fake_in, cfg_custom)
        check(rc3 == 0, "交互菜单选择完返回 0", f"exit={rc3}")
        check("挂起 / 恢复输入法" in txt3, "菜单里有「挂起 / 恢复输入法」")
        check("换默认上屏语言" in txt3, "菜单里有「换默认上屏语言」")
        check("退出 translate-ime" in txt3, "菜单里有「退出 translate-ime」")
        check(bool(fake_in.snapshots) and fake_in.snapshots[0] == "",
              "第一次要输入时还没发过命令", repr(fake_in.snapshots[:1]))
        check(len(fake_in.snapshots) >= 2 and fake_in.snapshots[1] == "toggle_enabled",
              "★ 选「1）挂起」之后命令文件里是 toggle_enabled",
              repr(fake_in.snapshots[1:2]))
        lines = ctl_lines()
        check(bool(lines) and lines[-1] == "quit" and "toggle_enabled" in lines,
              "★ 命令文件最后一条是 quit（退出发出去了）", repr(lines))
        check("已退出" in txt3, "面板报了「已退出」")
    finally:
        for p, data in backup.items():
            try:
                if data is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_bytes(data)
            except OSError:
                pass

# ---------------------------------------------------------------------- #
print("\n-- 4. 没有托盘也要能做的三件事，面板上都得有 --")

check("toggle_enabled" in main_mod.PANEL_COMMANDS, "面板里有「挂起 / 恢复」")
check("toggle_default" in main_mod.PANEL_COMMANDS, "面板里有「换语言」")
check("quit" in main_mod.PANEL_COMMANDS and main_mod.PANEL_COMMANDS[-1] == "quit",
      "面板里有「退出」（最后一项，符合习惯）")
check({"toggle_enabled", "toggle_default", "quit"} <= set(MENU_IDS),
      "这三条也在托盘菜单里 → 一个处理器就够")

# ---------------------------------------------------------------------- #
passed = sum(1 for ok, _ in results if ok)
total = len(results)
print()
if skipped:
    print(f"（其中 {skipped} 项因环境跳过）")
print(f"结果：{passed}/{total} 通过")
sys.exit(0 if passed == total else 1)
