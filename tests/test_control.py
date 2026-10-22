# -*- coding: utf-8 -*-
"""第二个实例的控制面板：状态文件 / 命令文件，以及菜单派发。

为什么值得单测：托盘图标在低完整性进程里装不上（UIPI 拦 `Shell_NotifyIconW`），
而托盘本来是唯一能退出/挂起/换语言的入口。所以「第二次双击 run.bat」这条退路
必须真的可靠 —— 它靠的不是 IPC，而是两个普通文件，正好可以脱机验证。

直接跑：``python tests/test_control.py``
"""
import builtins
import contextlib
import copy
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

import main as main_mod  # noqa: E402
from t_ime import daemon, winapi as W  # noqa: E402
from t_ime.app import App  # noqa: E402
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


cfg, _ = load()

# 万一真有个常驻实例在跑，别踩它的状态文件（注意顺序：先备份再清，反了就永远备份到 None）
saved = daemon.status_path(ROOT).read_bytes() if daemon.status_path(ROOT).exists() else None
daemon.clear_status(ROOT)

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  1. 状态文件")
print("=" * 72)

check("read_status() 在没有文件时返回 None", daemon.read_status(ROOT) is None)
check("status_is_stale() 在没有文件时算「旧」", daemon.status_is_stale(ROOT) is True)

sample = {"pid": 1234, "enabled": True, "direct": False, "active": "zh"}
check("write_status() 成功", daemon.write_status(ROOT, sample) is True)
check("read_status() 原样读回", daemon.read_status(ROOT) == sample,
      str(daemon.read_status(ROOT)))
check("状态文件是 UTF-8 的 JSON",
      json.loads(daemon.status_path(ROOT).read_text(encoding="utf-8"))["pid"] == 1234)
check("刚写的状态不算「旧」", daemon.status_is_stale(ROOT) is False)
# 阈值真的生效吗？不能拿「刚刚写出来的文件 + max_age=0」去问：status_age() 是
# max(0.0, now - mtime)，同一个时钟节拍内算出来正好是 0.0，而 0.0 > 0 是假 ——
# 这条断言于是只在「状态文件根本不存在」（status_age 返回 None → 算旧）时才通过，
# 别的测试留下一个状态文件就会让它红（实测：先跑 test_commit_fallback 必现）。
# 改成把 mtime 明确拨到 5 秒前，再问阈值，两边都确定。
_ctl_status = daemon.status_path(ROOT)
_st = _ctl_status.stat().st_mtime
os.utime(_ctl_status, (_st - 5.0, _st - 5.0))
check("mtime 拨到 5 秒前后：max_age=1 秒算「旧」（阈值生效）",
      daemon.status_is_stale(ROOT, max_age=1.0) is True)
check("同一个文件：max_age=60 秒不算「旧」（阈值反向也生效）",
      daemon.status_is_stale(ROOT, max_age=60.0) is False)
check("写盘不留临时文件", not daemon.status_path(ROOT).with_suffix(".json.tmp").exists())

daemon.status_path(ROOT).write_text("{这不是 json", encoding="utf-8")
check("坏 JSON 时返回 None 而不是崩", daemon.read_status(ROOT) is None)
daemon.status_path(ROOT).write_text('["数组不是字典"]', encoding="utf-8")
check("数组也返回 None", daemon.read_status(ROOT) is None)

daemon.status_path(ROOT).write_bytes(
    ('\ufeff' + json.dumps(sample)).encode("utf-8"))
check("状态文件被塞了 BOM 也照样读得出来",
      daemon.read_status(ROOT) == sample, str(daemon.read_status(ROOT)))

daemon.clear_status(ROOT)
check("clear_status() 把状态文件清掉了", not daemon.status_path(ROOT).exists())

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  2. 命令收件箱")
print("=" * 72)

check("没有命令文件时 take_commands() 返回空表", daemon.take_commands(ROOT) == [])
check("send_command() 成功", daemon.send_command(ROOT, "toggle_direct") is True)
check("追加的第二条也在（连点两下不互相覆盖）",
      daemon.send_command(ROOT, "quit") is True)
check("take_commands() 按顺序全部取出",
      daemon.take_commands(ROOT) == ["toggle_direct", "quit"])
check("取过一次之后就空了（取完即清）", daemon.take_commands(ROOT) == [])
check("命令文件已被删掉", not daemon.ctl_path(ROOT).exists())
daemon.send_command(ROOT, "  toggle_enabled  ")
check("前后空白会被 strip", daemon.take_commands(ROOT) == ["toggle_enabled"])

# 第二种写法人人会踩：记事本 / PowerShell 的 `Out-File -Encoding utf8` 会塞 BOM
daemon.ctl_path(ROOT).write_bytes("\ufeffquit\n".encode("utf-8"))
check("带 UTF-8 BOM 的命令文件也能读对（不会变成 '\\ufeffquit'）",
      daemon.take_commands(ROOT) == ["quit"],
      str(daemon.take_commands(ROOT)))

# 第 64 轮：send_command() 刚 open()、内容还没落盘的那一刻，实例来取命令，
# 以前的「读完再删」会把文件删掉、写入方随后写进一个已经消失的文件里 ——
# 那条命令（可能是 quit）就凭空没了。现场：点了退出，输入法还在。
with open(daemon.ctl_path(ROOT), "a", encoding="utf-8") as _fh:
    _early = daemon.take_commands(ROOT)      # 此刻文件存在但 0 字节
    _fh.write("quit\n")
