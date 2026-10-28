# -*- coding: utf-8 -*-
"""设置窗口（``t_ime/panel.py``）的纯逻辑契约。

为什么值得单测：这个窗口是新用户**唯一**能动手的地方（托盘在低完整性进程里
装不上，控制台菜单填不了 API key）。它错了不会崩，只会静静地把 API key 写成
``********``、把缺省的配置项写成空串、或者把「权限不足，打不了字」说成
「一切正常」—— 每一条都是这次真机撞过的坑，所以这里不弹窗，只测纯函数。

直接跑：``python tests/test_panel.py``
"""
import ast
import json
import os
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

import main as main_mod  # noqa: E402
from t_ime import config as C  # noqa: E402
from t_ime import panel as P  # noqa: E402

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
    print(f"{n}. {title}")
    print("-" * 72)


def clone(cfg: dict) -> dict:
    return json.loads(json.dumps(cfg, ensure_ascii=False))


# ---------------------------------------------------------------------- #
head(1, "表单字段必须都落在真实的配置树上")
absent = object()
missing = [f["path"] for f in P.FIELDS
           if P._get(C.DEFAULTS, f["path"], absent) is absent]
check("每个字段都能在 config.DEFAULTS 里找到", not missing, str(missing))

paths = [f["path"] for f in P.FIELDS]
check("字段路径没有重复", len(paths) == len(set(paths)),
      f"{len(paths)} 个字段 / {len(set(paths))} 个唯一路径")

kinds = {f["kind"] for f in P.FIELDS}
check("只用了已知的控件类型", kinds <= {"text", "int", "float", "bool",
                                   "choice", "hotkey", "password"}, str(kinds))
check("choice 字段都带了选项表",
      all(f.get("options") for f in P.FIELDS if f["kind"] == "choice"),
      str([f["path"] for f in P.FIELDS
           if f["kind"] == "choice" and not f.get("options")]))

vals = P.values_from_config(C.DEFAULTS)
check("每个字段都取得到值（不会有空着的行）",
      set(vals) == set(paths), str(sorted(set(paths) - set(vals))))

# 配置文件里整段缺失（比如老版本的 config.json 没有 input 段）时，要显示默认值，
# 否则用户点一下「保存」就把这些键写成空串了。
empty_vals = P.values_from_config({})
check("配置里缺 json 段时回退到默认值",
      empty_vals["api.base_url"] == C.DEFAULTS["api"]["base_url"]
      and empty_vals["input.shift_tap_ms"] == C.DEFAULTS["input"]["shift_tap_ms"],
      str(empty_vals["input.shift_tap_ms"]))

roundtrip = P.apply_values(C.DEFAULTS, P.values_from_config(C.DEFAULTS))
check("默认配置过一遍表单还是它自己", roundtrip == C.DEFAULTS)

# ---------------------------------------------------------------------- #
head(2, "API key：只显示掩码，没动过就不能被覆盖")
cfg = clone(C.DEFAULTS)
cfg["api"]["api_key"] = "sk-live-secret"
form = P.values_from_config(cfg)
check("界面上看到的是掩码，不是原文",
      form["api.api_key"] == P.MASK_FOR_UI, str(form["api.api_key"]))
check("整张表单里都不含 key 原文",
      "sk-live-secret" not in json.dumps(form, ensure_ascii=False))

merged = P.apply_values(cfg, form)
check("没动过那个框：原 key 保住",
      merged["api"]["api_key"] == "sk-live-secret", str(merged["api"]["api_key"]))

merged = P.apply_values(cfg, form, key_touched=True)
check("真敲了新的 key：写进去",
      merged["api"]["api_key"] == str(form["api.api_key"]))

merged = P.apply_values(cfg, form, clear_key=True)
check("点「清空 key」：值清空并带上显式清除标记",
      merged["api"]["api_key"] == "" and merged["api"]["api_key_clear"] is True,
      f"{merged['api']['api_key']!r} / {merged['api']['api_key_clear']!r}")

merged = P.apply_values(cfg, form, key_touched=True)
check("敲过 key 之后 api_key_clear 要落回 False（否则保存时又被删一次）",
      merged["api"]["api_key_clear"] is False)

check("apply_values 不改传进来的那份",
      cfg["api"]["api_key"] == "sk-live-secret")

