# -*- coding: utf-8 -*-
"""托盘菜单与控制面板的「命令契约」。

为什么值得单测：托盘图标在低完整性进程里装不上（UIPI 拦 `Shell_NotifyIconW`），
所以托盘菜单这些 item_id 实际上很少被真的点过；而**它们和处理器一旦不同步，
症状是「菜单项点了没反应」或者更糟「点了以后程序哑掉」** —— 而且不会有人发现，
因为菜单本身画得出来、图标也在。控制面板（第二次双击 `run.bat`）走的是同一批
item_id，所以两边共用一张表、共用一个处理器，谁也不许自己加动作。

这里用假托盘（不碰 `Shell_NotifyIconW`），所以**能在任何环境下跑**，不依赖 shell。

直接跑：``python tests/test_tray_menu.py``
"""
import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

import main as main_mod  # noqa: E402
from t_ime.app import CORE_ACTIONS, MENU_IDS, MENU_ITEMS, App  # noqa: E402
from t_ime.config import load  # noqa: E402

PASS = 0
FAIL = 0
PROBLEMS: list[str] = []


def check(label: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"[PASS] {label}" + (f"  —— {extra}" if extra else ""))
    else:
        FAIL += 1
        PROBLEMS.append(label)
        print(f"[FAIL] {label}" + (f"  —— {extra}" if extra else ""))