check("写入中的收件箱这一拍先不动它（命令不能丢）", _early == [], _early)
check("写完的命令下一次还能取到（这次真的没丢）",
      daemon.take_commands(ROOT) == ["quit"])

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  3. 控制面板的菜单派发（换掉 input，模拟用户按键）")
print("=" * 72)

answers: list[str] = []
outer_isatty = sys.stdin.isatty


def fake_input(prompt: str = "") -> str:
    if not answers:
        raise EOFError
    return answers.pop(0)


class FakeStdin:
    def isatty(self) -> bool:
        return True


real_input = builtins.input
try:
    builtins.input = fake_input            # type: ignore[assignment]
    sys.stdin = FakeStdin()                # type: ignore[assignment]

    daemon.clear_status(ROOT)
    answers[:] = ["2", "0"]
    rc = main_mod.run_control_panel(cfg, ROOT, W)
    check("选 2 之后收件箱里是 toggle_direct",
          daemon.take_commands(ROOT) == ["toggle_direct"], f"rc={rc}")
    check("选 0 之后返回 0", rc == 0)

    answers[:] = ["bogus", "6", "0"]
    rc = main_mod.run_control_panel(cfg, ROOT, W)
    check("无效选项被忽略，接着还能正常选（选 6 发出 open_config）",
          daemon.take_commands(ROOT) == ["open_config"], f"rc={rc}")

    # 第 95 轮：Windows 上 stdin 重定向成 NUL 时 isatty() 仍返回真，于是进了菜单
    # 却读不到输入。以前这里静默 return 0 —— 脚本会把这个 0 当成「第二份实例也
    # 起来了」，而面板的契约是「已经在跑」用 3 表示。
    answers[:] = []
    rc = main_mod.run_control_panel(cfg, ROOT, W)
    check("★ 读到 EOF 时按非交互收工（返回 3，不是 0）", rc == 3, rc)

    class NotATty:
        def isatty(self) -> bool:
            return False

    sys.stdin = NotATty()                  # type: ignore[assignment]
    answers[:] = ["1"]
    rc = main_mod.run_control_panel(cfg, ROOT, W)
    check("非交互式控制台直接返回 3，不发命令",
          rc == 3 and daemon.take_commands(ROOT) == [], f"rc={rc}")
finally:
    builtins.input = real_input            # type: ignore[assignment]
    sys.stdin.isatty = outer_isatty        # type: ignore[method-assign]
    daemon.clear_status(ROOT)

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  4. 常驻实例真的会把命令执行掉（真 App，不装钩子）")
print("=" * 72)

app = App(cfg, ROOT, verbose=False, install_hook=False)
try:
    app._publish_status(force=True)
    st = daemon.read_status(ROOT)
    check("App 会把自己的状态写出去", bool(st) and st["pid"] > 0, str(st))
    check("状态里能看出「已启用 / 拼音 / 上屏中文」",
          bool(st) and st["enabled"] is True and st["direct"] is False
          and st["active"] == "zh")
    ov = (st or {}).get("overlay") or {}
    check("状态里带着候选窗的事实（shows / on_monitor / rect）",
          "shows" in ov and "on_monitor" in ov and "rect" in ov, str(ov))
    check("状态里分得清「候选窗」和「只是一条提示」",
          ov.get("mode") in ("hidden", "toast", "candidates"), str(ov))
    hk = (st or {}).get("hook_info") or {}
    check("状态里带着钩子的统计（real / injected / swallowed / alive）",
          all(k in hk for k in ("real", "injected", "swallowed", "alive")), str(hk))
    check("状态里带着主循环的错误计数",
          (st or {}).get("tick_errors") == 0, str((st or {}).get("tick_errors")))
    # 第 107 轮：安装失败曾被写成 hook: True（只问「对象在不在」）→ 启动器宣布「已就绪」。
    check("不装钩子时状态里 hook 是 False", (st or {}).get("hook") is False,
          str((st or {}).get("hook")))
    check("状态里标明这是「--no-hook 关的」，不是「装了但坏了」",
          ((st or {}).get("hook_info") or {}).get("disabled_by_flag") is True,
          str((st or {}).get("hook_info")))

    daemon.send_command(ROOT, "toggle_direct")
    app._drain_commands()
    check("toggle_direct 被真的执行了", app.core.direct is True)
    daemon.send_command(ROOT, "toggle_direct")
    app._drain_commands()
    check("再发一次能切回去", app.core.direct is False)

    daemon.send_command(ROOT, "toggle_enabled")
    app._drain_commands()
    check("toggle_enabled 被真的执行了", app.core.enabled is False)
    app.core.enabled = True

    daemon.send_command(ROOT, "toggle_default")
    app._drain_commands()
    check("toggle_default 换了上屏语言", app.core.active == 1)

    daemon.send_command(ROOT, "quit")
    app._drain_commands()
    check("quit 让常驻实例准备退出", app.core.quit_requested is True)
    app.core.quit_requested = False

    # 第 105 轮：在面板里点「换默认上屏语言」，翻译关着时状态机会拒绝 —— 可那句
    # 拒绝理由只弹在候选窗的 toast 上（1.6 秒），面板用户正看控制台，只会看到
    # 「上屏语言 中文」原地不动，以为菜单坏了。理由必须能通过状态文件读到。
    app.core.active = 0
    app.core.status = ""
    app.translator.enabled = False
    daemon.send_command(ROOT, "toggle_default")
    app._drain_commands()
    check("翻译关着时 toggle_default 拒绝切换（上屏语言留在中文）",
          app.core.active == 0, f"active={app.core.active}")
    check("拒绝的理由写在 core.status 里", "切不到译文" in app.core.status, app.core.status)
    app._publish_status(force=True)
    st3 = daemon.read_status(ROOT) or {}
    check("★ 拒绝理由进了状态文件（面板才看得见）",
          str(st3.get("status") or "") == app.core.status, str(st3.get("status")))
    app.core.status = "换好了"
    app._publish_status()                  # 只有 status 变了，其它都没动
    st4 = daemon.read_status(ROOT) or {}
    check("★ 只有 status 变了也要立刻写盘（不然面板要等下一次心跳）",
          str(st4.get("status") or "") == "换好了", str(st4.get("status")))
    app.core.status = ""
    app.translator.enabled = True

    app._publish_status(force=True)
    first = daemon.status_path(ROOT).read_bytes()
    app._publish_status()                  # 状态没变
    check("状态没变时不重复写盘（_status_sig 生效）",
          daemon.status_path(ROOT).read_bytes() == first)
