# -*- coding: utf-8 -*-
"""把配置里的热键写法（``ctrl+alt+q``）变成给人看的写法（``Ctrl+Alt+Q``）。

为什么单独一个模块：这个函数本来住在 ``overlay.py``（候选窗那套 —— 模块一导入就
把 tkinter 拉起来）。于是凡是想显示热键的地方（``--help`` 末尾、daemon 的退出提示、
挂起时的气泡、控制面板、``--doctor`` 的建议）都不愿意为了一个字符串处理去 import
tkinter，就干脆把 ``Ctrl+Alt+Q`` 抄一遍；用户一改 ``hotkeys``，这些抄来的提示全成了
假话（第 22 轮就是在收这一类的尾巴）。放在这里，谁都能便宜地用对的那一份。

``overlay._pretty_hotkey`` 仍然可用（它现在只是这里的转发），老代码不用动。

``HOTKEY_ACTIONS`` / ``check_hotkeys`` 是第 63 轮从 ``core.IMECore._parse_hotkeys``
里提出来的：``--check`` 和 ``--doctor`` 以前只验证「这个字符串能不能解析成
mods+vk」，不验证动作名认不认识、也不验证两个动作是不是撞了同一个组合键。于是
``hotkeys.quitt`` 这种拼错的键在体检里是 ``[ok]``，而真实例只会把
``[core] 热键动作名 'quitt' 不认识，已忽略`` 写进日志；两个动作撞车时体检两行都是
``[ok]``，真实例却只让先出现的那个生效 —— 用户想按退出键，结果把输入法挂起了。
"""

#: ``_fire`` 认识的动作名。写错名字的键以前会被当成合法热键注册（按下去什么也不
#: 发生，日志里也没有一句话），所以这里留一份白名单给诊断用。
HOTKEY_ACTIONS = frozenset({
    "quit",
    "toggle_enabled",
    "toggle_default",
    "toggle_direct",
    "compose",
})


def pretty(spec) -> str:
    """``ctrl+alt+q`` -> ``Ctrl+Alt+Q``；空串 / None -> 空串。"""
    return "+".join(p.strip().capitalize() for p in str(spec).split("+") if p.strip())


def tap_switch_hint(cfg: dict) -> str:
    """「点一下 Shift 切拼音/英文直通」那一句，跟着 ``input`` 走。

    ``input.shift_tap_toggles=false`` 是合法配置（有人习惯 Shift 只用来打大写），
    这时**返回空串** —— 调用方不许再写死「点一下 Shift」，否则等于教用户去按一个
    不存在的功能。时间窗同样从 ``input.shift_tap_ms`` 取，不写死 0.6 秒。

    ``shift_tap_ms <= 0`` 也返回空串：时间窗为零意味着 ``held_ms <= 0`` 永远不成
    立，Shift 单击一次都不会触发（第 90 轮真机：`ms=0` 时按一下 Shift 的 `direct`
    仍是 False，而提示还在教「点一下 Shift」）。
    """
    in_cfg = cfg.get("input") or {}
    if not bool(in_cfg.get("shift_tap_toggles", True)):
        return ""
    ms = int(in_cfg.get("shift_tap_ms", 600) or 0)
    if ms <= 0:
        return ""
    # 不足 100 毫秒时别写成「0.0 秒内」——那是句废话，直接给毫秒数。
    window = f"{ms / 1000:.1f} 秒内" if ms >= 100 else f"{ms} 毫秒内"
    return f"点一下 Shift（{window}）"


def shift_tap_off_reason(cfg: dict) -> str:
    """Shift 单击为什么不可用（可用时返回空串）。给「现在没有切换键」那句话用。"""
    in_cfg = cfg.get("input") or {}
    if not bool(in_cfg.get("shift_tap_toggles", True)):
        return "input.shift_tap_toggles 关着"
    ms = int(in_cfg.get("shift_tap_ms", 600) or 0)
    if ms <= 0:
        return "input.shift_tap_ms 是 0（没有时间窗）"
    return ""


def shift_tap_problem(cfg: dict) -> str:
    """Shift 单击的配置有没有自相矛盾（没有返回空串）。

    第 90 轮真机：``shift_tap_toggles=true`` + ``shift_tap_ms=0`` 时，一边说开着、
    一边把时间窗设成零 —— ``IMECore.handle_key`` 里 ``held_ms <= self.shift_tap_ms``
    永不成立，Shift 单击完全无效，而 banner / 面板 / --check 都在教用户按它。
    另外 ``0 < ms < 50`` 人手来不及，也值得说一句。
    """
    in_cfg = cfg.get("input") or {}
    if not bool(in_cfg.get("shift_tap_toggles", True)):
        return ""
    ms = int(in_cfg.get("shift_tap_ms", 600) or 0)
    if ms <= 0:
        return ("input.shift_tap_ms 是 0 —— 时间窗为零，单击 Shift 永远不会触发，"
                "可 input.shift_tap_toggles 还开着；想用就把 shift_tap_ms 设成 "
                "50~2000，不想用就把 shift_tap_toggles 设成 false")
    if ms < 50:
        return (f"input.shift_tap_ms 只有 {ms} 毫秒 —— 人手来不及在这么短的窗口里松手，"
                f"单击 Shift 基本不会触发；建议 50~2000 毫秒")
    return ""


