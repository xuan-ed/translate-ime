# -*- coding: utf-8 -*-
"""``--check`` 自己说的话，到底有几句真的验过？

为什么单独一个测试：``--check`` 是用户「双击没反应 / 打字没候选」时第一个会去敲的
命令，它打印的「全部通过 ✅」被当成结论拿去修别的东西。第 53 轮实测发现它的窗口层
一项是**假的**：只建了一个空白的 ``tk.Tk()``，然后无条件打一行
``[ok ] 候选窗可构造 (未显示)`` —— 而 ``App`` 启动时建的是 ``Overlay(...)``
（``t_ime/app.py:123``），窗口层真坏掉时 ``--check`` 照样全绿，程序却连候选窗都起不来。
这个测试盯着这条结论一共三件事：

  1. ``--check`` 必须真的把候选窗建一遍（输出里要有多大的数字、多少缩放比）；
  2. 候选窗构造失败时它必须 FAIL、退出码 1，并把失败原因和一句人话一起说；
  3. 缺字体这种「能用但难看」的情况不该让自检变红（会回退字体，只是 info）。

直接跑：``python tests/test_check.py``（约 15 秒，中间会短暂建一个不显示的候选窗）
"""
import contextlib
import copy
import io
import json
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

import main  # noqa: E402
import t_ime.overlay as overlay_mod  # noqa: E402
from t_ime import config as C  # noqa: E402
from t_ime import daemon as D  # noqa: E402
from t_ime import winapi as W  # noqa: E402

# 开始跑测试之前有没有实例在跑。第 118 轮：用户自己双击 run.bat 起的实例会让
# 「没留下状态文件 / 没有偷偷起一个常驻实例」这两条断言变成假故障（它又不是本
# 测试起的）。有实例时如实说一句，然后把这两条跳过，别去动用户的实例。
PRE_RUNNING = D.already_running()

OK = "[OK]  "
BAD = "[FAIL]"
results: list[tuple[bool, str]] = []


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"   {detail}" if detail else ""))
    return cond


def fresh_cfg():
    return copy.deepcopy(C.DEFAULTS)


def run_check(cfg):
    """在进程内跑一次 run_check，收走它的 stdout；返回 (rc, 文本, 秒数)。"""
    buf = io.StringIO()
    t0 = time.time()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, PROJ, W)
    return rc, buf.getvalue(), time.time() - t0


STATUS = PROJ / "translate-ime.status.json"


# ============================================================
print("=" * 74)
print("1. 候选窗构造失败时，--check 必须红（这条是本轮的主角）")
print("=" * 74)


class BoomOverlay:
    """假装 Tk 起不来：App 到构造候选窗那一步就会这么炸。"""

    def __init__(self, *a, **k):
        raise RuntimeError("模拟：Tk 窗口创不出来")


real_overlay = overlay_mod.Overlay
overlay_mod.Overlay = BoomOverlay
try:
    rc, text, _t = run_check(fresh_cfg())
finally:
    overlay_mod.Overlay = real_overlay

check(rc == 1, "候选窗炸掉时退出码是 1", f"rc={rc}")
check("[FAIL] 候选窗构造" in text, "输出里有 [FAIL] 候选窗构造")
check("模拟：Tk 窗口创不出来" in text, "把真实失败原因打了出来",
      "RuntimeError('模拟：Tk 窗口创不出来')")
check("候选窗起不来" in text, "还写了一句人话（候选窗起不来、打字看不到候选）")
check("有项目未通过" in text, "结尾是「有项目未通过」，不是「全部通过」")

# ============================================================
print()
print("=" * 74)
print("2. 候选窗真建出来：报告真实尺寸与缩放比，而不是「(未显示)」")
print("=" * 74)

rc, text, seconds = run_check(fresh_cfg())
m = re.search(r"\[ok \] 候选窗构造\s+(\d+)x(\d+) 像素 @([\d.]+)x", text)
check(rc == 0, "--check 正常情况退出码 0", f"rc={rc}, {seconds:.1f}s")
check(m is not None, "候选窗那一行带真实尺寸与缩放比", m.group(0) if m else "没匹配到")
if m:
    w, h = int(m.group(1)), int(m.group(2))
    check(w > 0 and h > 0, "尺寸是正的", f"{w}x{h}")
    check(abs(float(m.group(3)) - 1.5) < 0.6, "缩放比是个合理的数", m.group(3))
check("(未显示)" not in text, "不再有那句「(未显示)」的假话")
check("全部通过" in text, "结尾仍然是「全部通过」")
check("Traceback" not in text, "没有 traceback 甩到用户脸上")

# ============================================================
print()
print("=" * 74)
print("2b. 系统输入法：语言栏里没有本工具是设计，--check 得主动说出来")
print("=" * 74)
# 用户的第一反应是「我在语言栏里选不到它，所以没装好」。——check 自己一个字
# 不提，就等于把这种误解留在那儿。
check("== 系统输入法 ==" in text, "输出里有「系统输入法」一节")
check("语言栏 / Win+Space 里不会出现它" in text,
      "明说本工具不是系统输入法、语言栏里不会出现")
check(("系统里装着的输入法" in text) or ("读不出" in text),
      "要么列出系统里的输入法，要么如实说读不出来")
check("不需要选中" in text, "说清楚「不需要选中，启动即接管」")

from t_ime import winapi as _wa

_layouts = _wa.installed_input_layouts()
check(isinstance(_layouts, list), "installed_input_layouts() 返回列表")
check(all(isinstance(x, tuple) and len(x) == 2 for x in _layouts),
      "每一项都是 (布局 id, 名字)", f"{_layouts[:3]}")
check(all(isinstance(a, str) and isinstance(b, str) for a, b in _layouts),
      "id 与名字都是字符串")