finally:
    daemon.clear_status(ROOT)
    try:
        app.overlay.destroy()
    except Exception:
        pass

if saved is not None:
    daemon.status_path(ROOT).write_bytes(saved)

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  5. 控制面板怎么跟用户描述候选窗（故障点必须一眼看出来）")
print("=" * 72)

PANEL_BASE = {"pid": 4242, "enabled": True, "direct": False, "active": "zh",
              "tray": False, "hook": True, "log": "translate-ime.log", "tick_errors": 0}


def panel_text(**over) -> str:
    """把一份状态文件摆好，跑一遍控制面板，把它打印的东西抓回来。"""
    cfg_path = over.pop("cfg_path", None)
    use_cfg = over.pop("cfg", cfg)
    status = dict(PANEL_BASE)
    status.update(over)
    daemon.write_status(ROOT, status)
    buf = io.StringIO()
    real_stdin = sys.stdin
    builtins.input = lambda prompt="": "0"      # type: ignore[assignment]
    sys.stdin = FakeStdin()                     # type: ignore[assignment]
    try:
        with contextlib.redirect_stdout(buf):
            main_mod.run_control_panel(use_cfg, ROOT, W, cfg_path=cfg_path)
    finally:
        builtins.input = real_input             # type: ignore[assignment]
        sys.stdin = real_stdin                  # type: ignore[assignment]
    return buf.getvalue()


text = panel_text(overlay={"shows": 0, "rect": None, "on_monitor": False, "error": None},
                  hook_info={"real": 0, "self_injected": 0, "foreign_injected": 0,
                             "swallowed": 0, "alive": True})
check("还没敲过键时，面板不说「故障点」，只说「还没显示过」",
      "还没显示过" in text and "故障点" not in text and "还没收到过任何按键" in text,
      next((l.strip() for l in text.splitlines() if "候选窗" in l), ""))

# 第 82 轮：output.tray=false 是「你自己关的」，不是「没装上」。以前面板只有
# status["tray"] 一个字段，只能猜，于是把用户自己关掉的托盘写成「没装上」，
# 体检还会顺着说「UIPI 拦的」——用户会去查一个不该存在的故障。
text = panel_text(tray_enabled=False)
check("托盘是配置里关掉的：面板说「已关闭」，不说「没装上」",
      "已关闭（output.tray=false，你自己关的）" in text
      and "没装上（不影响打字）" not in text,
      next((l.strip() for l in text.splitlines() if "托盘" in l), ""))
text = panel_text(tray=True, tray_enabled=True)
check("托盘真的装上了：面板照旧说「已装上」", "已装上" in text,
      next((l.strip() for l in text.splitlines() if "托盘" in l), ""))

# 第 105 轮：实例拒绝动作时说的话，面板要摆成一行。以前它只活在候选窗的 toast 里。
text = panel_text(status="翻译关着（translate.enabled=false）—— 切不到译文")
check("★ 实例说的话（比如拒绝原因）面板会单独一行摆出来",
      "实例提示" in text and "切不到译文" in text,
      next((l.strip() for l in text.splitlines() if "实例提示" in l), ""))
text = panel_text()
check("实例没话说时，面板不多一行空的「实例提示」", "实例提示" not in text)

# 第 93 轮：实例正在建索引 / 装钩子时，状态文件还没写出来。以前面板一律说
# 「读不到状态：可能是旧版本在跑，或者状态文件被删了」—— 把「正在启动」说成故障。
print()
_orig_read_status = daemon.read_status
_orig_is_running = daemon.is_running
daemon.read_status = lambda *a, **k: None      # type: ignore[assignment]
daemon.is_running = lambda *a, **k: True       # type: ignore[assignment]
try:
    _buf = io.StringIO()
    _real_stdin = sys.stdin
    builtins.input = lambda prompt="": "0"      # type: ignore[assignment]
    sys.stdin = FakeStdin()                     # type: ignore[assignment]
    try:
        with contextlib.redirect_stdout(_buf):
            main_mod.run_control_panel(cfg, ROOT, W)
    finally:
        builtins.input = real_input             # type: ignore[assignment]
        sys.stdin = _real_stdin                 # type: ignore[assignment]
finally:
    daemon.read_status = _orig_read_status      # type: ignore[assignment]
    daemon.is_running = _orig_is_running        # type: ignore[assignment]