# ---------------------------------------------------------------------- #
head(3, "数字输入框：变不成数字就把原文交回去，让 config.sanitize 报错")
check("int 收 3", P.coerce_number("int", "3") == 3)
check("int 收 3.5 原样交回（不偷偷截成 3）", P.coerce_number("int", "3.5") == 3.5)
check("float 收 2.5", P.coerce_number("float", "2.5") == 2.5)
check("float 收带空格的 8", P.coerce_number("float", " 8 ") == 8.0)
check("非数字原样交回", P.coerce_number("int", "abc") == "abc")
check("空串原样交回（由 sanitize 决定是报错还是用默认）",
      P.coerce_number("int", "") == "")
check("已经是数字就原样返回", P.coerce_number("int", 7) == 7)

# ---------------------------------------------------------------------- #
head(4, "诊断：状态文件 → 人话 + 该怎么办")
not_running = P.diagnose(C.DEFAULTS, None, False, None, None)
check("没在跑：说「输入法没在运行」，并给「启动输入法」按钮",
      not_running["banner"]["level"] == "bad"
      and not_running["banner"]["action"] == "start"
      and "没在运行" in not_running["banner"]["title"],
      not_running["banner"]["title"])

healthy = {
    "pid": 4242, "hook": True, "hook_info": {"real": 12, "total": 20},
    "tray": True, "overlay": {"shows": 3, "cand_shows": 2},
    "translate": {"enabled": True, "mock": False, "provider": "deepseek",
                  "requests": 2, "cache_hits": 1},
    "boosts": 5,
}
info = P.diagnose(C.DEFAULTS, healthy, True, (0x2000, "Medium"), "未开启")
labels = [r["label"] for r in info["rows"]]
check("状态卡覆盖：权限/钩子/托盘/候选窗/翻译/自学习/实例",
      all(k in labels for k in
          ("权限", "键盘钩子", "托盘图标", "候选窗", "翻译", "自学习", "实例")),
      str(labels))
check("一切正常 → 绿色结论，并教怎么打字",
      info["banner"]["level"] == "ok" and "拼音" in info["banner"]["title"],
      info["banner"]["title"])

low = dict(healthy)
low_info = P.diagnose(C.DEFAULTS, low, True, (0x1000, "Low"), None)
check("低完整性 + 钩子装上了：也判「打不了字」（UIPI 会吃掉按键）",
      low_info["banner"]["level"] == "bad"
      and low_info["banner"]["action"] == "elevate",
      low_info["banner"]["title"])
check("低完整性那条把原因说出来（UIPI / 托盘装不上）",
      "UIPI" in low_info["banner"]["detail"]

      and "托盘" in low_info["banner"]["detail"])
check("权限那一行标红", [r for r in low_info["rows"] if r["label"] == "权限"][0]["level"]
      == "bad")

no_hook = P.diagnose(C.DEFAULTS, {"hook": False, "tray": False}, True, None, None)
check("钩子没装上 → 给「看日志」而不是「提权」",
      no_hook["banner"]["action"] == "log", no_hook["banner"]["title"])

never = P.diagnose(C.DEFAULTS, {"hook": True, "hook_info": {"real": 0},
                                "tray": True}, True, None, None)
check("钩子装上了但一个真实按键都没收到 → 黄色提醒",
      never["banner"]["level"] == "warn"
      and "真实按键" in never["banner"]["title"], never["banner"]["title"])

mock_cfg = clone(C.DEFAULTS)
mock_cfg["api"]["api_key"] = ""
mock_st = dict(healthy, translate={"enabled": True, "mock": True,
                                   "mock_reason": "没配 API key"})
mock_info = P.diagnose(mock_cfg, mock_st, True, None, None)
mock_row = [r for r in mock_info["rows"] if r["label"] == "翻译"][0]
check("没配 key：翻译那行说「离线词表」而不是假装在线",
      "离线" in mock_row["value"] and "没配 API key" in mock_row["value"],
      mock_row["value"])

off_cfg = clone(C.DEFAULTS)
off_cfg["translate"]["enabled"] = False
off_info = P.diagnose(off_cfg, dict(healthy, translate={"enabled": False}),
                      True, None, None)
off_row = [r for r in off_info["rows"] if r["label"] == "翻译"][0]
check("关掉翻译：那行说「已关闭（只做拼音→中文）」", "已关闭" in off_row["value"],
      off_row["value"])