# ============================================================
print()
print("=" * 74)
print("2c. 「你在用哪个 python」要写在报告第一屏（第 118 轮）")
print("=" * 74)
# 用户照文档敲 `python main.py --check`，命中的是**系统装的** Python（项目依赖却在
# `.venv` 里），于是 jieba / pypinyin 全「没装」、索引建不出来 —— 满屏 FAIL，可
# `run.bat` 起的那份实例好好地在跑。检查本身没错，缺的是「这台解释器是谁」这一行。
from t_ime import pinyin as _pinyin_mod  # noqa: E402

_interp_ok, _interp_detail = main._interpreter_note(PROJ)
check(_interp_ok, "测试进程自己就是 .venv 的 python（基线）", _interp_detail)
check(".venv" in _interp_detail, "解释器这一行写的是 .venv 里的路径", _interp_detail)
check("[info] 解释器" in text, "--check 输出里有「解释器」这一行")

_orig_exe = sys.executable
_orig_missing = _pinyin_mod.missing_dependencies
try:
    sys.executable = str(PROJ / "some-other-python" / "python.exe")
    _pinyin_mod.missing_dependencies = lambda: ["pypinyin"]  # type: ignore[assignment]
    _ok2, _detail2 = main._interpreter_note(PROJ)
finally:
    sys.executable = _orig_exe
    _pinyin_mod.missing_dependencies = _orig_missing  # type: ignore[assignment]
check(_ok2 is False, "不是 .venv 的 python + 缺依赖 → 判失败")
check("不是项目 .venv 里的 python" in _detail2, "说清楚「不是项目 .venv 里的 python」", _detail2)
check("缺：pypinyin" in _detail2, "把缺的依赖列出来（而不是只说「没装」）", _detail2)
check("run.bat --check" in _detail2, "给一条能直接照抄的命令", _detail2)

# ============================================================
print()
print("=" * 74)
print("3. 字体没装：只是回退，不该让自检变红")
print("=" * 74)

cfg = fresh_cfg()
cfg["overlay"]["font_family"] = "NoSuchFont XYZ"
rc, text, _t = run_check(cfg)
check(rc == 0, "缺字体时退出码仍是 0", f"rc={rc}")
check("没找到" in text and "回退字体" in text, "如实说了没找到、会用回退字体")
check("全部通过" in text, "结尾是全部通过")

# ============================================================
print()
print("=" * 74)
print("4. 热键写坏，--check 也拦得住（热键那一项不是摆设）")
print("=" * 74)

cfg = fresh_cfg()
cfg["hotkeys"]["toggle_default"] = "ctrl+alt+"
rc, text, _t = run_check(cfg)
check(rc == 1, "热键解析失败时退出码 1", f"rc={rc}")
check("[FAIL] toggle_default" in text, "点到了写坏的那条热键")

# ============================================================
print()
print("=" * 74)
print("4c. 热键「能解析」不等于「按了有用」：动作名拼错 / 撞车也要拦")
print("=" * 74)

# 第 63 轮：真实例会打印「热键动作名 'quitt' 不认识，已忽略」并且只让先出现的
# 那个动作生效，可 --check 以前对这两种配置都是「全部通过 ✅」。
cfg = fresh_cfg()
cfg["hotkeys"]["quitt"] = "ctrl+alt+f8"
rc, text, _t = run_check(cfg)
check(rc == 1, "动作名拼错时退出码 1", f"rc={rc}")
check("[FAIL] quitt" in text, "点到了拼错的那个动作", "\n".join(
    ln for ln in text.splitlines() if "quitt" in ln))
check("不认识" in text and "compose" in text,
      "说清动作名不认识、并列出支持的动作")

cfg = fresh_cfg()
cfg["hotkeys"]["toggle_enabled"] = cfg["hotkeys"]["quit"]
rc, text, _t = run_check(cfg)
check(rc == 1, "两个动作撞同一个组合键时退出码 1", f"rc={rc}")
check("[FAIL] toggle_enabled" in text or "[FAIL] quit" in text,
      "点到了撞车的那条", "\n".join(ln for ln in text.splitlines()
                                 if "绑了同一个组合键" in ln))
check("绑了同一个组合键" in text and "实际生效的是" in text,
      "说明谁真的生效、谁用不了")

# ============================================================
print()
print("=" * 74)
print("4b. 翻译引擎：离线原因要分清「没配 key」和「你自己选了 mock」")
print("=" * 74)

# 第 62 轮：第 55 轮让 api.provider 真的起作用之后，「离线」有两种原因。
# 以前 --check 对两种都喊「未检测到 API key」—— 配了 key 的人会去反复检查
# 那个已经填好的 key，而真正该改的是 api.provider。
cfg = fresh_cfg()
cfg["api"]["provider"] = "mock"
cfg["api"]["api_key"] = "sk-check-test"
rc, text, _t = run_check(cfg)
check(rc == 0, "forced mock 时 --check 仍然通过（离线不是故障）", f"rc={rc}")
tr_line = next((ln.strip() for ln in text.splitlines() if "翻译走离线词表" in ln), "")
check("api.provider=mock" in tr_line, "说清「是你主动选的离线」", tr_line)
check("未检测到 API key" not in text, "不再冤枉「未检测到 API key」")
check("想联网翻译就把 api.provider 改回" in text,
      "给出对得上的修法（改 provider，而不是填 key）")

cfg = fresh_cfg()
cfg["api"]["provider"] = "deepseek"
cfg["api"]["api_key"] = ""
rc, text, _t = run_check(cfg)
check("没配 key 时仍然说「没配 API key」", "翻译走离线词表：没配 API key" in text,
      next((ln.strip() for ln in text.splitlines() if "翻译走离线词表" in ln), ""))
check("并且给出填 key 的提示", "DEEPSEEK_API_KEY" in text)