text = _buf.getvalue()
check("状态文件还没写出来、但实例真在跑：面板说「正在启动」，不说「旧版本在跑」",
      "实例正在启动" in text and "旧版本在跑" not in text,
      next((l.strip() for l in text.splitlines() if "启动" in l), ""))

daemon.status_path(ROOT).unlink(missing_ok=True)
daemon.is_running = lambda *a, **k: False       # type: ignore[assignment]
try:
    _buf2 = io.StringIO()
    _real_stdin = sys.stdin
    builtins.input = lambda prompt="": "0"      # type: ignore[assignment]
    sys.stdin = FakeStdin()                     # type: ignore[assignment]
    try:
        with contextlib.redirect_stdout(_buf2):
            main_mod.run_control_panel(cfg, ROOT, W)
    finally:
        builtins.input = real_input             # type: ignore[assignment]
        sys.stdin = _real_stdin                 # type: ignore[assignment]
finally:
    daemon.is_running = _orig_is_running        # type: ignore[assignment]
text = _buf2.getvalue()
check("没有实例在跑时，面板照旧说「读不到状态」（老话术不能丢）",
      "读不到状态" in text, next((l.strip() for l in text.splitlines() if "状态" in l), ""))

# 第 95 轮：指南第 5 节贴的「控制面板长什么样」是旧版格式 —— 它写着
# 「输入状态  已启用 · 中文」「托盘图标  已就绪」「退出  Ctrl+Alt+Q」，
# 菜单也只写了 3 项（[2] 切换默认语言、[3] 退出），而真面板是 12 行、菜单 7 项：
# 照着按「[3] 退出」只会切上屏语言。这里把指南和真面板钉在一起。
text = panel_text()
_guide = (ROOT / "docs" / "使用流程.md").read_text(encoding="utf-8")
_sec5 = _guide.split("## 5.", 1)[1].split("## 6.", 1)[0]
_LABELS = ["进程号", "输入法", "输入模式", "上屏语言", "托盘图标", "开机自启",
           "自学习", "翻译", "键盘钩子", "候选窗", "日志", "配置文件"]
_missing = [lab for lab in _LABELS if lab not in text]
check("面板真的会打印这 12 行（指南的样例得照它写）", not _missing, _missing)
_missing = [lab for lab in _LABELS if lab not in _sec5]
check("★ 指南第 5 节的面板样例用的是同一套行名", not _missing, _missing)
check("★ 指南不再写旧版面板里的「输入状态 / 已就绪 / 退出 Ctrl+Alt+Q」",
      "输入状态" not in _sec5 and "已就绪" not in _sec5
      and "退出         Ctrl+Alt+Q" not in _sec5, _sec5[:80])
_menu_labels = [lab for _, lab in main_mod.PANEL_MENU]
_missing = [lab for lab in _menu_labels if lab not in _sec5]
check("★ 指南第 5 节把 7 项菜单都列出来了（数字键能对上）",
      not _missing and len(main_mod.PANEL_MENU) == 7, _missing)
check("★ 指南不再写「[3] 退出」这种错菜单",
      "[3] 退出" not in _sec5 and "[2] 切换默认语言" not in _sec5, "")

# 第 91 轮：上屏语言写着「译文」、可翻译关着 —— 那个模式永远到不了（_commit 会退回
# 中文）。面板这一行必须自己说清楚，否则它就是一句空话。
text = panel_text(active="en",
                  translate={"enabled": False, "mock": True, "mock_reason": "没配 API key",
                             "provider": "mock", "requests": 0, "cache_hits": 0,
                             "errors": 0, "last_error": None})
lang_line = next((l.strip() for l in text.splitlines() if "上屏语言" in l), "")
check("译文行 + 翻译关着：面板说「翻译关着，实际上屏会退回中文」",
      "译文" in lang_line and "翻译关着" in lang_line and "退回中文" in lang_line,
      lang_line)
text = panel_text(active="en",
                  translate={"enabled": True, "mock": True, "mock_reason": "没配 API key",
                             "provider": "mock", "requests": 0, "cache_hits": 0,
                             "errors": 0, "last_error": None})
lang_line = next((l.strip() for l in text.splitlines() if "上屏语言" in l), "")
check("翻译开着时这一行不加多余的话",
      lang_line.endswith("译文") and "退回中文" not in lang_line, lang_line)

# 第 58 轮：挂起 / 英文直通时字母本来就直通，候选窗不弹是设计，不是故障。
text = panel_text(enabled=False, overlay={"shows": 0, "rect": None, "on_monitor": False,
                                          "error": None},
                  hook_info={"real": 18, "self_injected": 0, "foreign_injected": 0,
                             "swallowed": 0, "alive": True})
check("挂起时 shows=0 不判故障，说明「挂起时本来就不弹候选」",
      "挂起" in text and "本来就不弹候选" in text and "故障点" not in text,
      next((l.strip() for l in text.splitlines() if "候选窗" in l), ""))

text = panel_text(direct=True, overlay={"shows": 0, "rect": None, "on_monitor": False,
                                        "error": None},
                  hook_info={"real": 18, "self_injected": 0, "foreign_injected": 0,
                             "swallowed": 0, "alive": True})
check("英文直通时 shows=0 不判故障，说明「直通时本来就不弹候选」",
      "英文直通" in text and "本来就不弹候选" in text and "故障点" not in text,
      next((l.strip() for l in text.splitlines() if "候选窗" in l), ""))

