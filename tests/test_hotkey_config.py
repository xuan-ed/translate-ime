# -*- coding: utf-8 -*-
"""``config.json`` 的 ``hotkeys`` 段写错时，会静默坏事 —— 这个测试盯着它。

README 说热键可以在 ``config.json`` 里改。但 ``IMECore._parse_hotkeys`` 以前对
**动作名**不看白名单，于是：

* ``{"quit_typo": "ctrl+alt+q"}``：名字拼错了，还是被注册成 ``("ctrl+alt+q")
  -> "quit_typo"``。那个组合键从此被我们吞掉，按下去却什么也不发生 ——
  ``_fire`` 只在 ``--verbose`` 时打一句「未知热键动作」，默认用户完全看不到。
* ``{"quit": "ctrl+alt+q", "toggle_enabled": "ctrl+alt+q"}``：后出现的**静默盖掉**
  先出现的，``quit`` 失效，用户只会觉得「我配的退出键没反应」。

现在：动作名有白名单 ``HOTKEY_ACTIONS``（就是 ``_fire`` 认识的那五个），写错的名字
不注册、直接在控制台/日志里点名；同一个组合键配给两个动作时，先出现的生效、
后出现的被忽略，并说清楚是谁赢了。

直接跑：``python tests/test_hotkey_config.py``
"""
import contextlib
import copy
import io
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime import winapi as W  # noqa: E402
from t_ime.config import DEFAULTS  # noqa: E402
from t_ime.console import fix_console_encoding  # noqa: E402
from t_ime.core import HOTKEY_ACTIONS, IMECore  # noqa: E402

fix_console_encoding()

PASS = 0
FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok ] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  <-- {extra!r}")


class StubEngine:
    """只回答「没有候选」，热键测试不关心拼音。"""

    def candidates(self, buffer):
        return []


class StubTranslator:
    enabled = False
    is_mock = False

    def translate_many(self, *args, **kwargs):
        return []


def build(hotkeys):
    """按给定的 hotkeys 段建一个 IMECore，并把它的启动输出也收下来。"""
    cfg = copy.deepcopy(DEFAULTS)
    cfg["hotkeys"] = hotkeys
    events = []
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        core = IMECore(
            cfg,
            StubEngine(),
            StubTranslator(),
            emit=events.append,
            committer=lambda text: True,
            verbose=False,
        )
    # 合成按键没有对应的物理按键：不 stub 的话 handle_key 会拿真实（全抬起）
    # 的修饰键状态去纠偏，把 Ctrl+Alt+Q 当成普通 'q' 打进缓冲 —— 假结论。
    core.resync_modifiers = lambda: None
    return core, buf.getvalue(), events


def press(core, vk, mods=(W.VK_CONTROL, W.VK_MENU)):
    for mod in mods:
        core.handle_key(mod, True, False)
    swallowed = core.handle_key(vk, True, False)
    for mod in reversed(mods):
        core.handle_key(mod, False, False)
    return swallowed


def combo(spec):
    return W.parse_hotkey(spec)


# --------------------------------------------------------------------- #
# 1. 动作名拼错：不注册、点名、留出可用动作，组合键不再被吞
# --------------------------------------------------------------------- #
print("\n[1] 动作名拼错（quit_typo）")
core, out, _ = build({"quit_typo": "ctrl+alt+q"})
try:
    check("拼错的动作名不会被注册成热键", core.hotkeys == {}, core.hotkeys)
    check("控制台点名了拼错的动作名", "'quit_typo'" in out, out)
    check("说明它「不认识」", "不认识" in out, out)
    check("列出了真正支持的动作名", all(n in out for n in ("compose", "quit", "toggle_enabled")), out)
    check("Ctrl+Alt+Q 不再被吞（透传给系统）", press(core, 0x51) is False)
    check("也没有被当成字母打进拼音缓冲", core.buffer == "", core.buffer)
finally:
    core.shutdown()