# 第 78 轮：translate.enabled=false 是用户自己关掉的合法配置，不是故障。
# 以前这里照样跑往返、拿到 []，判 [FAIL] 并说「连失败原因都没记下来，看日志」——
# 一句正确配置被诊断成故障，还让人去翻一份根本没写错的日志。
cfg = fresh_cfg()
cfg["translate"]["enabled"] = False
rc, text, _t = run_check(cfg)
check(rc == 0, "关掉翻译时 --check 仍然通过（这不是故障）", f"rc={rc}")
check("[FAIL]" not in text, "关掉翻译时输出里没有 [FAIL]")
check("跳过：translate.enabled=false" in text, "明说翻译往返被跳过，而不是假装失败",
      next((ln.strip() for ln in text.splitlines() if "翻译往返" in ln), ""))
check("连失败原因都没记下来" not in text,
      "不再冤枉「连失败原因都没记下来」（真实原因就是开关关着）")
check("翻译已关闭" in text, "引擎那行先说清「翻译已关闭」", 
      next((ln.strip() for ln in text.splitlines() if "翻译已关闭" in ln), ""))

# ============================================================
print()
print("=" * 74)
print("4d. 空热键：Shift 兜底只对切模式的动作成立，别一视同仁")
print("=" * 74)

# 第 72 轮：hotkeys 空着是合法的关掉方式，但每个动作的退路不一样 ——
# Shift 单击只管「拼音 ⇄ 英文直通」（而且 input.shift_tap_toggles 必须开着），
# 旧版本对 quit / compose / toggle_enabled 也写「（未绑定，靠点一下 Shift）」，
# 等于教用户去按一个没用的键。
cfg = fresh_cfg()
cfg["hotkeys"].update({"quit": "", "compose": "", "toggle_default": "",
                       "toggle_direct": "", "toggle_enabled": ""})
cfg.setdefault("input", {})["shift_tap_toggles"] = True
rc, text, _t = run_check(cfg)
check(rc == 0, "热键全空时 --check 仍然通过（这是合法的关掉方式）", f"rc={rc}")


def _unbound(text: str) -> dict[str, str]:
    """把「（未绑定 …）」那几行按动作名收集起来。"""
    got: dict[str, str] = {}
    for ln in text.splitlines():
        if "（未绑定" in ln and ln.strip().startswith("[ok ]"):
            parts = ln.split()
            if len(parts) >= 2:
                got[parts[1]] = ln.strip()
    return got


lines = _unbound(text)
quit_line = lines.get("quit", "")
compose_line = lines.get("compose", "")
tog_line = lines.get("toggle_default", "")
check("空热键仍然逐条列出来（不静默跳过）",
      bool(quit_line and compose_line and tog_line), sorted(lines))
check("quit 空着时不提 Shift（Shift 退不了程序）",
      bool(quit_line) and "Shift" not in quit_line, quit_line)
check("compose 空着时不提 Shift（Shift 不翻译）",
      bool(compose_line) and "Shift" not in compose_line, compose_line)
# 第 92 轮：toggle_default 切的是「空格上屏中文还是译文」，Shift 单击切的是
# 英文直通 —— 两件事。以前这里也写「可以点一下 Shift」，用户按下去得到的是
# 直通，不是译文。
check("toggle_default 空着时不许说「点一下 Shift」（Shift 只管英文直通）",
      bool(tog_line) and "Shift" not in tog_line, tog_line)
check("toggle_default 空着时要指到真能用的入口（托盘 / 面板菜单）",
      "托盘" in tog_line and "面板菜单" in tog_line, tog_line)
togg_line = lines.get("toggle_direct", "")
check("toggle_direct 空着、Shift 单击开着 → 才说「可以点一下 Shift」",
      "点一下 Shift" in togg_line, togg_line)

cfg = fresh_cfg()
cfg["hotkeys"].update({"quit": "", "compose": "", "toggle_default": "",
                       "toggle_direct": "", "toggle_enabled": ""})
cfg.setdefault("input", {})["shift_tap_toggles"] = False
rc, text, _t = run_check(cfg)
lines = _unbound(text)
tog_line = lines.get("toggle_default", "")
togg_line = lines.get("toggle_direct", "")
check("Shift 单击关掉之后，toggle_direct 空着也不再说点它有用",
      bool(togg_line) and "Shift" not in togg_line, togg_line)
check("并且明说「这个动作没有快捷键了」", "没有快捷键" in togg_line, togg_line)
check("toggle_default 这时候仍然指到托盘 / 面板菜单（跟 Shift 无关）",
      "托盘" in tog_line and "面板菜单" in tog_line, tog_line)

# ============================================================
print()
print("=" * 74)
print("4e. 没有修饰键的打字键热键：打字打到它就触发，--check 必须红")
print("=" * 74)

# 第 77 轮真机：hotkeys.quit = "q" 时 --check 判 [ok]（`q -> mods=[] vk=81`），
# 而真实例打字打到 q 就退出了；banner / 状态里还在播报「按 Q 退出」。
cfg = fresh_cfg()
cfg["hotkeys"]["quit"] = "q"
rc, text, _t = run_check(cfg)
check(rc == 1, "quit 配成裸字母 q 时 --check 退出码 1（不能报「全部通过」）", f"rc={rc}")
_quit_lines = [ln for ln in text.splitlines() if "quit" in ln and ("[FAIL]" in ln or "[ok ]" in ln)]
_quit_line = _quit_lines[0] if _quit_lines else ""
check("quit 那行是 FAIL", "[FAIL]" in _quit_line, _quit_line)
check("说明了原因：没有修饰键", "没有修饰键" in _quit_line, _quit_line)
check("说清了打字时会误触", "打字时按到" in _quit_line, _quit_line)

cfg = fresh_cfg()
cfg["hotkeys"]["toggle_enabled"] = "f8"      # F 键当单键热键是合理的，不该报
rc, text, _t = run_check(cfg)
check(rc == 0, "F8 这种打字时不碰的单键热键不误报", f"rc={rc}")