# 第 72 轮：挂起时 Shift 单击根本不响应（core 里那条件是
# tapped and shift_tap_toggles and enabled），恢复只认 hotkeys.toggle_enabled；
# 而 input.shift_tap_toggles 关掉之后，切直通只认 hotkeys.toggle_direct。
cfg_notap = copy.deepcopy(cfg)
cfg_notap.setdefault("input", {})["shift_tap_toggles"] = False
cfg_notap["hotkeys"].update({"toggle_enabled": "ctrl+shift+f8",
                             "toggle_direct": "ctrl+shift+f11"})
text = panel_text(cfg=cfg_notap, enabled=False,
                  overlay={"shows": 0, "rect": None, "on_monitor": False, "error": None},
                  hook_info={"real": 18, "self_injected": 0, "foreign_injected": 0,
                             "swallowed": 0, "alive": True})
cand = next((l.strip() for l in text.splitlines() if "候选窗" in l), "")
check("挂起时面板不说「按 Shift 恢复」，用的是配置里的 toggle_enabled",
      "按 Shift" not in cand and "Ctrl+Shift+F8" in cand, cand)
check("挂起时也给面板菜单这条退路", "面板菜单恢复" in cand, cand)

text = panel_text(cfg=cfg_notap, direct=True,
                  overlay={"shows": 0, "rect": None, "on_monitor": False, "error": None},
                  hook_info={"real": 18, "self_injected": 0, "foreign_injected": 0,
                             "swallowed": 0, "alive": True})
cand = next((l.strip() for l in text.splitlines() if "候选窗" in l), "")
check("Shift 单击关着时，直通分支改用 hotkeys.toggle_direct 说怎么切回拼音",
      "Ctrl+Shift+F11" in cand and "按 Shift 切回" not in cand, cand)

cfg_none = copy.deepcopy(cfg_notap)
cfg_none["hotkeys"]["toggle_direct"] = ""
text = panel_text(cfg=cfg_none, direct=True,
                  overlay={"shows": 0, "rect": None, "on_monitor": False, "error": None},
                  hook_info={"real": 18, "self_injected": 0, "foreign_injected": 0,
                             "swallowed": 0, "alive": True})
cand = next((l.strip() for l in text.splitlines() if "候选窗" in l), "")
check("两个切直通的办法都没有时，面板直说「没有切回拼音的键」",
      "没有切回拼音的键" in cand and "Shift" not in cand, cand)

text = panel_text(overlay={"shows": 0, "rect": None, "on_monitor": False, "error": None},
                  hook_info={"real": 18, "self_injected": 0, "foreign_injected": 0,
                             "swallowed": 0, "alive": True})
check("真故障（拼音模式敲过键、候选一次没弹）面板仍直指故障点",
      "从未显示过" in text and "已经进钩子" in text and "故障点" in text,
      next((l.strip() for l in text.splitlines() if "候选窗" in l), ""))

# 第 71 轮：改键器/宏/软键盘机器上 real 永远是 0，但用户确实敲了字。
# 面板的候选窗判断要跟同一屏的「键盘钩子」行用同一个口径（有效按键）。
text = panel_text(overlay={"shows": 0, "rect": None, "on_monitor": False, "error": None},
                  hook_info={"real": 0, "self_injected": 0, "foreign_injected": 18,
                             "swallowed": 0, "alive": True, "last_effective_age": 1.0})
cand = next((l.strip() for l in text.splitlines() if "候选窗" in l), "")
check("只有改键器转发的按键时，面板不能说「你还没敲过键」",
      "还没收到过任何按键" not in cand and "还没敲过键" not in cand, cand)
check("只有转发的按键、候选一次没弹 → 面板照样指出故障点（带上有效按键数）",
      "从未显示过" in cand and "18 个有效按键" in cand and "故障点" in cand, cand)

text = panel_text(overlay={"shows": 3, "rect": [10, 20, 300, 100],
                           "on_monitor": True, "error": None})
check("正常时面板报显示次数与位置",
      "显示过 3 次" in text and "300x100+10+20" in text and "在显示器上" in text)

text = panel_text(overlay={"shows": 2, "rect": [10, 20, 300, 100],
                           "on_monitor": False, "error": None})
check("窗口跑到屏幕外时面板会明说", "跑到屏幕外" in text)

text = panel_text(overlay={"shows": 1, "rect": None, "on_monitor": True,
                           "error": "窗口显示后 IsWindowVisible 仍为假"}, tick_errors=2)
check("窗口显示失败时面板报出原因", "显示失败" in text and "IsWindowVisible" in text)
check("主循环报过错时面板会提一句（带伤运行）",
      "主循环异常" in text and "累计 2 次" in text)

# ---- 第 86 轮：提示条也算「窗口显示过一次」，不能当成候选窗出现过 ----
# 真机：静置不敲任何键，欢迎提示 + 托盘关闭提示各弹一次 → shows=2、cand_shows=0，
# 旧面板照样打「候选窗 显示过 2 次 · 最近 691x251」，用户会以为候选窗是好的。
text = panel_text(overlay={"shows": 2, "cand_shows": 0, "rect": [0, 0, 691, 251],
                           "on_monitor": True, "error": None})
cand = next((l.strip() for l in text.splitlines() if "候选窗" in l), "")
check("只弹过提示时，面板不许说「候选窗 显示过 2 次」",
      "显示过 2 次 · 最近" not in cand and "还没显示过" in cand, cand)
check("只弹过提示时，面板要说明那 2 次都是提示、不是候选窗",
      "2 条都是提示" in cand and "不是候选窗" in cand, cand)