broken_ov = P.diagnose(C.DEFAULTS, dict(healthy, overlay={"shows": 1,
                                                          "error": "TclError: boom"}),
                        True, None, None)
ov_row = [r for r in broken_ov["rows"] if r["label"] == "候选窗"][0]
check("候选窗报过错：把异常原文贴出来（不是只说「0 次」）",
      "TclError" in ov_row["value"] and ov_row["level"] == "bad", ov_row["value"])

autostart_info = P.diagnose(C.DEFAULTS, healthy, True, None, "已开启（注册表）")
check("开机自启有状态时单独一行",
      [r["value"] for r in autostart_info["rows"] if r["label"] == "开机自启"]
      == ["已开启（注册表）"])

# ---------------------------------------------------------------------- #
head(5, "三种进程命令行：都要带 --detached（否则无限套娃）")
panel_argv = P.panel_argv(ROOT, ROOT / "config.json")
start_argv = P.start_argv(ROOT, ROOT / "config.json")
elev_argv = P.elevate_argv(ROOT, ROOT / "config.json")

check("面板：--panel + --detached",
      "--panel" in panel_argv and "--detached" in panel_argv
      and "--background" not in panel_argv, " ".join(panel_argv[-4:]))
check("启动实例：--background + --detached，且不带 --panel（免得开窗）",
      "--background" in start_argv and "--detached" in start_argv
      and "--panel" not in start_argv, " ".join(start_argv[-4:]))
check("提权重启：实例和窗口都要（--background --panel --detached）",
      {"--background", "--panel", "--detached"} <= set(elev_argv),
      " ".join(elev_argv[-5:]))
check("三条命令都把 --config 钉成用户给的那份",
      all(str(ROOT / "config.json") in a for a in
          (panel_argv, start_argv, elev_argv)))

# ---------------------------------------------------------------------- #
head(6, "启动时到底弹不弹窗")
REAL_STDIN = sys.stdin


class _Fake:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def read(self, *a, **k):  # pragma: no cover - 不该被调用
        return ""


def want(**kw) -> bool:
    base = dict(no_panel=False, panel=False, detached=False, background=True,
                foreground=False)
    base.update(kw)
    return main_mod._want_panel(Namespace(**base), C.DEFAULTS)


try:
    sys.stdin = _Fake(True)
    # 第 124 轮改成「只认 --panel」：isatty() 在 stdin/stdout 指向 NUL 时也
    # 返回 True，靠它猜会把窗口弹到脚本和测试脸上。
    check("run.bat 双击（带 --panel）：默认弹窗", want(panel=True) is True)
    check("没写 --panel（脚本 / bare main.py）：不弹", want() is False)
    check("stdin 是不是终端不影响结论", want(panel=True) is True)
    check("--no-panel：不弹（连 --panel 也压得住）",
          want(panel=True, no_panel=True) is False)
    check("常驻实例自己（--detached）：不弹，否则套娃", want(panel=True, detached=True) is False)
    check("--foreground：不弹（那时候用户本来就盯着控制台）",
          want(panel=True, foreground=True) is False)
    cfg_off = clone(C.DEFAULTS)
    cfg_off["ui"]["open_panel_on_start"] = False
    check("配置 ui.open_panel_on_start=false：连 run.bat 的 --panel 也不弹",
          main_mod._want_panel(
              Namespace(no_panel=False, panel=True, detached=False,
                        background=True, foreground=False), cfg_off) is False)
    sys.stdin = _Fake(False)
    check("stdin 被重定向（NUL 也照样 isatty=True 的那个坑）：结论不变",
          want(panel=True) is True and want() is False)
finally:
    sys.stdin = REAL_STDIN

# ---------------------------------------------------------------------- #
head(7, "设置窗口只许开一个")
tmp_root = Path(tempfile.mkdtemp(dir=ROOT / ".tmp"))
try:
    check("没锁时 other_panel 返回 0", P.other_panel(tmp_root) == 0)
    P.write_lock(tmp_root)
    check("自己写的锁不会被自己当成「别人」",
          P.read_lock(tmp_root) == os.getpid() and P.other_panel(tmp_root) == 0)
    (tmp_root / P.LOCK_NAME).write_text("999999", encoding="utf-8")
    check("陈旧的锁（进程早没了）被无视", P.other_panel(tmp_root) == 0)
    P.write_lock(tmp_root)
    check("探活不会顺手把进程杀掉（Windows 的 os.kill(pid,0) 会）",
          P._pid_alive(os.getpid()) is True)
    P.release_lock(tmp_root)
    check("release_lock 之后锁文件消失",
          not (tmp_root / P.LOCK_NAME).exists())