# 第 84 轮真机：shift+q —— Shift 单独出现就是「打大写字母」的标准方式，
# 真实例里打一个大写 Q 就把输入法退了，而 --check 判 [ok]。
cfg = fresh_cfg()
cfg["hotkeys"]["quit"] = "shift+q"
rc, text, _t = run_check(cfg)
_quit_lines = [ln for ln in text.splitlines() if "quit" in ln and ("[FAIL]" in ln or "[ok ]" in ln)]
_quit_line = _quit_lines[0] if _quit_lines else ""
check(rc == 1, "quit 配成 shift+q 时 --check 退出码 1", f"rc={rc}")
check("说清了「只有 Shift 一个修饰键」", "只有 Shift" in _quit_line, _quit_line)
check("说清了打大写字母时会误触", "打大写字母" in _quit_line, _quit_line)

cfg = fresh_cfg()
cfg["hotkeys"]["quit"] = "ctrl+shift+q"      # 加上 ctrl 就正常了
rc, _text, _t = run_check(cfg)
check(rc == 0, "ctrl+shift+q 不误报", f"rc={rc}")

# 一个串里写两个主键：以前悄悄只注册最后一个，播报却照抄原文。
cfg = fresh_cfg()
cfg["hotkeys"]["quit"] = "ctrl+q+w"
rc, text, _t = run_check(cfg)
_quit_lines = [ln for ln in text.splitlines() if "quit" in ln and ("[FAIL]" in ln or "[ok ]" in ln)]
_quit_line = _quit_lines[0] if _quit_lines else ""
check(rc == 1, "quit 配成 ctrl+q+w 时 --check 退出码 1", f"rc={rc}")
check("点出「两个主键」", "两个主键" in _quit_line, _quit_line)

# ============================================================
print()
print("=" * 74)
print("4f. 索引一个键都没有时：--check 不许报「全部通过」")
print("=" * 74)

# 第 79 轮真机：把 pinyin.min_freq 设大（比如 100000000）后索引是 0 个键，
# --check 照样打 `[ok ] 索引可用 0 键`、最后一行「全部通过 ✅」、
# 而打字一个候选都没有 —— 中间那 5 条候选词检查整段静默消失，连一句「跳过了」都没有。
# 这比「报错」更坏：用户拿到的是「自检说一切正常」，于是去怀疑键盘。
cfg = fresh_cfg()
cfg["pinyin"]["min_freq"] = 100000000
_cold_root = PROJ / ".tmp" / "check-empty-idx"
shutil.rmtree(_cold_root, ignore_errors=True)
_cold_root.mkdir(parents=True, exist_ok=True)
try:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _cold_root, W)
    text = buf.getvalue()
    check(rc == 1, "索引 0 键时 --check 退出码 1", f"rc={rc}")
    idx_line = next((ln.strip() for ln in text.splitlines() if "索引可用" in ln), "")
    check("[FAIL]" in idx_line, "索引可用那行是 FAIL，不是 [ok ]", idx_line)
    check("0 键" in idx_line and "min_freq" in idx_line,
          "说出「0 键」和嫌疑参数 min_freq", idx_line)
    skip_line = next((ln.strip() for ln in text.splitlines() if "候选词测试" in ln), "")
    check("跳过" in skip_line and "索引是空的" in skip_line,
          "候选词检查明说「跳过：索引是空的」，不是静默消失", skip_line)
    check("全部通过" not in text, "输出里没有「全部通过 ✅」")
finally:
    shutil.rmtree(_cold_root, ignore_errors=True)

# ============================================================
print()
print("=" * 74)
print("4g. 自学习权重：目录不在要说「实例会自己建」，写不进去要判 FAIL，检查本身不许造文件")
print("=" * 74)