text = panel_text(overlay={"shows": 5, "cand_shows": 2, "rect": [10, 20, 300, 100],
                           "on_monitor": True, "error": None})
check("候选窗 2 次 + 3 条提示：两个数字分开报",
      "显示过 2 次" in text and "另有 3 条提示" in text, text)

# ---- 「我敲的键到底进没进钩子」：这一行是「打字没反应」的分诊台 ----
text = panel_text(hook=False)
check("用 --no-hook 起的时候面板会说明白", "没安装" in text and "打字不会有反应" in text)

text = panel_text(hook=True, hook_info={"real": 0, "self_injected": 3,
                                        "foreign_injected": 0, "swallowed": 0,
                                        "alive": True})
check("钩子装着却没收到过真实按键 → 面板直指故障点（措辞留了余地）",
      "还没收到过真实按键" in text and "3 个我们自己注入的按键" in text
      and "如果你已经敲过字" in text)

# 本机最容易被忽略的一种「打字没反应」：改键器/宏/软键盘转发按键。
# 这类事件带 INJECTED 标志，旧版本一律丢弃 → 打字完全没反应。
text = panel_text(hook=True, hook_info={"real": 0, "self_injected": 0,
                                        "foreign_injected": 4, "swallowed": 4,
                                        "alive": True, "last_effective_age": 1.0})
check("只有「别的程序转发的」按键时，面板说明它已被当成真实按键处理",
      "收到过 4 个有效按键" in text and "别的程序转发" in text
      and "已按真实按键处理" in text)

text = panel_text(hook=True, hook_info={"real": 128, "self_injected": 2,
                                        "foreign_injected": 0, "swallowed": 120,
                                        "alive": True, "last_effective_age": 2.0})
check("收到过真实按键 → 面板报个数、最近时刻、吞掉多少",
      "收到过 128 个有效按键" in text and "刚刚" in text and "吞掉 120 个" in text
      and "2 个是我们自己注入的" in text)

text = panel_text(hook=True, hook_info={"real": 5, "self_injected": 0,
                                        "foreign_injected": 0, "swallowed": 5,
                                        "alive": False, "last_real_age": 99.0})
check("钩子线程没了的时候面板会警告",
      "钩子线程已经没了" in text and "99 秒前" in text)

text = panel_text(hook=True, hook_info={"real": 0, "error": "SetWindowsHookExW 失败，GetLastError=5"})
check("钩子装不上时报出原始错误",
      "安装失败" in text and "GetLastError=5" in text)

# ---- 第 107 轮：安装失败 / --no-hook 时，面板不许再教「敲几个拼音字母」----
# 钩子没装上时字母根本进不了程序，那句建议是**不可能生效**的；而状态文件以前
# 把「安装失败」也写成 hook: True，于是面板说「敲几个字母」，用户敲到怀疑键盘。
text = panel_text(hook=False, hook_info={"error": "SetWindowsHookExW 失败，GetLastError=5",
                                         "disabled_by_flag": False})
cand_line = next((l.strip() for l in text.splitlines() if "候选窗" in l), "")
check("安装失败时面板说「安装失败」并交代打字没反应",
      "安装失败" in text and "打字不会有反应" in text)
check("安装失败时候选窗那行说「不会出现」",
      "不会出现" in cand_line, cand_line)
check("安装失败时不再教用户「敲几个拼音字母」",
      "敲几个拼音字母" not in cand_line, cand_line)

text = panel_text(hook=False, hook_info={"error": None, "disabled_by_flag": True})
cand_line = next((l.strip() for l in text.splitlines() if "候选窗" in l), "")
check("--no-hook 起的时候说清是用户自己关的",
      "--no-hook" in text and "打字不会有反应" in text, cand_line)
check("--no-hook 时候选窗那行给出去处（去掉 --no-hook 或 --demo）",
      "不会出现" in cand_line and "--demo" in cand_line, cand_line)

# ---- 开机自启：只说「已开启」不够，得问它指向的东西还在不在（第 59 轮）----
real_auto_status, real_auto_problem = daemon.autostart_status, daemon.autostart_problem
try:
    daemon.autostart_status = lambda: "registry"
    daemon.autostart_problem = lambda root: (
        "记下的 pythonw.exe 已经不在了"
        "（C:\\definitely-not-here\\pythonw.exe）—— 开机时它启动不起来")
    text = panel_text()
    check("自启项指向的 exe 没了时，面板不会只报「已开启」",
          "已开启（注册表）" in text and "但它不会生效" in text
          and "已经不在了" in text)

    daemon.autostart_problem = lambda root: ""
    text = panel_text()
    check("自启项没问题时面板不多话",
          "已开启（注册表）" in text and "不会生效" not in text)
finally:
    daemon.autostart_status = real_auto_status     # type: ignore[assignment]
    daemon.autostart_problem = real_auto_problem   # type: ignore[assignment]

# ---- 离线有两种原因：没配 key，还是你自己选了 mock（第 62 轮）----
# 第 55 轮让 api.provider 真的起作用之后，面板这句「没配 API key」就会
# 冤枉配了 key 的人 —— 用户会去反复检查那个已经填好的 key。
text = panel_text(translate={"mock": True, "provider": "mock",
                             "mock_reason": "api.provider=mock，你主动选的离线词表"})
check("强制离线时面板说清是「你主动选的」，不再冤枉「没配 API key」",
      "api.provider=mock" in text and "没配 API key" not in text,
      next((ln.strip() for ln in text.splitlines() if "翻译" in ln), ""))