# --------------------------------------------------------------------- #
# 2. 同一个组合键配给两个动作：先出现的赢，后出现的被忽略且说出来
# --------------------------------------------------------------------- #
print("\n[2] 组合键冲突（quit 与 toggle_enabled 都绑 ctrl+alt+q）")
core, out, events = build({"quit": "ctrl+alt+q", "toggle_enabled": "ctrl+alt+q"})
try:
    check("只注册了一个动作", len(core.hotkeys) == 1, core.hotkeys)
    check("注册的是先出现的 quit", list(core.hotkeys.values()) == ["quit"], core.hotkeys)
    check("冲突消息点名了两个动作", "quit" in out and "toggle_enabled" in out, out)
    check("消息说明了谁生效", "quit 生效" in out, out)
    check("消息说明了谁被忽略", "toggle_enabled 已忽略" in out, out)
    check("按下 Ctrl+Alt+Q 真的退出", press(core, 0x51) and core.quit_requested)
    check("退出事件发出去了", any(e.get("type") == "quit" for e in events), events)
finally:
    core.shutdown()

print("\n[3] 反序（toggle_enabled 写在前）")
core, out, events = build({"toggle_enabled": "ctrl+alt+q", "quit": "ctrl+alt+q"})
try:
    check("这次赢的是 toggle_enabled", list(core.hotkeys.values()) == ["toggle_enabled"], core.hotkeys)
    check("冲突消息里是 toggle_enabled 生效", "toggle_enabled 生效" in out, out)
    was = core.enabled
    press(core, 0x51)
    check("按下后挂起状态翻转了", core.enabled is (not was), (was, core.enabled))
    check("quit 没有被触发", core.quit_requested is False)
finally:
    core.shutdown()

# --------------------------------------------------------------------- #
# 4. 空值 = 不绑（这是文档里写明的用法），非法组合键 = 说一声
# --------------------------------------------------------------------- #
print("\n[4] 留空与非法组合键")
core, out, _ = build({"quit": "", "toggle_direct": "ctrl+shift"})
try:
    check("留空的动作没有注册", all(v != "quit" for v in core.hotkeys.values()), core.hotkeys)
    check("留空不刷任何消息", "quit" not in out or "无效" in out, out)
    check("非法组合键没有被注册", core.hotkeys == {}, core.hotkeys)
    check("非法组合键打了一句「无效」", "无效" in out, out)
    check("消息里带了那个写错的 spec", "ctrl+shift" in out, out)
finally:
    core.shutdown()

# --------------------------------------------------------------------- #
# 5. 五个合法动作各绑一个组合键：全部认、不冲突、白名单与 _fire 一致
# --------------------------------------------------------------------- #
print("\n[5] 五个合法动作")
SPECS = {
    "compose": "ctrl+alt+space",
    "quit": "ctrl+alt+q",
    "toggle_default": "ctrl+alt+l",
    "toggle_direct": "ctrl+alt+d",
    "toggle_enabled": "ctrl+alt+z",
}
core, out, _ = build(SPECS)
try:
    check("五个动作全部注册上了", set(core.hotkeys.values()) == set(SPECS), core.hotkeys)
    check("每个动作一个组合键，没有互相盖掉", len(core.hotkeys) == len(SPECS), core.hotkeys)
    check("全程没有警告", out.strip() == "", out)
    check(
        "HOTKEY_ACTIONS 正好是这五个",
        HOTKEY_ACTIONS == frozenset(SPECS),
        HOTKEY_ACTIONS,
    )
finally:
    core.shutdown()

# --------------------------------------------------------------------- #
# 6. 真实路径：config.json 要先和默认值合并，顺序由默认配置决定
# --------------------------------------------------------------------- #
print("\n[6] 经 load() 合并之后的真实顺序")
import json  # noqa: E402
import tempfile  # noqa: E402