# 第 81 轮真机：``"boost_file": ".tmp/r81/missing/boosts.json"`` 时实例每个心跳写失败一次、
# 日志 3 秒涨 1.1 KB、自学习一条也没落盘，而 --check 里连「自学习」三个字都没有。
_cold_root = PROJ / ".tmp" / "check-boost"
shutil.rmtree(_cold_root, ignore_errors=True)
_cold_root.mkdir(parents=True, exist_ok=True)
try:
    cfg = fresh_cfg()
    cfg["output"]["boost_file"] = "nested/data/boosts.json"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _cold_root, W)
    text = buf.getvalue()
    check("有「== 自学习权重 ==」这一节", "== 自学习权重 ==" in text)
    line_txt = next((ln.strip() for ln in text.splitlines() if "自学习权重" in ln and
                     ("[ok ]" in ln or "[info]" in ln or "[FAIL]" in ln)), "")
    check("父目录不存在时是 [info] 且说「目录还不存在」",
          "[info]" in line_txt and "目录还不存在" in line_txt, line_txt)
    check("★ 而且没有真去建那个目录（检查不该有副作用）",
          not (_cold_root / "nested" / "data").exists(), str(_cold_root / "nested"))
    check("这一项不影响退出码", rc == 0, f"rc={rc}")

    # 默认路径：目录在、文件还没生成 → [ok ] 目录可写，且不留下文件
    cfg = fresh_cfg()
    cfg["output"]["boost_file"] = "user_boosts.json"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _cold_root, W)
    text = buf.getvalue()
    line_txt = next((ln.strip() for ln in text.splitlines() if "自学习权重" in ln and
                     ("[ok ]" in ln or "[info]" in ln or "[FAIL]" in ln)), "")
    check("默认路径时是 [ok ] 且说「目录可写，文件还没生成」",
          "[ok ]" in line_txt and "目录可写" in line_txt, line_txt)
    check("探针文件写完就删了（没留下 0 字节垃圾）",
          not (_cold_root / "user_boosts.json").exists()
          and not list(_cold_root.glob("*.check-*")), list(_cold_root.iterdir()))

    # 父路径被一个文件占住：真写不进去 → FAIL + 原因
    (_cold_root / "blocked").write_text("我是个文件", encoding="utf-8")
    cfg = fresh_cfg()
    cfg["output"]["boost_file"] = "blocked/boosts.json"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _cold_root, W)
    text = buf.getvalue()
    line_txt = next((ln.strip() for ln in text.splitlines() if "自学习权重" in ln and
                     ("[ok ]" in ln or "[info]" in ln or "[FAIL]" in ln)), "")
    check("★ 写不进去时判 [FAIL] 并说清原因",
          "[FAIL]" in line_txt and "写不进去" in line_txt, line_txt)
    check("写不进去时退出码是 1", rc == 1, f"rc={rc}")

    # 文件在那儿、但里面读不懂：这也是「写了不生效」，必须判 FAIL（第 87 轮真机：
    # user_boosts.json 少个引号 → --check 判「[ok ] 已存在，17 B」，实例静默忽略整份，
    # 而下一次保存直接把它覆盖掉 —— 用户的词永久没了）。
    (_cold_root / "user_boosts.json").write_text('{"nihao": 3, oops', encoding="utf-8")
    cfg = fresh_cfg()
    cfg["output"]["boost_file"] = "user_boosts.json"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _cold_root, W)
    text = buf.getvalue()
    line_txt = next((ln.strip() for ln in text.splitlines() if "自学习权重" in ln and
                     ("[ok ]" in ln or "[info]" in ln or "[FAIL]" in ln)), "")
    check("★ 文件存在但读不了 → 判 [FAIL]（不是 [ok ] 已存在）",
          "[FAIL]" in line_txt and "已存在" not in line_txt, line_txt)
    check("★ 而且说清了「里面的词不会生效 / 会留 .broken 备份」",
          "读不了" in line_txt and "JSONDecodeError" in line_txt and ".broken" in line_txt,
          line_txt)
    check("读不了的时候退出码是 1", rc == 1, f"rc={rc}")

    # 好文件：报词数，别只会说「已存在」。
    (_cold_root / "user_boosts.json").write_text(
        json.dumps({"你好": 1.0, "世界": 2.0}), encoding="utf-8")
    cfg = fresh_cfg()
    cfg["output"]["boost_file"] = "user_boosts.json"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _cold_root, W)
    text = buf.getvalue()
    line_txt = next((ln.strip() for ln in text.splitlines() if "自学习权重" in ln and
                     ("[ok ]" in ln or "[info]" in ln or "[FAIL]" in ln)), "")
    check("好文件报 [ok ] 并说出里面有几个词",
          "[ok ]" in line_txt and "2 个词" in line_txt, line_txt)
    check("好文件不影响退出码", rc == 0, f"rc={rc}")
finally:
    shutil.rmtree(_cold_root, ignore_errors=True)

# ============================================================
print()
print("=" * 74)
print("4h. 配置文件里有写错的值 / 拼错的键时，--check 不许说「全部通过」")
print("=" * 74)

# 第 83 轮真机：``{"hotkeys": 5}`` 会让 hotkeys 整段退回默认值（用户配的退出键、
# 挂起键一起丢掉），``{"log": {"path": ...}}`` 那个键永远不会生效 —— 两份配置下
# ``--check`` 都判「全部通过 ✅」、rc 0。被静默丢弃的值必须进闸门。
_cold_root = PROJ / ".tmp" / "check-cfg"
shutil.rmtree(_cold_root, ignore_errors=True)
_cold_root.mkdir(parents=True, exist_ok=True)
try:
    import json as _json

    def _run_with(cfg_file: Path | None, cfg_dict: dict):
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            _rc = main.run_check(cfg_dict, _cold_root, W, cfg_path=cfg_file)
        return _rc, _buf.getvalue()

    # 整段类型写错：值被静默丢弃，必须 FAIL
    _bad = _cold_root / "bad-type.json"
    _bad.write_text(_json.dumps({"hotkeys": 5}), encoding="utf-8")
    from t_ime import config as _C

    _cfg, _ = _C.load(_bad)
    rc, text = _run_with(_bad, _cfg)
    check("有「== 配置文件 ==」这一节", "== 配置文件 ==" in text)
    check("★ 整段写错时退出码 1", rc == 1, f"rc={rc}")
    check("★ 不再说「全部通过」", "全部通过" not in text,
          [ln for ln in text.splitlines() if "通过" in ln])
    check("说清「你写的值没生效」", "你写的值没生效" in text or "已按默认值顶上" in text,
          [ln for ln in text.splitlines() if "写错的选项" in ln])
    check("点名 hotkeys 那一段", "hotkeys" in text)

    # 拼错的键：永远不会生效，同样必须 FAIL
    _typo = _cold_root / "typo.json"
    _typo.write_text(_json.dumps({"log": {"path": "alt.log"}}), encoding="utf-8")
    _cfg, _ = _C.load(_typo)
    rc, text = _run_with(_typo, _cfg)
    check("★ 拼错的键让 --check 退出 1", rc == 1, f"rc={rc}")
    check("点名 log.path 并说「永远不会生效」",
          "log.path" in text and "永远不会生效" in text,
          [ln for ln in text.splitlines() if "不认识的键" in ln])
    check("拼错时也不说「全部通过」", "全部通过" not in text)

    # 干净配置：仍然全部通过（别把闸门焊死）
    _clean = _cold_root / "clean.json"
    _clean.write_text("{}", encoding="utf-8")
    _cfg, _ = _C.load(_clean)
    rc, text = _run_with(_clean, _cfg)
    check("干净配置仍然 rc 0", rc == 0, f"rc={rc}")
    check("干净配置仍然「全部通过 ✅」", "全部通过 ✅" in text)
    check("「配置文件」那行是 [ok ] 并带上路径",
          any("[ok ]" in ln and "配置文件" in ln and "clean.json" in ln
              for ln in text.splitlines()))

    # 指定的配置根本不存在：不能再假装检查了它
    rc, text = _run_with(_cold_root / "nope.json", fresh_cfg())
    check("指定的配置文件不存在时退出码 1", rc == 1, f"rc={rc}")
    check("并说清「下面检查的是内置默认值」", "下面检查的是内置默认值" in text,
          [ln for ln in text.splitlines() if "配置文件" in ln])