def head(n: int, title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {n}. {title}")
    print("=" * 72)


cfg, _ = load()

# ---------------------------------------------------------------------- #
head(1, "菜单表本身")

check("菜单项非空", len(MENU_ITEMS) > 0, f"{len(MENU_ITEMS)} 项")
check("item_id 不重复", len(set(MENU_IDS)) == len(MENU_IDS), str(MENU_IDS))
check("每项都有标签", all(label.strip() for _, label, _ in MENU_ITEMS))
check("MENU_IDS 与 MENU_ITEMS 顺序一致",
      MENU_IDS == tuple(i for i, _, _ in MENU_ITEMS))

# 这三个是「开关」，交给状态机；其余是 app 自己处理的命令。
check("CORE_ACTIONS 都是菜单里真有的项",
      all(a in MENU_IDS for a in CORE_ACTIONS), str(CORE_ACTIONS))
check("托盘必须有退出入口", "quit" in MENU_IDS)
check("托盘必须有求助入口", "help" in MENU_IDS)
check("托盘必须有日志 / 配置入口",
      "open_log" in MENU_IDS and "open_config" in MENU_IDS)
check("托盘必须有「当前状态」入口", "status" in MENU_IDS)
check("退出项排在最后（右键菜单的习惯）", MENU_IDS[-1] == "quit")

# ---------------------------------------------------------------------- #
head(2, "控制面板与托盘共用同一批命令")

check("控制面板的命令都在托盘菜单里（所以一个处理器就够）",
      all(c in MENU_IDS for c in main_mod.PANEL_COMMANDS), str(main_mod.PANEL_COMMANDS))
check("控制面板的命令不重复",
      len(set(main_mod.PANEL_COMMANDS)) == len(main_mod.PANEL_COMMANDS))
check("控制面板也保留了退出入口", main_mod.PANEL_COMMANDS[-1] == "quit")
check("PANEL_MENU 与 PANEL_COMMANDS 一一对应",
      main_mod.PANEL_COMMANDS == tuple(c for c, _ in main_mod.PANEL_MENU))
check("控制面板的标签都非空",
      all(label.strip() for _, label in main_mod.PANEL_MENU))


# ---------------------------------------------------------------------- #
class FakeTray:
    """只记账的托盘：不碰 Shell_NotifyIconW，所以低完整性环境里也能测。"""

    tray_added = True

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
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


head(3, "App 按菜单表建托盘项")

import t_ime.tray as tray_mod  # noqa: E402

made: list[FakeTray] = []
real_tray_cls = tray_mod.TrayIcon


def fake_tray_cls(*args, **kwargs):
    t = FakeTray(*args, **kwargs)
    made.append(t)
    return t


app = App(cfg, ROOT, verbose=False, install_hook=False)
# 关掉这个测试里的 Tk 窗口（免得抢焦点），但**保留引用** —— 把 Overlay 对象
# 连同它的 Tk root 一起让 GC 回收，进程退出时会 `Tcl_AsyncDelete` 直接 abort。
app.overlay.destroy()
try:
    tray_mod.TrayIcon = fake_tray_cls
    app._start_tray()
finally:
    tray_mod.TrayIcon = real_tray_cls

check("App 真的建了一个托盘", len(made) == 1)
fake = made[0] if made else FakeTray()
check("托盘项的顺序和标签与菜单表逐项一致",
      [a[0] for a in fake.added] == list(MENU_IDS)
      and [a[1] for a in fake.added] == [lbl for _, lbl, _ in MENU_ITEMS],
      str([a[0] for a in fake.added]))
check("分隔线标记也跟着菜单表走",
      [a[2] for a in fake.added] == [sep for _, _, sep in MENU_ITEMS])
check("托盘线程被启动了", fake.started)

# ---------------------------------------------------------------------- #
head(4, "托盘装不上时要让用户看得见（这是本机唯一无法验证的路径）")

made.clear()
app.tray = None
app._warnings.clear()
fake2 = FakeTray()
fake2.tray_added = False
fake2.last_error = "Shell_NotifyIconW(NIM_ADD) 失败，GetLastError=5"
tray_mod.TrayIcon = lambda *a, **k: fake2
try:
    app._start_tray()
finally:
    tray_mod.TrayIcon = real_tray_cls

check("NIM_ADD 失败时会往候选窗上挂一条提醒",
      any("托盘图标没能装上" in w for w in app._warnings), str(app._warnings))
check("那条提醒说清了「不影响打字」并且给了退路",
      any("不影响打字" in w and "tray-check" in w for w in app._warnings))
check("还把 ERROR_ACCESS_DENIED 翻译成了人话",
      any("shell 拒绝了" in w for w in app._warnings), str(app._warnings[-1:]))

# ---------------------------------------------------------------------- #
head(5, "每个菜单项都得有人处理（含副作用）")

app.core.enabled = True
app.core.direct = False
app.core.quit_requested = False
app._warnings.clear()

opened: list[Path] = []
real_open = app._open_path
app._open_path = lambda p: opened.append(Path(p))

out = io.StringIO()
err = io.StringIO()
effects: dict[str, str] = {}
try:
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        for item_id in MENU_IDS + ("__activate__",):
            app.core.toast = ""
            before = (app.core.enabled, app.core.direct, app.core.active,
                      app.core.quit_requested, len(opened), len(app._warnings))
            app._handle_tray_action(item_id)
            after = (app.core.enabled, app.core.direct, app.core.active,
                     app.core.quit_requested, len(opened), len(app._warnings))
            effects[item_id] = "变了" if before != after else "没变"
            app.core.quit_requested = False
            if item_id in ("status", "__activate__"):
                effects[item_id] = "弹提示" if app.core.toast else "没弹提示"
            if item_id == "help":
                effects[item_id] = "弹帮助" if app.core.toast else "没弹帮助"
            if item_id in ("open_log", "open_config"):
                app.core.quit_requested = False
finally:
    app._open_path = real_open
    logged = out.getvalue() + err.getvalue()

check("没有哪个菜单项落到「不认识」那条兜底上",
      "不认识这个菜单动作" not in logged, logged.strip()[:200])
check("也没有把未知动作塞给状态机（『未知热键动作』）",
      "未知热键动作" not in logged, logged.strip()[:200])

for item_id in CORE_ACTIONS:
    check(f"「{item_id}」真的动了状态机", effects.get(item_id) == "变了",
          effects.get(item_id, "?"))

check("「status」会弹一条状态提示", effects.get("status") == "弹提示")
check("托盘双击（__activate__）也会弹状态提示",
      effects.get("__activate__") == "弹提示")
check("「help」会弹出使用帮助", effects.get("help") == "弹帮助")
check("「open_log」去打开日志文件",
      len(opened) >= 1 and opened[0].name.endswith(".log"), str(opened[:1]))
check("「open_config」去打开配置文件",
      any(p.name == "config.json" or p.name.endswith("config.json") for p in opened),
      str(opened))
check("「quit」置上了退出请求", effects.get("quit") == "变了")

# 退出请求单独再验一遍（上面循环里每次都重置，这里要确认它真会留下）
app.core.quit_requested = False
app._handle_tray_action("quit")
check("「quit」确实把 quit_requested 立起来了", app.core.quit_requested is True)
app.core.quit_requested = False

# ---------------------------------------------------------------------- #
head(6, "菜单项写错名字时必须吵一声（不能静默）")

err = io.StringIO()
try:
    with contextlib.redirect_stderr(err):
        app._handle_tray_action("no_such_action")
finally:
    loud = err.getvalue()

check("未知动作会在 stderr 上点名", "不认识这个菜单动作" in loud, loud.strip()[:200])
check("未知动作不会被悄悄丢给状态机", "未知热键动作" not in loud, loud.strip()[:200])

# ---------------------------------------------------------------------- #
head(7, "_sync_tray 把状态刷到托盘（并且状态没变就不刷）")

app.core.enabled = True
app.core.direct = False
app.core.active = 0
app._tray_sig = None
fake3 = FakeTray()
app.tray = fake3
app._sync_tray(force=True)

set_map = {item_id: checked for item_id, checked, _, _ in fake3.sets}
check("三个开关都刷了（勾选状态跟着状态机）",
      set(set_map) == set(CORE_ACTIONS), str(set_map))
check("enabled=True → 「启用输入法」打勾", set_map.get("toggle_enabled") is True)
check("direct=False → 「英文直通」不打勾", set_map.get("toggle_direct") is False)
check("active=0 → 「上屏译文」不打勾", set_map.get("toggle_default") is False)
check("托盘提示里带着当前状态", bool(fake3.tooltips)
      and "translate-ime" in fake3.tooltips[-1], str(fake3.tooltips[-1:]))

n_before = len(fake3.sets)
app._sync_tray()               # 状态没变 → 不该再刷
check("状态没变时不再重复刷新托盘", len(fake3.sets) == n_before,
      f"{n_before} -> {len(fake3.sets)}")

app.core.direct = True
app._sync_tray()
set_map = {item_id: checked for item_id, checked, _, _ in fake3.sets}
check("直通打开后 → 「英文直通」重新打勾", set_map.get("toggle_direct") is True)

# 没装托盘时不能炸（托盘装不上是常态）
app.tray = None
try:
    app._sync_tray(force=True)
    survived = True
except Exception as exc:  # noqa: BLE001
    survived = False
    print(f"  ! {exc!r}")
check("没有托盘时 _sync_tray 静默跳过（不抛异常）", survived)

# ---------------------------------------------------------------------- #
head(8, "「打开配置文件」打开的是实例在读的那份（--config 指到别处时）")

# 第 61 轮：托盘/面板的「打开配置文件」以前写死 root/config.json。
# 实例带 --config 另一份配置跑时，用户打开编辑的其实是另一份，
# 改完永远不生效 —— 所以这里必须认 config_path。
alt_cfg = ROOT / ".tmp" / "tray_alt_config.json"
alt_cfg.parent.mkdir(parents=True, exist_ok=True)
alt_cfg.write_text('{"output": {"start_enabled": false}}', encoding="utf-8")
alt_app = App(cfg, ROOT, verbose=False, install_hook=False, config_path=alt_cfg)
alt_opened: list[Path] = []
alt_app._open_path = lambda p: alt_opened.append(Path(p))
alt_app._handle_tray_action("open_config")
check("--config 指到别处时，打开的是那一份",
      bool(alt_opened) and alt_opened[-1].resolve() == alt_cfg.resolve(), str(alt_opened))

alt_app.core.toast = ""
missing_cfg = (ROOT / ".tmp" / "tray_missing_config.json").resolve()
missing_cfg.unlink(missing_ok=True)
alt_app._config_path = missing_cfg
alt_opened.clear()
alt_app._handle_tray_action("open_config")
check("配置文件还不存在时，打开模板 config.example.json",
      bool(alt_opened) and alt_opened[-1].name == "config.example.json", str(alt_opened))
check("并且说清「启动用的配置文件还不存在」，不假装打开的就是它",
      "启动用的配置文件还不存在" in alt_app.core.toast
      and "模板" in alt_app.core.toast, alt_app.core.toast)

alt_app._config_path = None
alt_opened.clear()
alt_app._handle_tray_action("open_config")
check("没指定 --config 时打开本目录的 config.json",
      bool(alt_opened) and alt_opened[-1] == ROOT / "config.json", str(alt_opened))
alt_cfg.unlink(missing_ok=True)

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print(f"  结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
for p in PROBLEMS:
    print(f"  ! {p}")
sys.exit(1 if FAIL else 0)