def direct_switch_hint(cfg: dict) -> str:
    """「拼音 ⇄ 英文直通」现在到底按什么：Shift 单击、``hotkeys.toggle_direct``，两个都算。

    都不可用时返回空串。注意 Shift 单击只有在输入法**没挂起**时才生效（挂起时按键
    全部透传、连 Shift 都不响应），所以「挂起之后怎么恢复」必须用
    :func:`resume_hint`，不能用这个。
    """
    ways = [tap_switch_hint(cfg)]
    spec = pretty((cfg.get("hotkeys") or {}).get("toggle_direct"))
    if spec:
        ways.append(spec)
    return " 或 ".join(w for w in ways if w)


def resume_hint(cfg: dict) -> str:
    """挂起之后按什么恢复：只有 ``hotkeys.toggle_enabled`` 管用。

    Shift 单击不行 —— ``IMECore.handle_key`` 里那条件是
    ``tapped and self.shift_tap_toggles and self.enabled``，挂起时 ``enabled`` 是假。
    所以面板/提示里说「按 Shift 恢复」是假的，这里只认 toggle_enabled。
    """
    return pretty((cfg.get("hotkeys") or {}).get("toggle_enabled"))


#: 「打字时手会按到」的键。热键的主键落在这里又没有修饰键时，那个键会被钩子吞掉、
#: 转而触发动作：打「q」就把输入法退了，打「1」就切了模式 —— 打字直接被毁。
#: 字母 / 数字 / 标点 / 空格 / 回车 / 退格 / 方向键都算；F1–F12、Pause、ScrollLock
#: 这类打字时不会碰的键不算，它们当单键热键是合理的。
_TYPING_VKS = frozenset(
    {0x08, 0x09, 0x0D, 0x1B, 0x20}                       # 退格 / Tab / 回车 / Esc / 空格
    | set(range(0x30, 0x3A))                             # 0-9
    | set(range(0x41, 0x5B))                             # A-Z
    | set(range(0x60, 0x70))                             # 小键盘 0-9 与运算键
    | set(range(0x21, 0x29))                             # PgUp/PgDn/End/Home/方向键
    | set(range(0xBA, 0xC1)) | set(range(0xDB, 0xE0))    # ;=,-./` 与 [\]' 等 OEM 标点
    | {0x2D, 0x2E, 0xE2, 0x5D, 0x14}                      # Ins / Del / OEM102 / 菜单 / CapsLock
)


def needs_modifier(vk: int) -> bool:
    """这个键当热键主键时，必须配一个修饰键（否则打字就会误触）。"""
    return int(vk) in _TYPING_VKS


#: ``VK_SHIFT``。这里不 import winapi（那会把 ctypes 拖进来），但 Shift 单独出现
#: 时必须特殊对待 —— 见 :func:`_combo_problem`。
_SHIFT_VK = 0x10


def _combo_problem(combo, spec: str) -> str:
    """已解析出来的组合键有什么毛病（没有毛病返回空串）。"""
    try:
        mods, vk = combo
    except Exception:
        return ""
    if not needs_modifier(vk):
        return ""
    if set(mods) - {_SHIFT_VK}:
        return ""
    if mods:
        # 只有 Shift：Shift+字母就是打大写字母，打一个大写 Q 就会触发这个动作。
        # 允许它等于把「打大写」变成「按热键」，比完全没修饰键还隐蔽（第 84 轮）。
        return ("只有 Shift 一个修饰键 —— 打大写字母时按的就是 Shift+这个键，"
                "会误触这个动作；请再加上 ctrl / alt / win，"
                "或者换成 F1–F12 这类打字时不碰的键")
    return (f"没有修饰键 —— 打字时按到 {pretty(spec)} 就会触发这个动作"
            f"（那个键也就打不出来了）；请加上 ctrl / alt / win，"
            f"或者换成 F1–F12 这类打字时不碰的键")


def spec_problem(spec: str, parse) -> str:
    """这个写法按下去会不会在打字时误触（解析不了返回空串，解析错另有话说）。"""
    try:
        combo = parse(spec)
    except Exception:
        return ""
    return _combo_problem(combo, spec)


def check_hotkeys(hotkeys: dict, parse) -> list[tuple[str, str, str]]:
    """逐个动作判一遍「它到底能不能生效」。

    ``parse`` 是 ``winapi.parse_hotkey``（这里不 import winapi，免得单纯想显示
    热键的地方把 ctypes 也拖进来）。

    返回 ``[(动作名, 配置里的写法, 问题)]``，按配置顺序；问题为空串表示这个动作
    真的能生效。问题文案已经是给用户看的人话。
    """
    out: list[tuple[str, str, str]] = []
    owner: dict[tuple, str] = {}
    for name, spec in (hotkeys or {}).items():
        spec = "" if spec is None else str(spec)
        if not spec.strip():
            out.append((str(name), spec, ""))
            continue
        if name not in HOTKEY_ACTIONS:
            out.append((str(name), spec,
                        f"动作名 {name!r} 不认识（支持的是 "
                        f"{', '.join(sorted(HOTKEY_ACTIONS))}）—— 这个组合键按了不会有任何反应"))
            continue
        try:
            combo = parse(spec)
        except Exception as exc:
            out.append((str(name), spec, f"解析失败：{exc}"))
            continue
        problem = _combo_problem(combo, spec)
        if problem:
            # 不占 owner：这个键根本不会注册，后面别的动作想用它不该被判成撞车
            out.append((str(name), spec, problem))
            continue
        prev = owner.get(combo)
        if prev is not None:
            out.append((str(name), spec,
                        f"和 {prev} 绑了同一个组合键 —— 实际生效的是 {prev}，这个动作用不了"))
            continue
        owner[combo] = str(name)
        out.append((str(name), spec, ""))
    return out