finally:
    shutil.rmtree(_cold_root, ignore_errors=True)

# ============================================================
print()
print("=" * 74)
print("4i. 索引缓存写不进去时，--check 要把「每次启动都要重建」判成失败")
print("=" * 74)

# 第 89 轮真机：``pinyin.dict_cache`` 指向一个建不出来的路径（父路径是个文件）时，
# 索引每次启动都在内存里重建（约 2 秒，第 88 轮之后 run.bat 还要等它就绪），
# 可 ``--check`` 照样判「[ok ] 索引可用 … / 2.2s」+「全部通过 ✅」，
# 唯一的线索是日志里一句「写缓存失败（不影响使用）」—— 那句话本身就是假话。
_cold_root = PROJ / ".tmp" / "check-idxcache"
shutil.rmtree(_cold_root, ignore_errors=True)
_cold_root.mkdir(parents=True, exist_ok=True)
try:
    (_cold_root / "blocker").write_text("我是个文件", encoding="utf-8")
    cfg = fresh_cfg()
    cfg["pinyin"]["dict_cache"] = str(_cold_root / "blocker" / "idx.pkl")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _cold_root, W)
    text = buf.getvalue()
    line_txt = next((ln.strip() for ln in text.splitlines() if "索引缓存" in ln and
                     ("[ok ]" in ln or "[info]" in ln or "[FAIL]" in ln)), "")
    check("★ 缓存写不进去时判 [FAIL]", "[FAIL]" in line_txt, line_txt)
    check("★ 说清「写不进去」和「每次启动都要重建」",
          "写不进去" in line_txt and "重建索引" in line_txt, line_txt)
    check("并且不再说「全部通过」", "全部通过" not in text)
    check("退出码是 1", rc == 1, f"rc={rc}")

    # 写得进去的缓存：不该出现这一行（别把正常情况也判红）
    cfg = fresh_cfg()
    cfg["pinyin"]["dict_cache"] = str(_cold_root / "ok" / "idx.pkl")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _cold_root, W)
    text = buf.getvalue()
    check("缓存写得进去时不出现「索引缓存」FAIL 行",
          not any("索引缓存" in ln and "[FAIL]" in ln for ln in text.splitlines()))
    check("缓存写得进去时退出码 0", rc == 0, f"rc={rc}")
    check("缓存真的落盘了", (_cold_root / "ok" / "idx.pkl").exists())
finally:
    shutil.rmtree(_cold_root, ignore_errors=True)

# ============================================================
print()
print("=" * 74)
print("4j. shift_tap_ms=0 让 Shift 单击彻底失效，--check 不许再说「全部通过」")
print("=" * 74)

# 第 90 轮进程内真机：input.shift_tap_ms 的合法范围是 0–2000，可时间窗为零时
# IMECore.handle_key 里 `held_ms <= self.shift_tap_ms` 永不成立 —— Shift 单击一次
# 都不触发，而 banner / 面板 / 引导当时都还在教用户按它。这不是「用户关掉的功能」，
# 是两个开关自相矛盾（toggles 说开着，窗口是 0），要判 FAIL 并给出两条出路。
_cold_root = PROJ / ".tmp" / "check-tap0"
shutil.rmtree(_cold_root, ignore_errors=True)
_cold_root.mkdir(parents=True, exist_ok=True)
try:
    cfg = fresh_cfg()
    cfg["input"]["shift_tap_toggles"] = True
    cfg["input"]["shift_tap_ms"] = 0
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _cold_root, W)
    text = buf.getvalue()
    tap_line = next((ln.strip() for ln in text.splitlines() if "Shift 单击" in ln), "")
    check("★ 时间窗为 0 判 [FAIL]", "[FAIL]" in tap_line, tap_line)
    check("★ 说清「永远不会触发」", "永远不会触发" in tap_line, tap_line)
    check("★ 给出两条出路（调大 / 关掉 toggles）",
          "50~2000" in tap_line and "shift_tap_toggles" in tap_line, tap_line)
    check("并且不再说「全部通过」", "全部通过" not in text)
    check("退出码是 1", rc == 1, f"rc={rc}")

    # 时间窗小到人手按不到（30ms）：只提示，不判失败（它确实「开着」）
    cfg = fresh_cfg()
    cfg["input"]["shift_tap_toggles"] = True
    cfg["input"]["shift_tap_ms"] = 30
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _cold_root, W)
    text = buf.getvalue()
    tap_line = next((ln.strip() for ln in text.splitlines() if "Shift 单击" in ln), "")
    check("30 毫秒时是 [info]，不是 [FAIL]", "[info]" in tap_line and "[FAIL]" not in tap_line,
          tap_line)
    check("30 毫秒时提醒「人手来不及」", "来不及" in tap_line, tap_line)

    # 正常时间窗：这一行根本不该出现（别把正常配置也报出来）
    cfg = fresh_cfg()
    cfg["input"]["shift_tap_toggles"] = True
    cfg["input"]["shift_tap_ms"] = 600
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _cold_root, W)
    text = buf.getvalue()
    check("正常时间窗不出现「Shift 单击」这一行",
          not any("Shift 单击" in ln for ln in text.splitlines()))
    check("正常时间窗退出码 0", rc == 0, f"rc={rc}")
finally:
    shutil.rmtree(_cold_root, ignore_errors=True)

# ============================================================
print()
print("=" * 74)
print("4k. 默认上屏设成译文、可翻译关着：--check 不许说「全部通过」")
print("=" * 74)