text = panel_text(translate={"mock": True, "provider": "mock",
                             "mock_reason": "没配 API key"})
check("被动离线时面板如实说「没配 API key」", "没配 API key" in text,
      next((ln.strip() for ln in text.splitlines() if "翻译" in ln), ""))

text = panel_text(translate={"mock": False, "provider": "deepseek", "mock_reason": ""})
check("在线时面板报的是在线 provider", "在线 deepseek" in text,
      next((ln.strip() for ln in text.splitlines() if "翻译" in ln), ""))

# ---- 翻译是用户自己关的：别拿「没配 key」解释一件没发生的事（第 78 轮）----
text = panel_text(translate={"enabled": False, "mock": True, "provider": "mock",
                             "mock_reason": "没配 API key"})
_tr_line = next((ln.strip() for ln in text.splitlines() if "翻译" in ln), "")
check("translate.enabled=false 时面板说「已关闭」",
      "已关闭" in _tr_line and "translate.enabled=false" in _tr_line, _tr_line)
check("并且不再说「离线词表（没配 API key）」", "离线词表" not in _tr_line, _tr_line)

# ---- 翻译失败的原因也要按种类说人话（第 117 轮）----
# 以前面板把翻译层的错误原文整段照抄：「翻译请求失败：<urlopen error …
# 由于目标计算机积极拒绝…>」。用户看到这个会去翻 config.json，可配置没错，
# 只是这台机器没网。现在状态里带 last_error_kind，面板先说人话、原文放后面。
text = panel_text(translate={"mock": False, "provider": "deepseek", "requests": 3,
                             "errors": 2, "last_error": "翻译请求失败：<urlopen error timed out>",
                             "last_error_kind": "network", "last_error_status": None})
_tr_line = next((ln.strip() for ln in text.splitlines() if "翻译" in ln), "")
check("★ 没网时面板说「不是配置写错」", "不是配置写错" in _tr_line, _tr_line)
check("★ 没网时面板说「连不上网 / 被防火墙拦了」", "连不上网" in _tr_line, _tr_line)
check("原文也还在（要能拿去贴给作者）", "urlopen error timed out" in _tr_line, _tr_line)

text = panel_text(translate={"mock": False, "provider": "deepseek", "requests": 3,
                             "errors": 1, "last_error": "翻译请求失败：HTTP 401 …",
                             "last_error_kind": "http", "last_error_status": 401})
_tr_line = next((ln.strip() for ln in text.splitlines() if "翻译" in ln), "")
check("★ key 错时面板说「服务器拒绝 HTTP 401」",
      "服务器拒绝" in _tr_line and "HTTP 401" in _tr_line, _tr_line)
check("★ key 错时才把用户指向 key / 额度 / 模型名",
      "API key" in _tr_line or "key / 额度" in _tr_line, _tr_line)

# 旧状态文件里没有 last_error_kind：不能因此崩掉或丢原文，退回原来的说法。
text = panel_text(translate={"mock": False, "provider": "deepseek", "requests": 1,
                             "errors": 1, "last_error": "boom"})
_tr_line = next((ln.strip() for ln in text.splitlines() if "翻译" in ln), "")
check("旧状态没有 last_error_kind 时退回「最近：原文」",
      "最近：boom" in _tr_line, _tr_line)

# ---- 实例用的配置可能不是面板读的这一份（第 60 轮）----
# 面板读的是本目录 config.json；实例可能是带 --config 另一份文件起来的。
# 不说清楚的话，面板上的热键、上屏语言会被当成实例的现状。
other_cfg = ROOT / ".tmp" / "panel_other_config.json"
text = panel_text(config=str(other_cfg))
check("实例用的是另一份配置时，面板把「实例配置」摆出来",
      "实例配置" in text and str(other_cfg) in text
      and "不是本目录的 config.json" in text,
      next((ln.strip() for ln in text.splitlines() if "实例配置" in ln), ""))

text = panel_text(config=str(ROOT / "config.json"))
check("实例用的就是本目录 config.json 时，面板不多一句「另一份」",
      "不是本目录的 config.json" not in text)

text = panel_text()
check("状态里没报配置路径时，面板说「可能是旧版本」", "可能是旧版本" in text)

# 第 65 轮：面板要认**本次命令行真正加载的那份**配置（--config）。
# 以前它写死 root/config.json，于是用户带 --config 再双击一次时，面板上面
# 刚打印「配置来自 alt.json」，下面又指着 config.json 说「配置文件」，
# 连「几个选项没写」都是在数那份没在用的文件。
explicit_cfg = ROOT / ".tmp" / "panel_explicit_config.json"
explicit_cfg.parent.mkdir(parents=True, exist_ok=True)
explicit_cfg.write_text(json.dumps({"hotkeys": {"quit": "ctrl+alt+f9"}},
                                   ensure_ascii=False), encoding="utf-8")
text = panel_text(config=str(explicit_cfg), cfg_path=explicit_cfg)
check("带 --config 时，面板的「配置文件」指的是那一份",
      f"配置文件   {explicit_cfg}" in text
      and f"配置文件   {ROOT / 'config.json'}" not in text,
      next((ln.strip() for ln in text.splitlines() if "配置文件" in ln), ""))
check("带 --config 且实例也用同一份时，不再警告「不是本目录的」",
      "不是本目录的 config.json" not in text)
check("「几个选项没写」按实例真正读的那份算（这里只剩默认项）",
      "个选项没写" in text,
      next((ln.strip() for ln in text.splitlines() if "选项没写" in ln), ""))