finally:
    for child in tmp_root.iterdir():
        child.unlink(missing_ok=True)
    tmp_root.rmdir()

# ---------------------------------------------------------------------- #
head(8, "热键摘要与源码可编译")
summary = P.hotkey_summary(C.DEFAULTS)
check("摘要里能读出上屏热键", "space" in summary.lower(), summary)
check("摘要里能读出退出热键", "q" in summary.lower())

for name, path in (("panel.py", ROOT / "t_ime" / "panel.py"),
                   ("main.py", ROOT / "main.py")):
    try:
        ast.parse(path.read_text(encoding="utf-8"))
        ok = True
        err = ""
    except SyntaxError as exc:  # pragma: no cover
        ok = False
        err = str(exc)
    check(f"{name} 语法可编译", ok, err)

# ---------------------------------------------------------------------- #
head(9, "保存真的能写进配置（走 webpanel 那条同源链路）")
# 这一节是「窗口上点保存」的替身：collect() 产出的 dict 形状一样，直接喂给
# webpanel.save_panel_config，看文件里到底改成了什么。面板自己不做校验也不
# 自己写文件 —— 就是想证明「窗口」和「网页」不是两套实现。
import json
import shutil
import tempfile

from t_ime import config as C
from t_ime import webpanel

tmp = Path(tempfile.mkdtemp(prefix="t-ime-panel-", dir=str(ROOT / ".tmp")))
tmp_cfg = tmp / "config.json"
tmp_root = tmp / "root"
tmp_root.mkdir()
tmp_cfg.write_text(json.dumps({
    "api": {"provider": "deepseek", "api_key": "sk-test-1234567890abcd"},
    "hotkeys": {"compose": "ctrl+alt+space"},
    "pinyin": {"min_freq": 20},
}, ensure_ascii=False), encoding="utf-8")

try:
    cfg, cfg_path = C.load(tmp_cfg)
    check("测试配置读进来没有报错", not C.LAST_LOAD_ERROR, C.LAST_LOAD_ERROR)

    values = P.values_from_config(cfg)
    check("values_from_config 产出的值数量 == 字段数量",
          len(values) == len(P.FIELDS), f"{len(values)} vs {len(P.FIELDS)}")

    values["hotkeys.compose"] = "ctrl+alt+k"
    # 界面里显示的永远是 ********；用户没动过它（key_touched=False）就不该写。
    values["translate.debounce_ms"] = 300
    values["pinyin.min_freq"] = 20             # 故意不动，下面单独测「要重启」
    res = webpanel.save_panel_config(tmp_cfg, P.apply_values(cfg, values), tmp_root)
    check("保存返回 ok=True", res.get("ok") is True, res.get("errors"))
    saved = json.loads(tmp_cfg.read_text(encoding="utf-8-sig"))
    check("热键改到文件里了", saved["hotkeys"]["compose"] == "ctrl+alt+k",
          saved["hotkeys"].get("compose"))
    check("数字按数字写进去（不是字符串）",
          saved["translate"]["debounce_ms"] == 300
          and isinstance(saved["translate"]["debounce_ms"], int),
          repr(saved["translate"].get("debounce_ms")))
    check("打了*的 api_key 保住了原值（没被写成 ********）",
          saved["api"]["api_key"] == "sk-test-1234567890abcd",
          saved["api"].get("api_key"))
    raw = tmp_cfg.read_text(encoding="utf-8-sig")
    check("中文注释/值没被转成 \\uXXXX", "\\u" not in raw)

    # 坏热键：必须拒绝写，而且原来的值不能被破坏
    values["hotkeys.compose"] = "ctrl+alt+"
    bad = webpanel.save_panel_config(tmp_cfg, P.apply_values(cfg, values), tmp_root)
    check("坏热键被拒绝（ok=False）", bad.get("ok") is False)
    check("拒绝理由点名了 compose",
          any("compose" in str(e) or "热键" in str(e) for e in bad.get("errors", [])),
          bad.get("errors"))
    check("拒绝时文件没被改坏",
          json.loads(tmp_cfg.read_text(encoding="utf-8-sig"))["hotkeys"]["compose"]
          == "ctrl+alt+k")

    # 显示成星号但 key_touched=False：文件里的明文必须一个字都没变
    touched = P.apply_values(cfg, values, key_touched=True)
    check("真动过输入框才写 api_key",
          touched["api"]["api_key"] == webpanel.MASK, touched["api"].get("api_key"))

    # 显式清空 key
    values["hotkeys.compose"] = "ctrl+alt+k"
    cleared = P.apply_values(cfg, values, clear_key=True)
    check("clear_key 之后 api_key 变空串", cleared["api"]["api_key"] == "", cleared["api"].get("api_key"))
    check("clear_key 带上 api_key_clear 标记（否则 _merge_secret 会留着旧 key）",
          cleared["api"].get("api_key_clear") is True)

    # 改了要重启才生效的键，得回一个 restart_required
    values["pinyin.min_freq"] = 25
    res2 = webpanel.save_panel_config(tmp_cfg, P.apply_values(cfg, values), tmp_root)
    check("改 pinyin.min_freq 会提示要重启",
          bool(res2.get("restart_required")), res2.get("restart_required"))