# 第 91 轮真机：output.default_language="en" + translate.enabled=false 时，
# _commit 的 `if row == ROW_EN and not self.en` 会退回中文（每次空格都退），
# 「译文」这个模式永远不可达，而 banner / 面板当时都写着「译文」。
_cold_root = PROJ / ".tmp" / "check-default-en"
shutil.rmtree(_cold_root, ignore_errors=True)
_cold_root.mkdir(parents=True, exist_ok=True)
try:
    cfg = fresh_cfg()
    cfg["output"]["default_language"] = "en"
    cfg["translate"]["enabled"] = False
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _cold_root, W)
    text = buf.getvalue()
    lang_line = next((ln.strip() for ln in text.splitlines() if "上屏语言" in ln), "")
    check("★ 矛盾配置判 [FAIL]", "[FAIL]" in lang_line, lang_line)
    check("★ 说清「每次上屏都会退回中文」", "退回中文" in lang_line, lang_line)
    check("★ 给出两条出路（打开翻译 / 改成 zh）",
          "translate.enabled" in lang_line and "output.default_language" in lang_line,
          lang_line)
    check("并且不再说「全部通过」", "全部通过" not in text)
    check("退出码是 1", rc == 1, f"rc={rc}")

    # 翻译开着 + 默认译文：正常，不该出现这一行
    cfg = fresh_cfg()
    cfg["output"]["default_language"] = "en"
    cfg["translate"]["enabled"] = True
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _cold_root, W)
    text = buf.getvalue()
    check("翻译开着时没有「上屏语言」这一行",
          not any("上屏语言" in ln for ln in text.splitlines()))
    check("翻译开着时退出码 0", rc == 0, f"rc={rc}")

    # 默认中文 + 翻译关着：合法配置，也不该出现这一行
    cfg = fresh_cfg()
    cfg["output"]["default_language"] = "zh"
    cfg["translate"]["enabled"] = False
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _cold_root, W)
    text = buf.getvalue()
    check("默认中文时没有「上屏语言」这一行",
          not any("上屏语言" in ln for ln in text.splitlines()))
    check("默认中文时退出码 0", rc == 0, f"rc={rc}")
finally:
    shutil.rmtree(_cold_root, ignore_errors=True)

# ============================================================
print()
print("=" * 74)
print("4l. overlay.min_width > max_width：两个值各自合法，但自相矛盾必须红")
print("=" * 74)

# 第 99 轮真机：overlay.min_width=1200 / overlay.max_width=200（两个值都在
# _INT_SPECS 的合法区间里）时，宽度链是 `w = max(req, min_width)` 再
# `if w > max_width: w = max_width`，最终窗口只有 300px（@1.5x）—— 比用户要求的
# 最小宽度还窄、候选被裁掉，而 --check 只报 reqwidth、一个字都不说。
_width_root = PROJ / ".tmp" / "check-width-conflict"
shutil.rmtree(_width_root, ignore_errors=True)
_width_root.mkdir(parents=True, exist_ok=True)
try:
    cfg = fresh_cfg()
    cfg["overlay"]["min_width"] = 1200
    cfg["overlay"]["max_width"] = 200
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _width_root, W)
    text = buf.getvalue()
    width_line = next((ln.strip() for ln in text.splitlines() if "窗口宽度" in ln), "")
    check("★ 下限 > 上限判 [FAIL]", "[FAIL]" in width_line, width_line)
    check("★ 行里写着两个相矛盾的数字（1200 / 200）",
          "1200" in width_line and "200" in width_line, width_line)
    check("★ 说清实际按下限算（否则比最小宽度还窄）",
          "下限 1200" in width_line and "还窄" in width_line, width_line)
    check("★ 给出两条出路（max_width 调大 / min_width 调小）",
          "max_width" in width_line and "min_width" in width_line, width_line)
    check("并且不再说「全部通过」", "全部通过" not in text)
    check("退出码是 1", rc == 1, f"rc={rc}")

    cfg = fresh_cfg()
    cfg["overlay"]["min_width"] = 520
    cfg["overlay"]["max_width"] = 900
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _width_root, W)
    text = buf.getvalue()
    width_line = next((ln.strip() for ln in text.splitlines() if "窗口宽度" in ln), "")
    check("正常值时窗口宽度那行只是 [info]、不判红",
          width_line.startswith("[info]") and "[FAIL]" not in width_line, width_line)
    check("正常值退出码 0", rc == 0, f"rc={rc}")
finally:
    shutil.rmtree(_width_root, ignore_errors=True)

# ============================================================
print()
print("=" * 74)
print("4m. 连不上 API 时：翻译往返是 [warn]，不是 [FAIL]（第 117 轮）")
print("=" * 74)

# 第 117 轮真机：离线机器上跑 --check，输出是
#   [FAIL] 翻译往返  你好 -> []（翻译请求失败：<urlopen error [WinError 10061] …>）
# 用户拿着一个 FAIL 去翻 config.json —— 可配置一个字都没错，只是这台机器没网。
# 「环境造成的验不了」要跟「你配错了」分开说：前者 [warn] 且不把自检判红。
_s = socket.socket()
_s.bind(("127.0.0.1", 0))
_dead_port = _s.getsockname()[1]
_s.close()  # 关掉：这个端口现在没人听，连接会立刻被拒（不是超时，测试才快）
_nonet_root = PROJ / ".tmp" / "check-nonet"
shutil.rmtree(_nonet_root, ignore_errors=True)
_nonet_root.mkdir(parents=True, exist_ok=True)
try:
    cfg = fresh_cfg()
    cfg["translate"]["enabled"] = True
    cfg["api"]["provider"] = "deepseek"
    cfg["api"]["api_key"] = "sk-not-a-real-key"
    cfg["api"]["base_url"] = f"http://127.0.0.1:{_dead_port}/v1"
    cfg["api"]["timeout"] = 1.0
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main.run_check(cfg, _nonet_root, W)
    text = buf.getvalue()
    tr_line = next((ln.strip() for ln in text.splitlines() if "翻译往返" in ln), "")
    check("★ 连不上时那一行是 [warn]", tr_line.startswith("[warn]"), tr_line)
    check("★ 整份输出里没有 [FAIL] 翻译往返", "[FAIL] 翻译往返" not in text)
    check("★ 说清「不是配置写错」", "不是配置写错" in text)
    check("★ 给出「联网后再跑一次 --check」", "联网后再跑一次 --check" in text, text[-300:])
    check("★ 报出连的是哪个地址", f"127.0.0.1:{_dead_port}" in tr_line, tr_line)
    check("连不上不该把整个自检判红", rc == 0, f"rc={rc}")
    check("结尾仍然是「全部通过 ✅」（沿用 [warn] 不翻 ok 的口径）",
          "全部通过 ✅" in text)