text = panel_text(cfg_path=None)
check("没带 --config 时，面板的「配置文件」还是本目录 config.json",
      f"配置文件   {ROOT / 'config.json'}" in text,
      next((ln.strip() for ln in text.splitlines() if "配置文件" in ln), ""))

explicit_cfg.unlink(missing_ok=True)


# 非交互控制台里那句「想关掉它：按 X」必须用**实例自己报的键**：
# 面板这份配置的 quit 改了没改都跟那个实例无关，按错了就等于没有退出热键。
def non_tty_text(**over) -> str:
    status = dict(PANEL_BASE)
    status.update(over)
    daemon.write_status(ROOT, status)

    class NotATty:
        def isatty(self) -> bool:
            return False

    buf = io.StringIO()
    real_stdin = sys.stdin
    sys.stdin = NotATty()                       # type: ignore[assignment]
    try:
        with contextlib.redirect_stdout(buf):
            rc = main_mod.run_control_panel(cfg, ROOT, W)
    finally:
        sys.stdin = real_stdin                  # type: ignore[assignment]
    check("非交互控制台返回 3", rc == 3, f"rc={rc}")
    return buf.getvalue()


text = non_tty_text(quit_hotkey="Ctrl+Alt+F9")
check("非交互面板按实例报的退出键提示（不是面板那份配置的键）",
      "Ctrl+Alt+F9" in text and "Ctrl+Alt+Q" not in text,
      next((ln.strip() for ln in text.splitlines() if "想关掉它" in ln), ""))

text = non_tty_text(quit_hotkey="")
check("实例没报退出键时，退回面板这份配置的键",
      "Ctrl+Alt+Q" in text,
      next((ln.strip() for ln in text.splitlines() if "想关掉它" in ln), ""))

text = non_tty_text(quit_hotkey="（没设热键）")
check("实例明确没有退出热键时，改说托盘 / 任务管理器",
      "任务管理器" in text and "按 （没设热键）" not in text,
      next((ln.strip() for ln in text.splitlines() if "想关掉它" in ln), ""))

# 第 82 轮：托盘关掉的时候，非交互提示也得说清是「配置里关的」。
text = non_tty_text(quit_hotkey="（没设热键）", tray_enabled=False)
check("托盘是配置里关掉的：非交互提示也不许说「装不上 / 完整性级别」",
      "output.tray=false" in text and "完整性级别低于 explorer" not in text
      and "托盘图标也没装上" not in text,
      next((ln.strip() for ln in text.splitlines() if "托盘" in ln), ""))

daemon.clear_status(ROOT)

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  5b. 自学习那行：文件名跟着配置走，写不进去要当场说")
print("=" * 72)
# 第 81 轮：boost_file 指到没权限 / 建不出来的地方时，面板以前只说「最多 5 秒后落盘」，
# 用户永远等不到那句「已写入」却不知道哪里错了。而且文件名写死 user_boosts.json，
# 改过 output.boost_file 的人看到的是一句假话。
text = panel_text(boosts=3, boost_saves=1, boost_file=r"D:\somewhere\my_boosts.json",
                  boost_error=None)
check("落盘成功后显示的是配置里的文件名（不是写死的 user_boosts.json）",
      "my_boosts.json" in text and "user_boosts.json" not in text,
      next((ln.strip() for ln in text.splitlines() if "自学习" in ln), ""))

text = panel_text(boosts=3, boost_saves=0, boost_file=r"D:\somewhere\my_boosts.json",
                  boost_error="PermissionError: [Errno 13] Permission denied")
check("★ 写不进去时面板直接说原因",
      "写不进" in text and "PermissionError" in text,
      next((ln.strip() for ln in text.splitlines() if "自学习" in ln), ""))
check("写不进去时不再说「最多 5 秒后落盘」（那句会让人一直等）",
      "最多 5 秒后落盘" not in text,
      next((ln.strip() for ln in text.splitlines() if "自学习" in ln), ""))

# 第 87 轮：权重文件本身读不懂（少个引号之类）。以前面板只说「还没记住任何词」，
# 用户以为自学习还没开始；实际上他那份文件里的词一个都没生效，而且下次保存就覆盖掉。
text = panel_text(boosts=0, boost_saves=0, boost_file="user_boosts.json",
                  boost_error=None,
                  boost_load_error="JSONDecodeError: Expecting property name enclosed in double quotes")
_check_line = next((ln.strip() for ln in text.splitlines() if "自学习" in ln), "")
check("★ 权重文件读不了时面板说「读不了」+ 原因，不说「还没记住任何词」",
      "读不了" in _check_line and "JSONDecodeError" in _check_line
      and "还没记住" not in _check_line, _check_line)
check("★ 而且告诉用户已经留了 .broken 备份（词没被覆盖）",
      ".broken" in _check_line, _check_line)

text = panel_text(boosts=12, boost_saves=2, boost_file="user_boosts.json",
                  boost_error=None,
                  boost_load_error="JSONDecodeError: x")
_check_line = next((ln.strip() for ln in text.splitlines() if "自学习" in ln), "")
check("读坏之后新记的词也算数（说「已记住 12 个词」，但点明这份是新的）",
      "12" in _check_line and "新的" in _check_line and "读不了" in _check_line, _check_line)

daemon.clear_status(ROOT)

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print(f"  结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
for p in PROBLEMS:
    print(f"  ! {p}")
sys.exit(1 if FAIL else 0)