from t_ime import config as C  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="t-ime-hotkey-"))
try:
    raw = copy.deepcopy(C.DEFAULTS)
    raw["hotkeys"] = {"quit": "ctrl+alt+q", "toggle_enabled": "ctrl+alt+q"}
    p = TMP / "config.json"
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    merged, _ = C.load(p)
    order = list(merged["hotkeys"].keys())
    check(
        "合并后 toggle_enabled 排在 quit 前面（默认配置的顺序）",
        order.index("toggle_enabled") < order.index("quit"),
        order,
    )
    core, out, _ = build(merged["hotkeys"])
    try:
        check("真实路径下 Ctrl+Alt+Q 归 toggle_enabled", core.hotkeys.get(combo("ctrl+alt+q")) == "toggle_enabled", core.hotkeys)
        check("quit 没有被注册", "quit" not in core.hotkeys.values(), core.hotkeys)
        check("消息说清是 toggle_enabled 生效", "toggle_enabled 生效" in out, out)
        was = core.enabled
        press(core, 0x51)
        check("按下 Ctrl+Alt+Q 挂起了输入法（而不是退出）", core.enabled is (not was), core.enabled)
        check("quit 没有被触发", core.quit_requested is False)
    finally:
        core.shutdown()
finally:
    import shutil  # noqa: E402

    shutil.rmtree(TMP, ignore_errors=True)

# --------------------------------------------------------------------- #
# 7. 默认配置本身必须干净：不能一启动就刷警告
# --------------------------------------------------------------------- #
print("\n[7] 默认配置")
core, out, _ = build(copy.deepcopy(DEFAULTS["hotkeys"]))
try:
    default_specs = {k: v for k, v in DEFAULTS["hotkeys"].items() if v}
    check("默认动作名全在白名单里", set(default_specs) <= HOTKEY_ACTIONS, default_specs)
    check("默认配置没有警告", out.strip() == "", out)
    check("默认注册数 = 非空 spec 数", len(core.hotkeys) == len(default_specs), core.hotkeys)
    check("默认的 Ctrl+Alt+Q 还是退出", core.hotkeys.get(combo("ctrl+alt+q")) == "quit", core.hotkeys)
finally:
    core.shutdown()

# --------------------------------------------------------------------- #
# 8. 体检用的热键分诊：不只是「能解析」，还要能点出「按了没反应」
# --------------------------------------------------------------------- #
# 第 63 轮：--check / --doctor 以前只验证 spec 能解析成 mods+vk，于是拼错的动作名
# （hotkeys.quitt）和两个动作撞同一个组合键，在体检里全是 [ok]，真实例却把原因只
# 写进日志 —— 用户照着「全部通过」的结论按退出键，结果按出来的是「挂起」。
print("\n[8] check_hotkeys：体检也能点出「这个键按了没用」")
from t_ime.hotkey import check_hotkeys  # noqa: E402

_rows = check_hotkeys(DEFAULTS["hotkeys"], W.parse_hotkey)
check("默认配置一个问题都没有", all(not p for _, _, p in _rows), _rows)
check("默认动作数就是热键段里的项数", len(_rows) == len(DEFAULTS["hotkeys"]), _rows)

_rows = {n: (s, p) for n, s, p in check_hotkeys(
    {"quit": "ctrl+alt+q", "quitt": "ctrl+alt+f8"}, W.parse_hotkey)}
check("拼错的动作名被点名", "不认识" in _rows["quitt"][1], _rows["quitt"])
check("点名时列出支持的动作",
      "compose" in _rows["quitt"][1] and "toggle_enabled" in _rows["quitt"][1],
      _rows["quitt"][1])
check("没写错的动作不受牵连", _rows["quit"][1] == "", _rows["quit"])

_rows = {n: (s, p) for n, s, p in check_hotkeys(
    {"toggle_enabled": "ctrl+alt+q", "quit": "ctrl+alt+q"}, W.parse_hotkey)}
check("撞车的第二个动作被点名", "绑了同一个组合键" in _rows["quit"][1], _rows["quit"])
check("点名时说出谁真的生效", "toggle_enabled" in _rows["quit"][1], _rows["quit"][1])
check("先出现的那个不报问题", _rows["toggle_enabled"][1] == "", _rows["toggle_enabled"])