finally:
    shutil.rmtree(str(tmp), ignore_errors=True)
    check("临时目录被清干净", not tmp.exists())

# ---------------------------------------------------------------------- #
head(10, "一次性动作不会被 --panel 吞掉（第 124 轮回归）")
# ``if args.panel:`` 那段原来紧跟在 --web 后面，于是 --check / --demo / --doctor
# 根本到不了：``run.bat --check`` 会弹个窗口然后返回 0，自检一个字都不打。
from t_ime import panel as PANEL_MOD

tmp2 = Path(tempfile.mkdtemp(prefix="t-ime-panel2-", dir=str(ROOT / ".tmp")))
cfg2 = tmp2 / "config.json"
cfg2.write_text(json.dumps({"api": {"provider": "mock"}}, ensure_ascii=False),
                encoding="utf-8")
spawned: list[tuple] = []
_real_spawn = PANEL_MOD.spawn_panel


def _fake_spawn(*args: object, **kwargs: object) -> tuple[bool, str]:
    spawned.append((args, kwargs))
    return True, "（测试里假装开了窗口）"


PANEL_MOD.spawn_panel = _fake_spawn  # type: ignore[assignment]
try:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc_check = main_mod.main(["--panel", "--check", "--config", str(cfg2)])
    out_check = buf.getvalue()
    check("--panel --check 走的是自检，不是开窗口",
          "全部通过" in out_check or "失败" in out_check, out_check.strip()[:120])
    check("--panel --check 没有去开窗口", spawned == [], str(spawned[:1]))
    check("--panel --check 退出码是自检的（0 或 1，不是被窗口改写）",
          rc_check in (0, 1), f"rc={rc_check}")

    spawned.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc_only = main_mod.main(["--panel", "--config", str(cfg2)])
    check("只给 --panel：开一个窗口然后返回", rc_only == 0 and len(spawned) == 1,
          f"rc={rc_only} spawned={len(spawned)}")

    spawned.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc_fore = main_mod.main(["--panel", "--foreground", "--config", str(cfg2),
                                 "--no-hook", "--demo", "--demo-seconds", "0.2"])
    check("--panel 被 --foreground 压住时不弹窗", spawned == [], str(spawned[:1]))
finally:
    PANEL_MOD.spawn_panel = _real_spawn  # type: ignore[assignment]
    shutil.rmtree(str(tmp2), ignore_errors=True)

# ---------------------------------------------------------------------- #
head(11, "双击打包版 exe：等价于双击 run.bat，而且已经在跑时也要给窗口（第 125 轮）")
# 打包版没有 run.bat 可点，用户双击的就是 exe 本身，argv 为空。以前这条路径
# 掉进「前台跑一个没有控制台的实例、不弹窗口」，双击之后什么都没出现。
import contextlib  # noqa: E402
import io  # noqa: E402

from t_ime import daemon  # noqa: E402
from t_ime import winapi as W  # noqa: E402

