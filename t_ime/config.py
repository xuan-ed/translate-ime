# -*- coding: utf-8 -*-
"""配置加载。

优先级：环境变量（敏感项） > config.json > 内置 DEFAULTS。
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

from . import paths as _paths

# 打包成 exe 之后 ROOT 必须是 **exe 旁边**（配置、日志、索引都写这儿），
# 而 config.example.json 躺在只读的解包目录里。见 t_ime/paths.py。
ROOT = _paths.app_root()
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = _paths.bundle_root() / "config.example.json"
if not EXAMPLE_PATH.exists():           # 源码跑时两者相同，这里只是兜底
    EXAMPLE_PATH = ROOT / "config.example.json"

DEFAULTS = {
    "api": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "path": "/chat/completions",
        "model": "deepseek-chat",
        "api_key": "",
        "timeout": 8.0,
        "temperature": 0.0,
    },
    "hotkeys": {
        # 一键直译上屏：跳过 Tab，直接把当前最佳译文注入目标窗口
        "compose": "ctrl+alt+space",
        # 切换「默认上屏语言」：zh <-> en
        "toggle_default": "ctrl+alt+l",
        # 临时挂起/恢复整个输入法（钩子仍在，但完全透传）
        "toggle_enabled": "ctrl+alt+z",
        # 切换「拼音输入 / 英文直通」。留空表示只用单击 Shift，不强占这个组合键。
        "toggle_direct": "",
        # 退出程序
        "quit": "ctrl+alt+q",
    },
    "output": {
        # 候选窗默认高亮哪一行：zh = 中文行，en = 英文行
        "default_language": "zh",
        # 启动时是否启用（false = 起来后先挂起，按 toggle_enabled 才接管键盘）
        "start_enabled": True,
        # 自学习权重落盘的文件（相对项目根目录）
        "boost_file": "user_boosts.json",
        # 选中非首选项时的学习强度，以及选中首选项时的轻微强化
        "boost_on_correct": 1.0,
        "boost_on_pick": 0.25,
        # 后台日志文件（相对项目根目录）；后台模式下 stdout/stderr 都进这里
        "log_file": "translate-ime.log",
        # 是否显示托盘图标（常驻时的主要操作入口）
        "tray": True,
    },
    "translate": {
        "debounce_ms": 250,
        "max_length": 280,
        "cache_size": 1024,
        "enabled": True,
        # 译文候选行最多显示几个
        "variants": 4,
    },
    "pinyin": {
        "max_candidates": 7,
        "beam_width": 24,
        "max_word_len": 6,
        "min_freq": 20,
        # 拼音索引缓存文件（相对项目根目录）
        "dict_cache": "pinyin_index.pkl",
    },
    "overlay": {
        "font_family": "Microsoft YaHei UI",
        "font_size": 16,
        "secondary_font_size": 14,
        "hint_font_size": 9,
        "min_width": 520,
        "max_width": 900,
        "bg": "#1b1c22",
        "fg": "#eceef5",
        "accent_bg": "#2f6fd0",
        "accent_fg": "#ffffff",
        "dim": "#8b8d9a",
        "border": "#3a3c47",
        "row_bg": "#24262e",
        "row_active_bg": "#2b2e39",
        "sel_soft": "#3d4150",
        "alpha": 0.97,
        "offset_y": 28,
    },
    "input": {
        # 上屏方式只有一种：SendInput 注入 Unicode 字符（不经过剪贴板）。
        # 早期模板里还有 commit_method / commit_delay_ms 两个选项，但代码从来
        # 没读过它们 —— 改了也毫无变化。见 RETIRED_KEYS。
        # 单击 Shift 切换「拼音 / 英文直通」：按住多久以内算「单击」。
        # 600ms 而不是 300ms：从容地「按一下去切模式」常常超过 300ms，那会变成
        # 什么都不发生（用户第 20 轮的反馈就是这个）；又不能取消上限，否则
        # Shift+鼠标点击（期间没有按键事件）会被当成单击、把模式悄悄翻过去。
        "shift_tap_ms": 600,
        # 关掉它就完全不响应单击 Shift（有些人习惯 Shift 只是用来打大写）
        "shift_tap_toggles": True,
        # 候选窗上短提示（模式切换、挂起/恢复）显示多久
        "toast_ms": 1600,
    },
    "ui": {
        # 启动时自动弹出图形设置窗口（t_ime/panel.py）。
        # 为什么默认开：托盘图标在低完整性环境里装不上，控制台菜单又改不了
        # API key / 热键 —— 一个刚拿到这个工具的人，靠这个窗口才知道它有哪些
        # 功能、当前为什么不能用（第 124 轮）。
        "open_panel_on_start": True,
        # 窗口左边栏显示「三步上手」引导
        "guide": True,
    },
}


# 用户手改 config.json 时最常见的两种毁法：
#   1. 数字写成字符串（"variants": "four"）—— App 构造时 int() 抛 ValueError，
#      连 --check 都一起崩，屏幕上只剩 traceback，看不出是哪一项；
#   2. 数字写成 0 或负数（"max_candidates": 0）—— 不崩，但候选窗一个候选都不画，
#      看起来就是「输入法坏了」。
# 这里按「能继续用」优先：数值不合规就退回默认值，并留下一句人话告诉用户
# 是哪一项、写的是什么、实际用了什么。范围给得宽松 —— 只挡明显写错的值。
_INT_SPECS: dict[tuple[str, str], tuple[float, float]] = {
    ("pinyin", "max_candidates"): (1, 20),
    ("pinyin", "beam_width"): (1, 256),
    ("pinyin", "max_word_len"): (1, 12),
    ("pinyin", "min_freq"): (0, 10 ** 9),
    ("translate", "variants"): (1, 8),
    ("translate", "debounce_ms"): (0, 5000),
    ("translate", "max_length"): (1, 20000),
    ("translate", "cache_size"): (1, 200000),
    ("input", "shift_tap_ms"): (0, 2000),
    ("input", "toast_ms"): (0, 60000),
    ("overlay", "font_size"): (6, 72),
    ("overlay", "secondary_font_size"): (6, 72),
    ("overlay", "hint_font_size"): (6, 72),
    ("overlay", "min_width"): (100, 4000),
    ("overlay", "max_width"): (200, 8000),
    ("overlay", "offset_y"): (-500, 5000),
}
_FLOAT_SPECS: dict[tuple[str, str], tuple[float, float]] = {
    ("output", "boost_on_correct"): (0.0, 100.0),
    ("output", "boost_on_pick"): (0.0, 100.0),
    ("overlay", "alpha"): (0.2, 1.0),
    ("api", "timeout"): (0.5, 120.0),
    ("api", "temperature"): (0.0, 2.0),
}
_BOOL_SPECS: list[tuple[str, str]] = [
    ("translate", "enabled"),
    ("output", "start_enabled"),
    ("output", "tray"),
    ("input", "shift_tap_toggles"),
    ("ui", "open_panel_on_start"),
    ("ui", "guide"),
]

# 字符串型选项：写成数字 / 数组 / null 时，上面的数值白名单管不到，而这些值会被
# 直接拿去拼路径、建 Tk 控件 —— 崩点在启动里，不在配置解析里：
#   "dict_cache": 5   → app.py 里 路径 / 5 → TypeError，App 构造失败
#   "bg": 5           → tk.Label(bg=5) → TclError: unknown color name "5"
# 后台模式下用户只看到「双击一下，什么都没发生」，日志里是一段原始 traceback。
# 所以这里按「必须是一段文字」挡一层（value 是给用户看的人话名字）。
_STR_SPECS: dict[tuple[str, str], str] = {
    ("pinyin", "dict_cache"): "缓存文件路径",
    ("output", "boost_file"): "自学习权重文件路径",
    ("output", "log_file"): "日志文件路径",
    ("overlay", "font_family"): "字体名",
    ("api", "base_url"): "接口地址",
    ("api", "path"): "接口路径",
    ("api", "model"): "模型名",
}
# 允许空串的字符串项（api_key 没配是正常状态）
_STR_ALLOW_EMPTY: set[tuple[str, str]] = {("api", "api_key")}

# 颜色项：写数字同样在 tk.Label 上炸；写成残缺的 #hex 也在 Tk 里才炸。
# Tk 也认英文颜色名（red / white），所以不是 # 开头的一律放行。
_COLOR_KEYS: list[tuple[str, str]] = [
    ("overlay", "bg"),
    ("overlay", "fg"),
    ("overlay", "accent_bg"),
    ("overlay", "accent_fg"),
    ("overlay", "dim"),
    ("overlay", "border"),
    ("overlay", "row_bg"),
    ("overlay", "row_active_bg"),
    ("overlay", "sel_soft"),
]
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# 只能取固定几个值、写错也不报错的选项：default_language 写 "jp" 时程序照常起来、
# 高亮行悄无声息地留在中文 —— 又是一个「改了没反应」。
_ENUM_SPECS: dict[tuple[str, str], tuple[str, ...]] = {
    ("output", "default_language"): ("zh", "en"),
    ("api", "provider"): ("deepseek", "mock"),
}

# 最近一次 load() 发现的问题（人话，一行一项）。main.py 拿它来提示用户。
LAST_PROBLEMS: list[str] = []

# 最近一次 load() 发现「DEFAULTS 不认识」的键（点分名）。和写错类型不同，
# 这些键**不会报错也不会生效**：用户改完配置、重启、发现毫无变化，程序一声不吭。
# main.py 拿它把同一句话也写进日志（后台模式 stdout 是 DEVNULL）。
LAST_UNKNOWN: list[str] = []

# 最近一次 load() 是不是**整份配置都没读成**（JSON 语法错、文件读不动、根不是
# ``{...}`` 对象）。这种情况程序照常起来、全部用内置默认值 —— 但后台模式 stdout
# 是 DEVNULL，那句「你的配置被整份忽略了」只能靠 main.py 补进日志。空字符串表示
# 这一轮没出这种事。
LAST_LOAD_ERROR: str = ""

# 早期模板里发出去过、但代码从来没有读过的选项。它们不是拼写错（用户是从我们
# 自己的 config.example.json 里抄来的），也已经从模板里删掉了；老配置里还留着时
# 要说一句「这个一直没生效」，而不是让用户继续以为改了有用。
RETIRED_KEYS: dict[str, str] = {
    "input.commit_method": (
        "上屏方式一直只有 SendInput 注入 Unicode 这一种（不经过剪贴板），"
        "写 clipboard 也还是走 SendInput，这个选项已从模板删除"
    ),
    "input.commit_delay_ms": (
        "注入前等待从来没有实现（在键盘钩子回调里 sleep 会卡住整机按键），"
        "这个选项已从模板删除"
    ),
}

# 最近一次 load() 在用户配置里发现的、属于上面那批的键（点分名）。
LAST_RETIRED: list[str] = []


def _show(v: object) -> str:
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(v)


def sanitize(cfg: dict) -> list[str]:
    """就地修正 cfg 里写错的项，返回人话问题清单（可能为空）。

    只动明显写错的：段不是对象、数字类型不对、超出宽松范围、布尔写成别的。
    缺项不在此处理 —— `_merge` 已经用默认值补齐了。
    """
    problems: list[str] = []
    for section, dflt in DEFAULTS.items():
        if not isinstance(cfg.get(section), dict):
            problems.append(
                f"{section} 应该是一整段配置（{{...}}），现在是 {_show(cfg.get(section))}"
                f" —— 用默认值"
            )
            cfg[section] = copy.deepcopy(dflt)

    def _bad(section: str, key: str, msg: str) -> None:
        problems.append(
            f"{section}.{key} {msg}，现在是 {_show(cfg[section].get(key))}"
            f" —— 用默认值 {_show(DEFAULTS[section][key])}"
        )
        cfg[section][key] = copy.deepcopy(DEFAULTS[section][key])

    for (section, key), (lo, hi) in _INT_SPECS.items():
        cur = cfg[section].get(key)
        if isinstance(cur, bool) or not isinstance(cur, (int, float)):
            _bad(section, key, f"应该是整数（{lo:g}~{hi:g}）")
            continue
        if isinstance(cur, float) and not float(cur).is_integer():
            _bad(section, key, f"应该是整数（{lo:g}~{hi:g}）")
            continue
        val = int(cur)
        if not lo <= val <= hi:
            _bad(section, key, f"超出范围 {lo:g}~{hi:g}")
            continue
        cfg[section][key] = val

    for (section, key), (lo, hi) in _FLOAT_SPECS.items():
        cur = cfg[section].get(key)
        if isinstance(cur, bool) or not isinstance(cur, (int, float)):
            _bad(section, key, f"应该是数字（{lo:g}~{hi:g}）")
            continue
        val = float(cur)
        if not lo <= val <= hi:
            _bad(section, key, f"超出范围 {lo:g}~{hi:g}")
            continue
        cfg[section][key] = val

    for section, key in _BOOL_SPECS:
        cur = cfg[section].get(key)
        if not isinstance(cur, bool):
            _bad(section, key, "应该是 true 或 false")

    # 字符串项：写成数字 / 数组 / null 会在启动时崩（拼路径、建控件），不是解析时崩
    for (section, key), human in _STR_SPECS.items():
        cur = cfg[section].get(key)
        if not isinstance(cur, str) or not cur.strip():
            _bad(section, key, f"应该是{human}（一段非空文字）")
    for section, key in _STR_ALLOW_EMPTY:
        cur = cfg[section].get(key)
        if not isinstance(cur, str):
            _bad(section, key, "应该是一段文字（可以是空串）")

    for section, key in _COLOR_KEYS:
        cur = cfg[section].get(key)
        if not isinstance(cur, str) or not cur.strip():
            _bad(section, key, "应该是颜色（#rrggbb 或英文颜色名）")
            continue
        val = cur.strip()
        if val.startswith("#") and not _HEX_RE.match(val):
            _bad(section, key, "应该是颜色（#rrggbb 或英文颜色名）")
            continue
        cfg[section][key] = val

    for (section, key), options in _ENUM_SPECS.items():
        cur = cfg[section].get(key)
        if not isinstance(cur, str) or cur.strip().lower() not in options:
            _bad(section, key, "只能是 " + " / ".join(options) + " 之一")
            continue
        cfg[section][key] = cur.strip().lower()

    return problems


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: os.PathLike | str | None = None) -> tuple[dict, Path]:
    """返回 (配置字典, 实际使用的配置文件路径)。"""
    p = Path(path) if path else CONFIG_PATH
    global LAST_LOAD_ERROR  # load() 会写它；不声明这句，下面所有赋值都会变成局部变量
    raw: dict = {}
    failure = ""
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # 配置坏了不该让程序起不来
            # 常见的两种「改了配置却没反应」：JSON 写坏了（尾逗号、少个引号），
            # 或者存成了带 BOM 的 UTF-8。后者由上面的 utf-8-sig 兜住；前者只能
            # 忽略整份文件。
            failure = f"读取 {p} 失败：{exc}"
        else:
            # 顶层不是对象也别让它崩：以前 `_merge` 会对着 list/str 调 .items()，
            # 直接 AttributeError，而这是在日志建好之前 —— 用户看到的就是
            # 「双击一下，什么都没发生」。和 JSON 写坏一样按「整份忽略」处理。
            if not isinstance(raw, dict):
                failure = (
                    f"{p} 的顶层不是一个 JSON 对象（拿到的是"
                    f" {type(raw).__name__}: {_show(raw)[:80]}）"
                )
                raw = {}
    if failure:
        message = f"{failure}，整份配置被忽略、改用内置默认值"
        # 和类型问题一样，一次运行里 load() 常被调两遍，别刷两次。
        if message != LAST_LOAD_ERROR:
            print(f"[config] {message}")
        LAST_LOAD_ERROR = message
    else:
        LAST_LOAD_ERROR = ""
    cfg = _merge(DEFAULTS, raw)
    problems = sanitize(cfg)
    # 同一次运行里 config 常被加载两遍（main 一次、具体命令再一次），
    # 提示只该说一遍；内容相同就不重复刷。
    if problems and problems != LAST_PROBLEMS:
        print(f"[config] {p} 里有 {len(problems)} 项写错，已按默认值启动：")
        for msg in problems:
            print(f"[config]   - {msg}")
    LAST_PROBLEMS.clear()
    LAST_PROBLEMS.extend(problems)

    # 拼错的键（比如把 hotkeys 写成 hotkey、把 font_size 写成 font_szie）不会
    # 报错，只会**永远不生效** —— 「改了配置却没反应」最常见的就是这个。类型写错
    # 至少会退回默认值，拼错的键连默认值都没有：用户以为改了，其实那句话没落到
    # 任何地方。所以在启动时也点一次名，而且同样只点一遍。
    bad_keys: list[str] = []
    problem_prefixes = type_problem_prefixes(problems)
    for key in unknown_keys(raw):
        if key.startswith("_"):
            continue  # 下划线开头的段当作用户注释（_comment / _notes），不唠叨
        if key.startswith("hotkeys."):
            continue  # 动作名写错由 core._parse_hotkeys 点名，那里说得更具体
        # 整段写成标量（"output": 5）已经在上面按类型问题报过了，别报两遍
        if any(key == p or key.startswith(p + ".") for p in problem_prefixes):
            continue
        bad_keys.append(key)
    if bad_keys and bad_keys != LAST_UNKNOWN:
        print(f"[config] {p.name} 里有 {len(bad_keys)} 个选项我不认识（多半是拼写错），它们不会生效：")
        for key in bad_keys:
            print(f"[config]   - {key}")
        print("[config]     合法选项见 config.example.json；每个选项的实际生效值跑 run.bat --show-config 看")
    LAST_UNKNOWN.clear()
    LAST_UNKNOWN.extend(bad_keys)

    # 我们从自己的模板里发出去过、后来发现从来没接上的选项。用户照着
    # config.example.json 抄下来、改了值、重启，程序一声不吭 —— 和拼错的键
    # 一样是「改了却没反应」，但原因不同：不是他写错，是我们当年没做完。
    # 模板已经删掉了，老配置里还留着的就在启动时说一句，并把这些键从生效
    # 配置里摘掉（留着只会让 cfg 里带一个「文档里已经没有」的键）。
    retired = sorted(k for k in flat_keys(raw) if k in RETIRED_KEYS)
    for key in retired:
        section, _, leaf = key.partition(".")
        table = cfg.get(section)
        if isinstance(table, dict):
            table.pop(leaf, None)
    if retired and retired != LAST_RETIRED:
        print(f"[config] {p.name} 里有 {len(retired)} 个从旧模板留下的选项，从来没生效过，已从模板删掉：")
        for key in retired:
            print(f"[config]   - {key}：{RETIRED_KEYS[key]}")
    LAST_RETIRED.clear()
    LAST_RETIRED.extend(retired)

    env = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("T_IME_API_KEY")
    if env and env.strip():
        cfg["api"]["api_key"] = env.strip()
    return cfg, p


def read_raw(path: os.PathLike | str | None = None) -> tuple[dict, Path]:
    """读**原始** config.json：不做深合并，也不套环境变量。

    返回 ``(原文, 路径)``。文件不存在、读不动、坏 JSON、或者根不是对象，
    都返回 ``({}, 路径)`` —— 由调用方决定怎么提示。
    """
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        return {}, p
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}, p
    return (raw if isinstance(raw, dict) else {}), p


def flat_keys(d: dict, prefix: str = "") -> dict[str, object]:
    """把嵌套配置摊平成 ``{点分键名: 值}``；中间层不是 dict 就按叶子算。"""
    out: dict[str, object] = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flat_keys(v, key + "."))
        else:
            out[key] = v
    return out


def dict_paths(d: dict, prefix: str = "") -> set[str]:
    """所有**子字典**的点分路径（不含叶子）。空 dict 也算一个路径。"""
    out: set[str] = set()
    for k, v in (d or {}).items():
        if isinstance(v, dict):
            key = f"{prefix}{k}"
            out.add(key)
            out |= dict_paths(v, key + ".")
    return out


def missing_keys(raw: dict) -> list[str]:
    """``raw`` 里没写的选项，点分名，排序返回。

    它们**并没有失效**：:func:`load` 会把 :data:`DEFAULTS` 深合并进去，
    只是你在 config.json 里看不到、也就想不到去改。
    """
    return sorted(set(flat_keys(DEFAULTS)) - set(flat_keys(raw)))


def unknown_keys(raw: dict) -> list[str]:
    """``raw`` 里 :data:`DEFAULTS` 不认识的键，点分名，排序返回。

    最常见的来源是拼错（``hotkey`` 写成了单数）。程序只会**静静忽略**这些
    键，永远不报错 —— 所以「改了配置却没反应」经常出在这里。

    只报**最具体**的那一层：``{"hotkey": {"compose": "x"}}`` 报
    ``hotkey.compose`` 而不是连 ``hotkey`` 一起报；但 ``{"hotkey": {}}``
    这种空段没有叶子可报，就报 ``hotkey`` 本身。

    已经从模板删掉的早期选项（:data:`RETIRED_KEYS`）不算「不认识」：它们不是
    用户手滑拼出来的，而是我们自己的模板发出去过的，由 :func:`load` 单独解释。
    """
    known_leaves = set(flat_keys(DEFAULTS))
    known_dicts = dict_paths(DEFAULTS)
    bad = {
        k for k in flat_keys(raw)
        if k not in known_leaves and k not in RETIRED_KEYS
    }
    for path in sorted(dict_paths(raw)):
        if path in known_dicts or path in known_leaves:
            continue
        if any(k.startswith(path + ".") for k in bad):
            continue  # 已经把这个段里的具体键点出来了
        bad.add(path)
    return sorted(bad)


def type_problem_prefixes(problems: list[str] | None = None) -> set[str]:
    """``sanitize()`` 报过「整段类型写错」的那些段（``hotkeys`` / ``output`` …）。

    ``problems`` 里每条消息都以段名开头（``hotkeys 应该是一整段配置…``），
    取第一个词就是段名。:func:`load` 用它把整段写错的键从「不认识的键」里
    排掉（那是值写错，不是拼错）。``--show-config`` 必须用**同一个口径**，
    否则同一次运行里两处会给出互相矛盾的说法和计数（第 122 轮的真机复现：
    ``[config]`` 说「1 个选项我不认识」，``--show-config`` 却说「2 个键
    DEFAULTS 不认识」，其中一个是它自己刚说过「现在是 5」的 ``hotkeys``）。
    """
    src = LAST_PROBLEMS if problems is None else problems
    return {msg.split(" ", 1)[0] for msg in src}


def get_dotted(d: dict, dotted: str, default=None):
    """按点分名取值；路径断了、或者中间层不是 dict，都返回 ``default``。"""
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def env_api_key() -> str:
    """环境变量里给的 key（优先级高于 config.json）。"""
    for name in ("DEEPSEEK_API_KEY", "T_IME_API_KEY"):
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return ""


def effective_rows(cfg: dict, raw: dict) -> list[tuple[str, str, object]]:
    """返回 ``[(点分键名, 来源, 生效值)]``。

    来源是 ``"file"``（写在 config.json 里）、``"default"``（吃内置默认值）、
    ``"env"``（环境变量盖过文件）、``"ignored"``（DEFAULTS 不认识，被忽略）。
    """
    flat_raw = flat_keys(raw)
    flat_cfg = flat_keys(cfg)
    rows: list[tuple[str, str, object]] = []
    for key, val in flat_cfg.items():
        if key == "api.api_key" and env_api_key():
            src = "env"
        elif key in flat_raw:
            src = "file"
        else:
            src = "default"
        rows.append((key, src, val))
    for key in unknown_keys(raw):
        rows.append((key, "ignored", get_dotted(raw, key)))
    return rows


def write_example(path: os.PathLike | str | None = None) -> Path:
    """把默认配置写成 ``config.example.json``（不含任何密钥）。

    这个文件是**入库**的模板；用户复制成 ``config.json`` 再改，
    而 ``config.json`` 在 .gitignore 里，永远不会被提交。
    """
    p = Path(path) if path else EXAMPLE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(DEFAULTS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return p


def init_config(path: os.PathLike | str | None = None) -> Path:
    """把 config.example.json 复制成 config.json，已存在则不覆盖。"""
    dst = Path(path) if path else CONFIG_PATH
    if dst.exists():
        return dst
    if not EXAMPLE_PATH.exists():
        write_example()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(EXAMPLE_PATH.read_text(encoding="utf-8-sig"), encoding="utf-8")
    return dst