_rows = {n: (s, p) for n, s, p in check_hotkeys({"quit": "ctrl+alt+"}, W.parse_hotkey)}
check("写坏的写法报解析失败", "解析失败" in _rows["quit"][1], _rows)
check("留空 = 合法关掉，不算问题",
      check_hotkeys({"quit": ""}, W.parse_hotkey)[0][2] == "",
      check_hotkeys({"quit": ""}, W.parse_hotkey))

# --------------------------------------------------------------------- #
# 9. 没有修饰键的打字键热键：注册它等于把那个键从打字里抢走
# --------------------------------------------------------------------- #
# 第 77 轮真机：hotkeys.quit = "q" 时 ``--check`` 判 [ok]，而真实例打字打到 q
# 就退出了（状态文件凭空消失），banner / 状态里还在播报「按 Q 退出」。
print("\n[9] 没有修饰键的打字键（quit: q）")
from t_ime.hotkey import needs_modifier, spec_problem  # noqa: E402

check("字母键需要修饰键", needs_modifier(0x51), 0x51)
check("数字键需要修饰键", needs_modifier(0x31), 0x31)
check("空格 / 回车 / 退格也算打字键",
      needs_modifier(0x20) and needs_modifier(0x0D) and needs_modifier(0x08))
check("F8 不需要修饰键（打字时不碰它）", not needs_modifier(0x77), 0x77)
check("单独一个字母的写法被点出「没有修饰键」",
      "没有修饰键" in spec_problem("q", W.parse_hotkey), spec_problem("q", W.parse_hotkey))
check("ctrl+q 是正常写法，不报问题", spec_problem("ctrl+q", W.parse_hotkey) == "",
      spec_problem("ctrl+q", W.parse_hotkey))
check("f8 是正常写法，不报问题", spec_problem("f8", W.parse_hotkey) == "",
      spec_problem("f8", W.parse_hotkey))

_rows = {n: (s, p) for n, s, p in check_hotkeys({"quit": "q"}, W.parse_hotkey)}
check("check_hotkeys 把 quit: q 判成问题", "没有修饰键" in _rows["quit"][1], _rows["quit"])
check("问题文案说清了「打字时按到就会触发」", "打字时按到" in _rows["quit"][1], _rows["quit"])
_rows = {n: (s, p) for n, s, p in check_hotkeys(
    {"quit": "ctrl+q", "toggle_direct": "f8"}, W.parse_hotkey)}
check("带修饰键 / F 键都不误报",
      _rows["quit"][1] == "" and _rows["toggle_direct"][1] == "", _rows)

core, out, _ = build({"quit": "q"})
try:
    check("核心拒绝注册 quit: q", core.hotkeys == {}, core.hotkeys)
    check("registered_hotkeys 里也没有它", "quit" not in core.registered_hotkeys,
          core.registered_hotkeys)
    check("日志里点名了它并说了原因", "'q'" in out and "没有修饰键" in out, out)
    press(core, 0x51, mods=())          # 输入法本来就该吞掉字母，判据是「进缓冲了」
    check("按 q 不再触发退出，而是当字母打进缓冲",
          core.quit_requested is False and core.buffer == "q",
          (core.quit_requested, core.buffer))
finally:
    core.shutdown()

core, out, _ = build({"quit": "ctrl+alt+q", "toggle_enabled": "ctrl+alt+z"})
try:
    check("正常热键会进 registered_hotkeys",
          core.registered_hotkeys.get("quit") == "Ctrl+Alt+Q", core.registered_hotkeys)
finally:
    core.shutdown()

# --------------------------------------------------------------------- #
# 10. 只有 Shift 的组合键：Shift+字母就是打大写字母，等于把「打大写」变成按热键
# --------------------------------------------------------------------- #
# 第 84 轮真机：hotkeys.quit = "shift+q" 时 --check 全绿、真实例打一个大写 Q 就退出
# （那个 Q 还被吞掉了）。另一半是 "ctrl+q+w" 这种写了两个主键的串：真正注册的只有
# 最后一个主键，而 banner / 状态里报的还是配置原文。
print("\n[10] 只有 Shift 的组合键 / 一个串里两个主键")
check("shift+q 被判成问题", "只有 Shift" in spec_problem("shift+q", W.parse_hotkey),
      spec_problem("shift+q", W.parse_hotkey))