_frozen_backup = getattr(sys, "frozen", None)
sys.frozen = True  # type: ignore[attr-defined]
try:
    check("双击（无参数）补成 --background --panel",
          main_mod._double_click_args([]) == ["--background", "--panel"],
          str(main_mod._double_click_args([])))
    check("带了参数就不动（--check 不能被补成后台）",
          main_mod._double_click_args(["--check"]) == ["--check"])
    check("--no-panel 之类照原样进去（由 _want_panel 决定弹不弹）",
          main_mod._double_click_args(["--background", "--no-panel"])
          == ["--background", "--no-panel"])
finally:
    if _frozen_backup is None:
        try:
            del sys.frozen  # type: ignore[attr-defined]
        except AttributeError:
            pass
    else:
        sys.frozen = _frozen_backup  # type: ignore[attr-defined]

check("源码模式 python main.py 不带参数：还是前台跑，不被静默改成后台",
      main_mod._double_click_args([]) == [])

# 窗口版 exe 双击时 sys.stdout / sys.stderr 是 None，print 一下就 AttributeError。
_std_backup = (sys.stdout, sys.stderr)
try:
    sys.stdout = None  # type: ignore[assignment]
    sys.stderr = None  # type: ignore[assignment]
    main_mod._ensure_streams()
    ok_streams = sys.stdout is not None and sys.stderr is not None
    print("")  # 真跑一句，确认不会再炸
    check("stdout/stderr 为 None 时被换成能写的空流", ok_streams)
finally:
    sys.stdout, sys.stderr = _std_backup

# 「已经在跑」时双击，仍然要把窗口开出来 —— 那正是用户要找的入口。
tmp3 = Path(tempfile.mkdtemp(prefix="t-ime-panel3-", dir=str(ROOT / ".tmp")))
cfg3 = tmp3 / "config.json"
cfg3.write_text(json.dumps({"api": {"provider": "mock"}}, ensure_ascii=False),
                encoding="utf-8")
spawned2: list[tuple] = []


def _fake_spawn2(*args: object, **kwargs: object) -> tuple[bool, str]:
    spawned2.append((args, kwargs))
    return True, "（测试里假装开了窗口）"


_real_spawn2 = PANEL_MOD.spawn_panel
_real_inst = W.instance_exists
_real_bg = daemon.spawn_background
PANEL_MOD.spawn_panel = _fake_spawn2  # type: ignore[assignment]
try:
    # (a) 状态文件说实例活着 —— 走「第二个实例变控制面板」那条路
    W.instance_exists = lambda *a, **k: True  # type: ignore[assignment]
    daemon.spawn_background = lambda *a, **k: daemon.SPAWN_ALREADY_RUNNING  # type: ignore[assignment]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc_dup = main_mod.main(["--background", "--panel", "--config", str(cfg3)])
    out_dup = buf.getvalue()
    check("已经在跑 + 要窗口：照样开窗（不是直接 return 0）",
          len(spawned2) == 1 and rc_dup == 0,
          f"rc={rc_dup} spawned={len(spawned2)} out={out_dup.strip()[:100]}")
    check("已经在跑：说的是「已经在跑」，没有再去拉一份实例",
          "已经在跑了" in out_dup and "已在后台启动" not in out_dup,
          out_dup.strip()[:120])

    # (b) 状态文件还没写出来、但互斥体已被抢（两次双击撞车的竞态）
    spawned2.clear()
    W.instance_exists = lambda *a, **k: False  # type: ignore[assignment]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc_race = main_mod.main(["--background", "--panel", "--config", str(cfg3)])
    out_race = buf.getvalue()
    check("撞车（spawn 返回「已经在跑」）时也开窗",
          len(spawned2) == 1 and rc_race == 0,
          f"rc={rc_race} spawned={len(spawned2)} out={out_race.strip()[:100]}")
    check("撞车时明说「已经有一份在后台跑着」", "已经有一份" in out_race,
          out_race.strip()[:120])
finally:
    PANEL_MOD.spawn_panel = _real_spawn2  # type: ignore[assignment]
    W.instance_exists = _real_inst  # type: ignore[assignment]
    daemon.spawn_background = _real_bg  # type: ignore[assignment]
    shutil.rmtree(str(tmp3), ignore_errors=True)

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print(f"  结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
for p in PROBLEMS:
    print(f"  ! {p}")
sys.exit(1 if FAIL else 0)