finally:
    shutil.rmtree(_nonet_root, ignore_errors=True)

# ============================================================
print()
print("=" * 74)
print("5. 自检跑完要收拾干净：不留窗口、不留状态文件、不起实例")
print("=" * 74)

import tkinter as tk  # noqa: E402
check(tk._default_root is None, "临时候选窗的 Tk 根窗口已销毁")
if PRE_RUNNING:
    print("   开始跑测试前就已经有一个实例在跑 —— 状态文件 / 实例是它的，这两条跳过")
else:
    check(not STATUS.exists(), "没留下状态文件")
    check(not D.already_running(), "没有偷偷起一个常驻实例")

# ============================================================
print()
print("=" * 74)
print("5b. 常驻实例用的是另一份配置时，--check 要说出来（第 121 轮）")
print("=" * 74)
# 用户最容易踩的：手里改的是 A 份，跑 --check --config A 得到「全部通过 ✅」，
# 可正在跑的那个实例读的是 B 份 —— 按新热键毫无反应。报告必须点名「它用的是哪份」。

_other_cfg = PROJ / ".tmp" / "check-other-config.json"
_other_cfg.parent.mkdir(parents=True, exist_ok=True)
_other_cfg.write_text(json.dumps({"hotkeys": {"quit": "ctrl+alt+f12"}}, ensure_ascii=False),
                      encoding="utf-8")
_old_running = D.is_running
_old_status = D.read_status
try:
    D.is_running = lambda: True
    D.read_status = lambda root_dir: {
        "pid": 4321,
        "config": str(PROJ / "config.json"),
        "quit_hotkey": "Ctrl+Alt+Q",
    }

    _buf_mis = io.StringIO()
    with contextlib.redirect_stdout(_buf_mis):
        rc_mis = main.run_check(fresh_cfg(), PROJ, W, cfg_path=_other_cfg)
    _txt_mis = _buf_mis.getvalue()
    check("[warn] 常驻实例" in _txt_mis,
          "★ 检查别的文件时，用 [warn] 点出常驻实例", _txt_mis[-260:])
    check("不是这次检查的" in _txt_mis, "★ 说清「不是这次检查的」那一份")
    check(str(PROJ / "config.json") in _txt_mis, "★ 报出实例真正在用的路径")
    check("退出键 Ctrl+Alt+Q" in _txt_mis, "★ 顺带报出实例当前的退出键")
    check(rc_mis == 0, "这只是提醒，不把自检判红", f"rc={rc_mis}")
    check("全部通过 ✅" in _txt_mis, "结尾仍是「全部通过 ✅」")

    _buf_same = io.StringIO()
    with contextlib.redirect_stdout(_buf_same):
        rc_same = main.run_check(fresh_cfg(), PROJ, W)
    _txt_same = _buf_same.getvalue()
    check("用的就是这份配置" in _txt_same and "pid 4321" in _txt_same,
          "检查的就是实例那份时，说「用的就是这份配置」", _txt_same[-260:])
    check("[warn] 常驻实例" not in _txt_same, "这种情况不出 [warn] 常驻实例")
    check(rc_same == 0, "同样 rc 0", f"rc={rc_same}")

    D.is_running = lambda: False
    _buf_off = io.StringIO()
    with contextlib.redirect_stdout(_buf_off):
        rc_off = main.run_check(fresh_cfg(), PROJ, W)
    check("常驻实例" not in _buf_off.getvalue(), "没有实例在跑时，这行根本不出现")
    check(rc_off == 0, "rc 0", f"rc={rc_off}")
finally:
    D.is_running = _old_running
    D.read_status = _old_status
    _other_cfg.unlink(missing_ok=True)

# ============================================================
print()
print("=" * 74)
print("6. 真机：main.py --check 端到端（用户手里敲的就是这一条）")
print("=" * 74)

if D.already_running():
    print("   已经有一个实例在跑 —— 这一节跳过（它自己会写状态文件）")
else:
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "main.py", "--check"],
        cwd=str(PROJ), capture_output=True, text=True,
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=300,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    check(proc.returncode == 0, "退出码 0", f"rc={proc.returncode}, {time.time() - t0:.1f}s")
    check("== 窗口层 ==" in out, "窗口层那一节在输出里")
    check(re.search(r"\[ok \] 候选窗构造\s+\d+x\d+ 像素 @", out) is not None,
          "候选窗真的被构造过（有尺寸）")
    check("全部通过 ✅" in out, "最后是「全部通过 ✅」")
    check("Traceback" not in out, "没有 traceback")
    check(not STATUS.exists(), "跑完没有留下状态文件")
    check(not D.already_running(), "跑完没有留下常驻实例")

# ============================================================
print()
print("=" * 74)
passed = sum(1 for ok, _ in results if ok)
total = len(results)
print(f"结果：{passed}/{total} 通过")
print("=" * 74)
for ok, label in results:
    if not ok:
        print(f"{BAD} {label}")
sys.exit(0 if passed == total else 1)