check("问题里说明了「打大写字母时会误触」",
      "打大写字母" in spec_problem("shift+q", W.parse_hotkey))
check("shift+space 同样被判成问题",
      "只有 Shift" in spec_problem("shift+space", W.parse_hotkey))
check("ctrl+shift+q 是正常写法，不报问题",
      spec_problem("ctrl+shift+q", W.parse_hotkey) == "")
check("shift+f8 是正常写法（F 键打字时不碰）",
      spec_problem("shift+f8", W.parse_hotkey) == "")

_rows = {n: (s, p) for n, s, p in check_hotkeys({"quit": "shift+q"}, W.parse_hotkey)}
check("check_hotkeys 把 quit: shift+q 判成问题", "只有 Shift" in _rows["quit"][1])

core, out, _ = build({"quit": "shift+q"})
try:
    check("核心拒绝注册 quit: shift+q", core.hotkeys == {}, core.hotkeys)
    check("日志里点名了 shift+q", "'shift+q'" in out and "只有 Shift" in out, out)
    core.handle_key(W.VK_SHIFT, True, False)
    core.handle_key(0x51, True, False)     # Shift 按住 + Q = 打一个大写 Q
    core.handle_key(0x51, False, False)
    core.handle_key(W.VK_SHIFT, False, False)
    check("打一个大写 Q 不再把输入法退掉", core.quit_requested is False,
          core.quit_requested)
finally:
    core.shutdown()

try:
    W.parse_hotkey("ctrl+q+w")
except ValueError as exc:
    check("一个串里两个主键会被拒绝", "两个主键" in str(exc), str(exc))
else:
    check("一个串里两个主键会被拒绝", False, "parse_hotkey 没有报错")

_rows = {n: (s, p) for n, s, p in check_hotkeys({"quit": "ctrl+q+w"}, W.parse_hotkey)}
check("check_hotkeys 把它报成解析失败", "解析失败" in _rows["quit"][1], _rows["quit"])

check("combo_text 只写真正生效的键",
      W.combo_text(*W.parse_hotkey("ctrl+shift+q")) == "Ctrl+Shift+Q",
      W.combo_text(*W.parse_hotkey("ctrl+shift+q")))
check("combo_text 认别名（return / control）",
      W.combo_text(*W.parse_hotkey("control+return")) == "Ctrl+Enter",
      W.combo_text(*W.parse_hotkey("control+return")))
check("combo_text 认 win 键",
      W.combo_text(frozenset({W.VK_LWIN}), 0x51) == "Win+Q",
      W.combo_text(frozenset({W.VK_LWIN}), 0x51))

# --------------------------------------------------------------------- #
# 11. shift_tap_ms = 0：时间窗为零 = Shift 单击永远不会触发，别再教用户按它
# --------------------------------------------------------------------- #
# 第 90 轮进程内真机：input.shift_tap_ms 的合法范围是 0–2000（config._INT_SPECS），
# 而 IMECore.handle_key 里 tapped 的条件是 held_ms <= self.shift_tap_ms —— 窗口为 0
# 时谁按都不成立。可 tap_switch_hint 当时返回「点一下 Shift」，直通提示还写
# 「单击 Shift（0.0 秒内）」，等于教用户按一个死键。
print("\n[11] shift_tap_ms=0：提示不许再教「点一下 Shift」")
from t_ime.hotkey import shift_tap_off_reason, shift_tap_problem, tap_switch_hint  # noqa: E402

_cfg_tap = copy.deepcopy(DEFAULTS)
_cfg_tap.setdefault("input", {})["shift_tap_toggles"] = True
_cfg_tap["input"]["shift_tap_ms"] = 0
check("时间窗为 0 时 tap_switch_hint 返回空串", tap_switch_hint(_cfg_tap) == "",
      tap_switch_hint(_cfg_tap))
check("off_reason 指出是 shift_tap_ms 为 0",
      "shift_tap_ms" in shift_tap_off_reason(_cfg_tap), shift_tap_off_reason(_cfg_tap))
_problem = shift_tap_problem(_cfg_tap)
check("problem 点明「永远不会触发」", "永远不会触发" in _problem, _problem)
check("problem 给出两条出路（调大 / 关掉）",
      "50~2000" in _problem and "shift_tap_toggles" in _problem, _problem)

_cfg_tap["input"]["shift_tap_ms"] = 30
check("30 毫秒：problem 提醒人手来不及", "来不及" in shift_tap_problem(_cfg_tap),
      shift_tap_problem(_cfg_tap))
check("30 毫秒：hint 用毫秒说，不写「0.0 秒」",
      "30 毫秒" in tap_switch_hint(_cfg_tap), tap_switch_hint(_cfg_tap))

_cfg_tap["input"]["shift_tap_ms"] = 800
check("正常时间窗不报警", shift_tap_problem(_cfg_tap) == "" and shift_tap_off_reason(_cfg_tap) == "",
      (shift_tap_problem(_cfg_tap), shift_tap_off_reason(_cfg_tap)))

_cfg_tap["input"]["shift_tap_toggles"] = False
check("关掉 Shift 单击时 off_reason 说 toggles",
      "shift_tap_toggles" in shift_tap_off_reason(_cfg_tap),
      shift_tap_off_reason(_cfg_tap))
check("关掉 Shift 单击时不算配置矛盾", shift_tap_problem(_cfg_tap) == "",
      shift_tap_problem(_cfg_tap))

# ---------------------------------------------------------------------------
print("\n[12] 候选窗底部的「换语言」只能写真正注册上的键（第 115 轮）")
# `toggle_default: "l"` 会被 core 拒绝注册（没有修饰键的打字键），以前底部提示
# 读的是配置原文再 pretty()，于是照样写「L 换语言」，用户按 L 只打出一个 l；
# 配成空串时是「 ·  换语言」—— 指着一个不存在的键。
from t_ime.overlay import Overlay  # noqa: E402  只有这一节需要 Tk

_cfg_hint = copy.deepcopy(DEFAULTS)


def _hint_of(spec: str, switch_hint=None) -> str:
    _cfg_hint["hotkeys"]["toggle_default"] = spec
    ov = Overlay(_cfg_hint, verbose=False, switch_hint=switch_hint)
    try:
        return ov.hint
    finally:
        ov.destroy()


_mute = _hint_of("l")
check("被拒绝注册的键不出现在底部提示里", "换语言" not in _mute, _mute)
check("空串也不留一个悬空的「换语言」", "换语言" not in _hint_of(""), _hint_of(""))
check("正常组合键照旧写出来", "Ctrl+Alt+L 换语言" in _hint_of("ctrl+alt+l"), _hint_of("ctrl+alt+l"))
check("App 传进来的注册表写法优先级最高（配置原文写了什么都不管）",
      "Ctrl+Alt+F9 换语言" in _hint_of("l", switch_hint="Ctrl+Alt+F9"),
      _hint_of("l", switch_hint="Ctrl+Alt+F9"))
check("App 确认「没有可用键」时空串传进来就不提",
      "换语言" not in _hint_of("ctrl+alt+l", switch_hint=""),
      _hint_of("ctrl+alt+l", switch_hint=""))

_cfg_hint["hotkeys"]["toggle_default"] = "ctrl+alt+l"
_ov = Overlay(_cfg_hint, verbose=False, switch_hint="Ctrl+Alt+L")
try:
    _ov.set_switch_hint("")
    check("热重载把键清空后提示行跟着去掉", "换语言" not in _ov.hint, _ov.hint)
    _ov.set_switch_hint("Ctrl+Shift+F10")
    check("热重载换成新键后提示行跟着换", "Ctrl+Shift+F10 换语言" in _ov.hint, _ov.hint)
finally:
    _ov.destroy()

print(f"\n结果：{PASS}/{PASS + FAIL} 通过")
if FAIL:
    sys.exit(1)
