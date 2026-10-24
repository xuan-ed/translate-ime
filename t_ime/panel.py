# -*- coding: utf-8 -*-
"""图形设置窗口 —— 双击 ``run.bat`` 默认弹出来的那个窗口。

为什么要有它（第 124 轮）：

* **托盘装不上**：``Shell_NotifyIcon`` 在低完整性进程里只回 ``ACCESS_DENIED``，
  而托盘是「改设置」唯一的图形入口，于是被降权启动的人一个入口都没有；
* **控制台菜单改不了东西**：第二个实例的 7 项菜单只能看状态、按数字选动作，
  API key / 热键 / 候选窗外观全都得自己去翻 ``config.json``；
* **Web 控制板门槛高**：能用，但要开浏览器、记 ``127.0.0.1:8765``，
  而且第一次用的人根本不知道有这回事。

所以这个窗口 = Web 控制板的表单 + 控制台菜单的状态解读 + 一份权限体检。
它自己**不碰键盘钩子**：保存走 :func:`t_ime.webpanel.save_panel_config`
（校验 → 落盘 → 给常驻实例投 reload），跟 Web 控制板是同一条路，
不存在「窗口里改的和实例读到的不一样」这种事。

诊断口径是实的，不是安慰话：低权限实例的钩子能装、能报「已装上」，
但**永远收不到** Medium 完整性窗口（资源管理器、Edge、Windows Terminal）
里的按键 —— 这一点只有看完整性级别才看得出来，所以 :func:`diagnose`
把它排在所有提示的最前面。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from . import config as C
from . import daemon

PANEL_TITLE = "translate-ime 设置"

# 配色跟候选窗的默认深色外观对齐：两个窗口得让人看出是同一个软件。
BG = "#1b1c22"
CARD = "#23252d"
FIELD_BG = "#2c2f39"
FG = "#eceef5"
DIM = "#8f92a1"
BORDER = "#3b3e4a"
ACCENT = "#3d7dd8"
OK = "#41b57a"
WARN = "#d8a33c"
BAD = "#e0605c"

# 界面上用它代表「API key 已经存过一份，但我不告诉你原文」。
MASK_FOR_UI = "********"

# 窗口专用的小文件：防止连点两下 run.bat 开出两个一模一样的设置窗口。
LOCK_NAME = ".panel.pid"

# 完整性级别 RID：不高于 0x1000（Low / Untrusted）就看不见普通窗口的按键。
LOW_INTEGRITY_MAX = 0x1000


# --------------------------------------------------------------------------- #
# 表单定义
# --------------------------------------------------------------------------- #
def _f(label: str, path: str, kind: str = "text", hint: str = "",
       options: tuple[tuple[str, str], ...] = ()) -> dict:
    """一个可编辑项。``kind``: text/password/int/float/bool/choice/hotkey。"""
    return {"label": label, "path": path, "kind": kind, "hint": hint,
            "options": options}


# 顺序 = 界面上从上到下的顺序。每组第一项决定卡片标题。
SECTIONS: list[tuple[str, str, list[dict]]] = [
    ("翻译模型（AI）", "填了 API key 才会真的调模型；留空就用内置离线词表，不联网", [
        _f("API key", "api.api_key", "password",
           "存在 config.json 里，界面只显示 ********；要清空请按「清空 key」"),
        _f("服务商", "api.provider", "choice",
           "mock = 内置离线词表（不联网）。指到别的 OpenAI 兼容服务时仍填 "
           "deepseek，只改下面的接口地址和模型名",
           (("deepseek", "DeepSeek / 兼容接口"), ("mock", "离线词表（mock）"))),
        _f("接口地址", "api.base_url", "text", "例如 https://api.deepseek.com"),
        _f("接口路径", "api.path", "text", "一般不用改：/chat/completions"),
        _f("模型名", "api.model", "text", "例如 deepseek-chat"),
        _f("超时（秒）", "api.timeout", "float", "翻译请求最多等多久"),
        _f("温度", "api.temperature", "float", "0 = 尽量稳定；翻译一般 0~0.6"),
    ]),
    ("翻译行为", "", [
        _f("启用翻译", "translate.enabled", "bool",
           "关掉就只剩拼音→中文，完全不联网"),
        _f("防抖（毫秒）", "translate.debounce_ms", "int",
           "停顿多久才发请求；太小会把每个字都发一遍"),
        _f("候选条数", "translate.variants", "int", "一次给几个译法（1~8）"),
        _f("最长字符", "translate.max_length", "int",
           "缓冲区超过这个长度就不再翻译"),
        _f("缓存条数", "translate.cache_size", "int",
           "问过的词直接命中缓存（LRU）"),
    ]),
    ("快捷键", "写成 ctrl+alt+space 这种；点「按一下」可以直接捕获", [
        _f("上屏译文", "hotkeys.compose", "hotkey",
           "默认 Ctrl+Alt+Space：把候选 1 上屏成当前译文"),
        _f("切换默认语言", "hotkeys.toggle_default", "hotkey",
           "默认 Ctrl+Alt+L：中文上屏 ←→ 译文上屏"),
        _f("挂起 / 恢复", "hotkeys.toggle_enabled", "hotkey",
           "默认 Ctrl+Alt+Z"),
        _f("切换英文直通", "hotkeys.toggle_direct", "hotkey",
           "默认空着：靠单击 Shift 切。留空 = 不绑键"),
        _f("退出", "hotkeys.quit", "hotkey",
           "默认 Ctrl+Alt+Q；改完记得自己还按得出来"),
    ]),
    ("按键与提示", "Shift 单击切模式、候选窗上的短提示", [
        _f("Shift 双击间隔（毫秒）", "input.shift_tap_ms", "int",
           "两下 Shift 之间的最长间隔；超过就当两次独立的 Shift"),
        _f("单击 Shift 也切模式", "input.shift_tap_toggles", "bool",
           "关掉只认双击（游戏或快捷键很多时能减少误触）"),
        _f("提示时长（毫秒）", "input.toast_ms", "int",
           "模式切换的短提示停留多久"),
    ]),
    ("上屏与自学习", "", [
        _f("默认上屏", "output.default_language", "choice",
           "敲完拼音默认上屏哪种：中文，还是直接给译文",
           (("zh", "中文"), ("en", "译文（英语）"))),
        _f("启动时启用", "output.start_enabled", "bool",
           "关掉则以「挂起」状态起来，按热键才接管键盘"),
        _f("显示托盘图标", "output.tray", "bool",
           "低权限启动时装不上，会退回到「只能靠这个窗口和热键」"),
        _f("自学习：选对加成", "output.boost_on_correct", "float",
           "候选 1 被直接上屏时给它的加分"),
        _f("自学习：改选加成", "output.boost_on_pick", "float",
           "手动改选别的候选时，给那个词的加分"),
        _f("自学习文件", "output.boost_file", "text",
           "记在哪个文件（相对 main.py）"),
        _f("日志文件", "output.log_file", "text", "改这里要重启实例才生效"),
    ]),
    ("拼音", "改这一组要重建索引，重启实例才生效", [
        _f("候选数", "pinyin.max_candidates", "int",
           "候选窗里一次显示几个字词"),
        _f("搜索宽度", "pinyin.beam_width", "int",
           "越大越准也越慢（默认 24）"),
        _f("最长词长", "pinyin.max_word_len", "int",
           "一句话最多连几个字成词"),
        _f("词频门槛", "pinyin.min_freq", "int",
           "低于它的词不进索引；调大能收敛候选"),
        _f("索引缓存", "pinyin.dict_cache", "text",
           "删掉这个文件会在下次启动时重建"),
    ]),
    ("候选窗外观", "改完要重启实例才生效；颜色在 config.json 的 overlay 里改", [
        _f("字体", "overlay.font_family", "text", "例如 Microsoft YaHei UI"),
        _f("字号", "overlay.font_size", "int", "逻辑像素，会按 DPI 缩放"),
        _f("副字号", "overlay.secondary_font_size", "int", "译文那两行用多大"),
        _f("提示字号", "overlay.hint_font_size", "int", "右上角短提示用多大"),
        _f("最小宽度", "overlay.min_width", "int", "逻辑像素"),
        _f("最大宽度", "overlay.max_width", "int", "逻辑像素"),
        _f("不透明度", "overlay.alpha", "float", "0.2~1.0，1.0 完全不透明"),
        _f("离光标距离", "overlay.offset_y", "int", "候选窗顶部离光标多少像素"),
    ]),
    ("这个窗口", "", [
        _f("启动时自动打开", "ui.open_panel_on_start", "bool",
           "关掉后双击 run.bat 不再弹窗；实例已在跑时会退到控制台菜单"),
        _f("显示上手引导", "ui.guide", "bool", "左侧「三步上手」那张卡"),
    ]),
]

#: 拍平后的全部可编辑项（测试用它核对「表单里的路径都得是配置里真有的」）。
FIELDS: list[dict] = [f for _title, _sub, items in SECTIONS for f in items]

#: 主键：改动它要清空旧值而不是写入掩码。
KEY_FIELD = "api.api_key"


# --------------------------------------------------------------------------- #
# 纯逻辑：配置 <-> 表单
# --------------------------------------------------------------------------- #
def _get(obj: Any, path: str, default: Any = None) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
    return default if cur is None else cur


def _set(obj: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def coerce_number(kind: str, text: Any) -> Any:
    """把输入框里的文字变成数字；变不成就把原文交回去，让校验去报错。

    为什么不在这里兜底成默认值：``config.sanitize`` 已经把「非法值」分成
    「修正」和「报错」两类，这里悄悄替换会把「你填的 3.5 被换成 2」这种
    事情藏起来 —— 界面上必须说出来。
    """
    if isinstance(text, (int, float)):
        return text
    raw = str(text or "").strip()
    if not raw:
        return raw
    try:
        val = float(raw)
    except ValueError:
        return raw
    if kind == "int":
        return int(val) if float(val).is_integer() else val
    return val


def values_from_config(cfg: dict) -> dict:
    """把配置摊成「表单路径 → 值」。key 用掩码，不把原文送进界面。"""
    out: dict[str, Any] = {}
    for f in FIELDS:
        path = f["path"]
        val = _get(cfg, path)
        if val is None:
            # 配置文件里缺这一段时也要显示默认值：留空会让保存把默认值写成空串
            val = _get(C.DEFAULTS, path)
        if f["kind"] == "password":
            val = MASK_FOR_UI if val else ""
        out[path] = val
    return out


def apply_values(cfg: dict, values: dict, *, key_touched: bool = False,
                 clear_key: bool = False) -> dict:
    """把表单值合并回配置，返回**新**字典（不改传进来的那份）。

    ``api_key`` 单独处理：界面里显示的是 ``********``，原样写回去就等于把
    用户的 key 换成一串星号（第 124 轮最容易犯的错）；只有用户真的动过那个
    输入框（``key_touched``）才写。
    """
    out = json.loads(json.dumps(cfg, ensure_ascii=False))
    for path, value in values.items():
        if path == KEY_FIELD and not key_touched:
            continue
        if path == KEY_FIELD:
            value = str(value or "")
        elif path == "api.api_key_clear":
            continue
        _set(out, path, value)
    if clear_key:
        _set(out, KEY_FIELD, "")
        # save_panel_config 的 _merge_secret 见到空串会「保留旧 key」，
        # 想真删掉必须带上这个显式标记。
        _set(out, "api.api_key_clear", True)
    elif key_touched:
        _set(out, "api.api_key_clear", False)
    return out


# --------------------------------------------------------------------------- #
# 纯逻辑：诊断
# --------------------------------------------------------------------------- #
def _row(label: str, value: str, level: str = "ok") -> dict:
    return {"label": label, "value": value, "level": level}


def _banner(level: str, title: str, detail: str = "",
            action: str = "") -> dict:
    return {"level": level, "title": title, "detail": detail, "action": action}


def diagnose(cfg: dict, st: dict | None, running: bool,
             integrity: tuple[int, str] | None = None,
             autostart: str | None = None) -> dict:
    """把状态文件 + 完整性级别翻成「人话 + 该怎么办」。

    返回 ``{"rows": [...], "banner": {...}, "ok": bool}``。纯函数（不碰注册表、
    不读文件），这样测试才能逐个口径验证 —— 这个窗口的价值全在「说得对」上。
    """
    st = st or {}
    rows: list[dict] = []

    # ---- 权限 ----
    rid, iname = (integrity if integrity else (-1, "读不到"))
    low = 0 <= int(rid) <= LOW_INTEGRITY_MAX
    rows.append(_row("权限", iname, "bad" if low else
                     ("warn" if rid < 0 else "ok")))

    # ---- 钩子 ----
    info = st.get("hook_info") or {}
    real = int(info.get("real") or 0)
    total = int(info.get("total") or 0)
    hook_ok = bool(st.get("hook"))
    if hook_ok:
        rows.append(_row("键盘钩子", f"已装上，收到 {real} 个真实按键"
                         + (f"（共 {total} 次回调）" if total else ""),
                         "ok" if real else "warn"))
    else:
        rows.append(_row("键盘钩子", "没装上", "bad"))

    # ---- 托盘 ----
    tray = bool(st.get("tray"))
    rows.append(_row("托盘图标", "已显示" if tray else "装不上（不影响打字）",
                     "ok" if tray else "warn"))

    # ---- 候选窗 ----
    ov = st.get("overlay") or {}
    shows = int(ov.get("shows") or 0)
    cand = int(ov.get("cand_shows") or 0)
    ov_err = str(ov.get("error") or "")
    rows.append(_row("候选窗", f"显示过 {shows} 次（候选 {cand} 次）"
                     + (f"，异常：{ov_err}" if ov_err else ""),
                     "bad" if ov_err else "ok"))

    # ---- 翻译 ----
    tr = st.get("translate") or {}
    if not bool(tr.get("enabled", _get(cfg, "translate.enabled", True))):
        rows.append(_row("翻译", "已关闭（只做拼音→中文）", "warn"))
    elif tr.get("mock"):
        rows.append(_row("翻译", f"离线词表：{tr.get('mock_reason') or '没配 API key'}", "warn"))
    else:
        rows.append(_row("翻译", f"{tr.get('provider') or '?'} 在线，"
                         f"已请求 {int(tr.get('requests') or 0)} 次"
                         f"，缓存命中 {int(tr.get('cache_hits') or 0)} 次", "ok"))

    # ---- 自学习 ----
    boosts = st.get("boosts")
    if isinstance(boosts, int):
        rows.append(_row("自学习", f"{boosts} 个词有偏好", "ok"))
    else:
        rows.append(_row("自学习", "还没有记录", "ok"))

    # ---- 开机自启 ----
    if autostart is not None:
        rows.append(_row("开机自启", autostart or "未知", "ok"))

    # ---- 实例 ----
    if running:
        rows.append(_row("实例", f"pid {st.get('pid') or '?'}", "ok"))
    else:
        rows.append(_row("实例", "没在运行", "bad"))

    # ---- 结论：只留一条最重要的 ----
    if not running:
        banner = _banner("bad", "输入法没在运行",
                         "点「启动输入法」。启动后这个窗口会自动刷新状态。",
                         "start")
    elif rid >= 0 and low:
        banner = _banner(
            "bad", "当前实例权限不足 —— 打字不会有任何反应",
            "Windows 的 UIPI 不让低完整性进程看到普通窗口的按键：钩子虽然装上了，"
            "但资源管理器、Edge、终端里敲的字它一个都收不到（托盘也因此装不上）。"
            "点下面的按钮用管理员身份重启，或在任务栏右键 run.bat 选"
            "「以管理员身份运行」。",
            "elevate")
    elif not hook_ok:
        banner = _banner(
            "bad", "键盘钩子没装上",
            f"{st.get('hook_error') or '原因不一定写在状态里'}。"
            "常见原因：安全软件拦截全局钩子、同时开了两个实例。",
            "log")
    elif not real:
        banner = _banner(
            "warn", "还没收到过真实按键",
            "如果敲拼音没反应：先在记事本里试；还不行就换用管理员身份重启。",
            "")
    elif not tray:
        banner = _banner(
            "warn", "托盘图标没装上（打字不受影响）",
            "常见于被降权启动。设置、热键、退出都可以用这个窗口和快捷键完成。",
            "")
    else:
        banner = _banner("ok", "一切正常：直接敲拼音就行",
                         "Tab 上屏译文 / 空格上屏中文 / 上下键选候选 / "
                         "两下 Shift 切模式 / Ctrl+Alt+Q 退出", "")

    return {"rows": rows, "banner": banner, "ok": banner["level"] == "ok",
            "low_integrity": bool(low)}


def status_text(info: dict) -> str:
    """诊断结果拍成一段纯文本（「复制状态」按钮和排障贴图都用它）。"""
    lines = [f"{info['banner']['level'].upper()}: {info['banner']['title']}"]
    detail = info["banner"].get("detail") or ""
    if detail:
        lines.append(detail)
    lines.append("")
    for row in info.get("rows") or []:
        lines.append(f"- {row['label']}: {row['value']} [{row['level']}]")
    return "\n".join(lines)


def hotkey_summary(cfg: dict) -> str:
    """快捷键一览（左侧卡片用）。读不懂的组合原样显示，不假装它是对的。"""
    from . import hotkey as _hk

    out = []
    for label, path, default in (
            ("上屏译文", "hotkeys.compose", "ctrl+alt+space"),
            ("切默认语言", "hotkeys.toggle_default", "ctrl+alt+l"),
            ("挂起/恢复", "hotkeys.toggle_enabled", "ctrl+alt+z"),
            ("退出", "hotkeys.quit", "ctrl+alt+q")):
        spec = str(_get(cfg, path, default) or default)
        try:
            shown = _hk.pretty(spec)
        except Exception:
            shown = spec
        out.append(f"{label}：{shown}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 进程相关：拉起实例 / 拉起重启 / 提权
# --------------------------------------------------------------------------- #
def _python() -> Path:
    """用哪个解释器。``pythonw`` 优先：不要黑窗口在背后闪一下。"""
    try:
        exe = daemon.pythonw_path()
    except Exception:
        exe = None
    return Path(exe) if exe else Path(sys.executable)


def _primary_argv(root_dir: Path, *flags: str) -> list[str]:
    """拉起另一个本程序进程用的前缀：``[解释器, main.py]`` 或打包版的 ``[exe]``。

    打包成 exe 之后既没有 ``python`` 也没有 ``main.py`` 可以拿来执行，
    参数直接喂给 exe 自己。
    """
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable)), *flags]
    return [str(_python()), str(Path(root_dir) / "main.py"), *flags]


def panel_argv(root_dir: Path, cfg_path: Path | str | None,
               log_path: Path | str | None = None) -> list[str]:
    """开一个**独立**的设置窗口进程要用的命令行。"""
    root_dir = Path(root_dir)
    argv = _primary_argv(root_dir, "--panel", "--detached")
    if cfg_path:
        argv += ["--config", str(cfg_path)]
    if log_path:
        argv += ["--log", str(log_path)]
    return argv


def start_argv(root_dir: Path, cfg_path: Path | str | None = None,
               log_path: Path | str | None = None) -> list[str]:
    """后台拉起常驻实例的命令行（不带 --panel，免得无限套娃）。"""
    root_dir = Path(root_dir)
    argv = _primary_argv(root_dir, "--background", "--detached")
    if cfg_path:
        argv += ["--config", str(cfg_path)]
    if log_path:
        argv += ["--log", str(log_path)]
    return argv


def elevate_argv(root_dir: Path, cfg_path: Path | str | None = None,
                 log_path: Path | str | None = None) -> list[str]:
    """提权重启用的命令行：起实例 + 起窗口，两个都要。

    为什么要 ``--background`` 和 ``--panel`` 同时带上：提权后那个进程不是从
    控制台起的（用户点的是 UAC 框），没人替它开窗口；只起实例的话，用户会看到
    UAC 确认完什么都没发生。
    """
    argv = start_argv(root_dir, cfg_path, log_path)
    argv.append("--panel")
    return argv


def _spawn(argv: list[str], cwd: Path) -> subprocess.Popen:
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP",
                 "CREATE_NO_WINDOW"):
        flags |= int(getattr(subprocess, name, 0) or 0)
    return subprocess.Popen(
        [str(a) for a in argv], cwd=str(cwd), close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=flags)


def spawn_instance(root_dir: Path, cfg_path: Path | str | None = None,
                   log_path: Path | str | None = None
                   ) -> tuple[bool, str]:
    """在后台把实例拉起来。已经在跑就直接说一声，不重复拉。"""
    try:
        if daemon.is_running():
            return True, "实例已经在跑了"
        _spawn(start_argv(root_dir, cfg_path, log_path), Path(root_dir))
        return True, "已启动实例"
    except Exception as exc:
        return False, f"启动失败：{exc}"


def spawn_panel(root_dir: Path, cfg_path: Path | str | None = None,
                log_path: Path | str | None = None) -> tuple[bool, str]:
    """另起一个进程显示设置窗口（调用方可以立刻返回，终端不挂住）。"""
    try:
        _spawn(panel_argv(root_dir, cfg_path, log_path), Path(root_dir))
        return True, "已打开设置窗口"
    except Exception as exc:
        return False, f"打开设置窗口失败：{exc}"


def _shell_execute(exe: str, params: str, cwd: Path) -> int:
    """跑 ``ShellExecuteW("runas")``，返回它的 HINSTANCE（<=32 表示失败）。"""
    import ctypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    hwnd = None
    op = "runas"
    ret = shell32.ShellExecuteW(hwnd, op, str(exe), params, str(cwd), 1)
    return int(ret or 0)


def restart_elevated(root_dir: Path, cfg_path: Path | str | None = None,
                     log_path: Path | str | None = None,
                     wait_s: float = 8.0) -> tuple[bool, str]:
    """先让旧实例退出，再用 UAC 提权重新拉起一份。

    **顺序不能反**：单实例互斥锁在旧实例手里，新的那份（哪怕是管理员）会认成
    「已经有一个在跑」直接退出 —— 用户点完 UAC 只会看到「什么都没变」。
    """
    root_dir = Path(root_dir)
    try:
        if daemon.is_running():
            daemon.send_command(root_dir, "quit")
            deadline = time.monotonic() + max(1.0, float(wait_s))
            while time.monotonic() < deadline:
                if not daemon.is_running():
                    break
                time.sleep(0.2)
            else:
                return False, ("旧实例还没退出（可能被安全软件卡住）。"
                               "先右任务管理器结束 pythonw.exe，再试一次。")
    except Exception as exc:
        return False, f"让旧实例退出时出错：{exc}"

    exe = _python()
    argv = elevate_argv(root_dir, cfg_path, log_path)
    params = subprocess.list2cmdline([str(a) for a in argv[1:]])
    try:
        code = _shell_execute(str(exe), params, root_dir)
    except Exception as exc:
        return False, f"调起 UAC 失败：{exc}"
    if code <= 32:
        return False, (f"UAC 没走完（ShellExecuteW 返回 {code}）。"
                       "常见情况：你在弹窗上点了「否」，或系统策略禁止提权。")
    return True, "已用管理员身份重新启动"


# --------------------------------------------------------------------------- #
# 单实例：别让连点两下 run.bat 开出两个一模一样的窗口
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int, W=None) -> bool:
    if pid <= 0:
        return False
    try:
        if W is not None and hasattr(W, "integrity_level_of"):
            return W.integrity_level_of(int(pid)) is not None
    except Exception:
        pass
    # 注意：Windows 上 ``os.kill(pid, 0)`` **不是探活**，它是
    # ``TerminateProcess(handle, 0)`` —— 会把人家窗口以退出码 0 杀掉。
    # 探活必须用 OpenProcess + GetExitCodeProcess。
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.GetExitCodeProcess.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_int

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not k32.GetExitCodeProcess(ctypes.c_void_p(handle),
                                          ctypes.byref(code)):
                # 查不到退出码就当它还活着：宁可少开一个窗口，也别重复开两个
                return True
            return int(code.value) == STILL_ACTIVE
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        return False


def lock_path(root_dir: Path) -> Path:
    return Path(root_dir) / LOCK_NAME


def read_lock(root_dir: Path) -> int:
    try:
        return int(lock_path(root_dir).read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return 0


def write_lock(root_dir: Path) -> None:
    try:
        lock_path(root_dir).write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def release_lock(root_dir: Path) -> None:
    try:
        if read_lock(root_dir) == os.getpid():
            lock_path(root_dir).unlink()
    except Exception:
        pass


def other_panel(root_dir: Path, W=None) -> int:
    """已经开着的那个设置窗口的 pid；没有就返回 0（陈旧锁会被无视）。"""
    pid = read_lock(root_dir)
    if pid and pid != os.getpid() and _pid_alive(pid, W):
        return pid
    return 0


def focus_existing(pid: int) -> bool:
    """把另一个进程的设置窗口提到前面（双击 run.bat 时用）。尽力而为。"""
    if not pid:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hits: list[int] = []

        def _cb(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid:
                return True
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == PANEL_TITLE:
                hits.append(hwnd)
                return False
            return True

        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                       wintypes.LPARAM)
        user32.EnumWindows(enum_proc(_cb), 0)
        if not hits:
            return False
        hwnd = hits[0]
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 窗口本体
# --------------------------------------------------------------------------- #
def _font(scale: float, pt: float, bold: bool = False) -> tuple:
    """Tk 的尺寸：负数字号 = 像素。开了 DPI 感知就必须自己乘缩放比。"""
    px = max(9, int(round(pt * 96.0 / 72.0 * scale)))
    fam = "Microsoft YaHei UI"
    return (fam, -px, "bold") if bold else (fam, -px)


class PanelApp:
    """设置窗口。把表单、诊断、命令按钮拼在一起。"""

    POLL_MS = 1000

    def __init__(self, root, cfg_path: Path, root_dir: Path, W=None,
                 scale: float = 1.0):
        self.root = root
        self.cfg_path = Path(cfg_path)
        self.root_dir = Path(root_dir)
        self.W = W
        self.scale = max(1.0, float(scale or 1.0))
        self.cfg_loaded: dict = {}
        self.entries: dict[str, Any] = {}
        self.vars: dict[str, Any] = {}
        # 下拉框：界面上显示中文标签，配置里存英文值，两张表互相翻译
        self.choice_maps: dict[str, dict[str, str]] = {}
        self.choice_rev: dict[str, dict[str, str]] = {}
        self.key_touched = False
        self.key_clear = False
        self._capture_q: queue.Queue = queue.Queue()
        self._capturing = False
        self._info: dict = {}
        self._last_status_sig = None

        self.f_body = _font(self.scale, 10)
        self.f_small = _font(self.scale, 9)
        self.f_bold = _font(self.scale, 10, bold=True)
        self.f_title = _font(self.scale, 15, bold=True)
        self.f_h2 = _font(self.scale, 11, bold=True)

        self._setup_window()
        self.reload_from_disk()
        self._build()
        self._fill_form()
        self.root.after(self.POLL_MS, self._poll)

    # ---------------- 窗口骨架 ---------------- #
    def _setup_window(self) -> None:
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = min(int(1060 * self.scale), sw - 40)
        h = min(int(820 * self.scale), sh - 60)
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.root.title(PANEL_TITLE)
        self.root.configure(bg=BG)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(min(int(880 * self.scale), w), min(int(560 * self.scale), h))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build(self) -> None:
        import tkinter as tk

        px = int(round(14 * self.scale))

        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=px, pady=(px, 6))
        tk.Label(head, text="translate-ime 设置", bg=BG, fg=FG,
                 font=self.f_title).pack(side="left")
        self.head_note = tk.Label(head, text="", bg=BG, fg=DIM,
                                  font=self.f_small)
        self.head_note.pack(side="right")

        # 结论横幅：放在最上面，权限问题不可能被忽略。
        self.banner = tk.Frame(self.root, bg=CARD)
        self.banner.pack(fill="x", padx=px, pady=(0, 8))
        self.banner_bar = tk.Frame(self.banner, bg=BAD, width=int(6 * self.scale))
        self.banner_bar.pack(side="left", fill="y")
        btext = tk.Frame(self.banner, bg=CARD)
        btext.pack(side="left", fill="both", expand=True, padx=int(12 * self.scale),
                   pady=int(10 * self.scale))
        self.banner_title = tk.Label(btext, text="", bg=CARD, fg=FG,
                                     font=self.f_bold, anchor="w", justify="left")
        self.banner_title.pack(fill="x")
        self.banner_detail = tk.Label(
            btext, text="", bg=CARD, fg=DIM, font=self.f_small, anchor="w",
            justify="left",
            # 这里的初值只是占位：窗口还没映射时 winfo_width() 是 1，按它算会把
            # 正文折断成每行三四个字（第 124 轮截图发现的）。真正的折行宽度由
            # _sync_banner_wrap 跟着 <Configure> 走。
            wraplength=int(700 * self.scale))
        self.banner_detail.pack(fill="x", pady=(int(4 * self.scale), 0))
        self.banner.bind("<Configure>", self._sync_banner_wrap)
        # 按钮挂在 banner 上而不是正文框里：挂在正文里会被挤到下一行，
        # 横幅就会多出一大块空白（第 124 轮截图）。
        self.banner_btn = self._button(self.banner, "以管理员身份重启",
                                       self._on_elevate, kind="primary")
        self.banner_btn.pack_forget()   # 只在权限不足时由 _render_status 摆出来

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=px, pady=0)

        self._build_left(body)
        self._build_form(body)

        # 底部状态行 + 按钮
        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", padx=px, pady=(int(8 * self.scale), px))
        self.msg = tk.Label(foot, text="", bg=BG, fg=DIM, font=self.f_small,
                            anchor="w", justify="left",
                            wraplength=max(320, int(760 * self.scale)))
        self.msg.pack(side="left", fill="x", expand=True)

        btns = tk.Frame(foot, bg=BG)
        btns.pack(side="right")
        self._button(btns, "保存并同步", self._on_save, kind="primary")
        self._button(btns, "重新读取", self._on_reload)
        self.btn_start = self._button(btns, "启动输入法", self._on_start)
        self.btn_quit = self._button(btns, "退出输入法", self._on_quit)

    def _sync_banner_wrap(self, event=None) -> None:
        """横幅正文按实际宽度折行（按钮占右边一截，要扣掉）。"""
        try:
            width = int(getattr(event, "width", 0) or self.banner.winfo_width())
            if width <= 1:
                return
            self.banner_detail.configure(
                wraplength=max(int(260 * self.scale),
                               width - int(200 * self.scale)))
        except Exception:
            pass

    def _button(self, parent, text: str, cmd, kind: str = "normal"):
        import tkinter as tk

        if kind == "primary":
            bg, fg, active = ACCENT, "#ffffff", "#4b8ce6"
        else:
            bg, fg, active = FIELD_BG, FG, BORDER
        btn = tk.Button(
            parent, text=text, command=cmd, bg=bg, fg=fg, font=self.f_body,
            activebackground=active, activeforeground=fg, relief="flat", bd=0,
            padx=int(12 * self.scale), pady=int(5 * self.scale),
            cursor="hand2", highlightthickness=0)
        btn.pack(side="left", padx=(int(6 * self.scale), 0))
        return btn

    def _build_left(self, body) -> None:
        import tkinter as tk

        left = tk.Frame(body, bg=BG, width=int(330 * self.scale))
        left.pack(side="left", fill="y", padx=(0, int(12 * self.scale)))
        left.pack_propagate(False)

        # 状态卡
        card = tk.Frame(left, bg=CARD)
        card.pack(fill="x")
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=int(12 * self.scale), pady=(int(10 * self.scale), 0))
        tk.Label(head, text="现在的状态", bg=CARD, fg=FG,
                 font=self.f_h2).pack(side="left")
        self.btn_rediagnose = self._button(head, "重新诊断", self._poll_now)
        self.btn_rediagnose.configure(font=self.f_small)

        self.rows_box = tk.Frame(card, bg=CARD)
        self.rows_box.pack(fill="x", padx=int(12 * self.scale),
                           pady=(int(6 * self.scale), int(12 * self.scale)))
        self.row_widgets: list[tuple] = []

        # 快捷键卡
        kcard = tk.Frame(left, bg=CARD)
        kcard.pack(fill="x", pady=(int(10 * self.scale), 0))
        tk.Label(kcard, text="快捷键", bg=CARD, fg=FG, font=self.f_h2,
                 anchor="w").pack(fill="x", padx=int(12 * self.scale),
                                  pady=(int(10 * self.scale), 0))
        self.hotkey_label = tk.Label(
            kcard, text="", bg=CARD, fg=DIM, font=self.f_small, anchor="w",
            justify="left")
        self.hotkey_label.pack(fill="x", padx=int(12 * self.scale),
                               pady=(int(4 * self.scale), int(12 * self.scale)))

        # 上手卡
        gcard = tk.Frame(left, bg=CARD)
        gcard.pack(fill="both", expand=True, pady=(int(10 * self.scale), 0))
        tk.Label(gcard, text="三步上手", bg=CARD, fg=FG, font=self.f_h2,
                 anchor="w").pack(fill="x", padx=int(12 * self.scale),
                                  pady=(int(10 * self.scale), 0))
        guide = tk.Label(
            gcard, bg=CARD, fg=DIM, font=self.f_small, anchor="nw",
            justify="left",
            # 别贪多：左栏高度是固定的，多一行就被窗口底边切掉半行字
            # （第 124 轮截图）。想补充说明就写进横幅或右侧提示。
            text=("1. 填 API key（没有就用离线词表）\n"
                  "2. 点「保存并同步」\n"
                  "3. 去记事本里敲 nihao\n"
                  "\n"
                  "空格 上屏中文 / Tab 译文\n"
                  "↑↓ 选候选 / 数字键直选\n"
                  "两下 Shift 切「中文←→译文」"),
            wraplength=int(290 * self.scale))
        guide.pack(fill="both", expand=True, padx=int(12 * self.scale),
                   pady=(int(4 * self.scale), int(12 * self.scale)))
        self.guide_label = guide

        tail = tk.Frame(left, bg=BG)
        tail.pack(fill="x", pady=(int(10 * self.scale), 0))
        self._button(tail, "打开日志", self._open_log)
        self._button(tail, "配置文件", self._open_config)
        self._button(tail, "复制状态", self._copy_status)

    def _build_form(self, body) -> None:
        import tkinter as tk

        wrap = tk.Frame(body, bg=BG)
        wrap.pack(side="left", fill="both", expand=True)

        canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        self.canvas = canvas
        vsb = tk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win, width=e.width))

        def _wheel(ev):
            try:
                canvas.yview_scroll(int(-ev.delta / 120), "units")
            except Exception:
                pass

        def _enter(_ev):
            canvas.bind_all("<MouseWheel>", _wheel)

        def _leave(_ev):
            canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", _enter)
        canvas.bind("<Leave>", _leave)
        inner.bind("<Enter>", _enter)
        inner.bind("<Leave>", _leave)

        for title, sub, items in SECTIONS:
            self._build_section(inner, title, sub, items)

    def _build_section(self, parent, title: str, sub: str,
                       items: list[dict]) -> None:
        import tkinter as tk

        px = int(12 * self.scale)
        card = tk.Frame(parent, bg=CARD)
        card.pack(fill="x", pady=(0, int(10 * self.scale)))

        tk.Label(card, text=title, bg=CARD, fg=FG, font=self.f_h2,
                 anchor="w").pack(fill="x", padx=px, pady=(int(10 * self.scale), 0))
        if sub:
            tk.Label(card, text=sub, bg=CARD, fg=DIM, font=self.f_small,
                     anchor="w", justify="left",
                     wraplength=max(280, int(560 * self.scale))).pack(
                fill="x", padx=px, pady=(int(2 * self.scale), 0))

        grid = tk.Frame(card, bg=CARD)
        grid.pack(fill="x", padx=px, pady=(int(6 * self.scale),
                                           int(10 * self.scale)))
        grid.columnconfigure(2, weight=1)

        for i, f in enumerate(items):
            row = i
            tk.Label(grid, text=f["label"], bg=CARD, fg=FG, font=self.f_body,
                     anchor="w", width=14).grid(
                row=row * 2, column=0, sticky="w",
                pady=(int(4 * self.scale), 0))
            widget = self._build_input(grid, f)
            widget.grid(row=row * 2, column=1, sticky="w",
                        padx=(int(8 * self.scale), int(10 * self.scale)),
                        pady=(int(4 * self.scale), 0))
            if f["hint"]:
                tk.Label(grid, text=f["hint"], bg=CARD, fg=DIM,
                         font=self.f_small, anchor="w", justify="left",
                         wraplength=max(220, int(330 * self.scale))).grid(
                    row=row * 2, column=2, sticky="w",
                    pady=(int(4 * self.scale), 0))
            tk.Frame(grid, bg=CARD, height=int(4 * self.scale)).grid(
                row=row * 2 + 1, column=0, columnspan=3, sticky="we")

    def _entry(self, parent, width: int, *, secret: bool = False):
        import tkinter as tk

        return tk.Entry(parent, bg=FIELD_BG, fg=FG, font=self.f_body,
                        insertbackground=FG, relief="flat", bd=0,
                        highlightthickness=1, highlightbackground=BORDER,
                        highlightcolor=ACCENT, width=width,
                        show="•" if secret else "")

    def _build_input(self, parent, f: dict):
        """造一个控件。返回的 widget 由调用方 grid；复合控件自己是个 Frame。"""
        import tkinter as tk

        path, kind = f["path"], f["kind"]
        width = max(10, int(26 * self.scale / 1.4))

        if kind == "bool":
            var = tk.BooleanVar(value=False)
            self.vars[path] = var
            return tk.Checkbutton(
                parent, text=" 开", variable=var, bg=CARD, fg=FG,
                font=self.f_body, activebackground=CARD, activeforeground=FG,
                selectcolor=FIELD_BG, bd=0, highlightthickness=0,
                anchor="w", cursor="hand2")

        if kind == "choice":
            opts = list(f.get("options") or ())
            # 下拉里给人看的是中文标签，配置里存的是英文值 —— 两边对不上的话
            # 保存一次 provider 就会变成「DeepSeek」这种非法值。
            self.choice_maps[path] = {lab: val for val, lab in opts}
            self.choice_rev[path] = {val: lab for val, lab in opts}
            var = tk.StringVar(value=opts[0][1] if opts else "")
            self.vars[path] = var
            om = tk.OptionMenu(parent, var, *[lab for _val, lab in opts])
            om.configure(bg=FIELD_BG, fg=FG, activebackground=BORDER,
                         activeforeground=FG, font=self.f_body, bd=0,
                         highlightthickness=0, anchor="w", width=width,
                         relief="flat")
            om["menu"].configure(bg=FIELD_BG, fg=FG, font=self.f_body,
                                 activebackground=ACCENT,
                                 activeforeground="#ffffff", bd=0)
            return om

        box = tk.Frame(parent, bg=CARD)
        ent = self._entry(box, width, secret=(kind == "password"))
        ent.pack(side="left")
        self.entries[path] = ent

        if kind == "hotkey":
            b1 = self._button(box, "按一下", lambda p=path: self._capture(p))
            b1.configure(font=self.f_small)
            b2 = self._button(box, "清空", lambda p=path: self._clear_hotkey(p))
            b2.configure(font=self.f_small)
        elif kind == "password":
            ent.bind("<KeyRelease>", lambda _e, p=path: self._key_typed(p))
            b = self._button(box, "清空 key", self._on_clear_key)
            b.configure(font=self.f_small)
        return box

    # ---------------- 读盘 / 填表 ---------------- #
    def reload_from_disk(self) -> None:
        from . import webpanel

        try:
            self.cfg_loaded = webpanel.load_panel_config(self.cfg_path)
            self.load_error = ""
        except Exception as exc:
            self.cfg_loaded = json.loads(json.dumps(C.DEFAULTS, ensure_ascii=False))
            self.load_error = str(exc)

    def _fill_form(self) -> None:
        values = values_from_config(self.cfg_loaded)
        for f in FIELDS:
            path, kind = f["path"], f["kind"]
            val = values.get(path)
            try:
                if kind == "bool":
                    self.vars[path].set(bool(val))
                elif kind == "choice":
                    cur = str(val if val is not None else "")
                    self.vars[path].set(self.choice_rev.get(path, {}).get(cur, cur))
                elif path in self.entries:
                    ent = self.entries[path]
                    ent.delete(0, "end")
                    ent.insert(0, "" if val is None else str(val))
            except Exception:
                pass
        self.key_touched = False
        self.key_clear = False

    def _key_typed(self, path: str) -> None:
        self.key_touched = True
        self.key_clear = False

    def _clear_hotkey(self, path: str) -> None:
        ent = self.entries.get(path)
        if ent is not None:
            ent.delete(0, "end")
        self._set_msg("已清空这个快捷键（保存后生效）", "warn")

    # ---------------- 收集 / 保存 ---------------- #
    def collect(self) -> dict:
        """把界面上的值收成「表单路径 → 值」。"""
        out: dict[str, Any] = {}
        for f in FIELDS:
            path, kind = f["path"], f["kind"]
            if kind == "bool":
                out[path] = bool(self.vars[path].get())
            elif kind == "choice":
                shown = str(self.vars[path].get())
                out[path] = self.choice_maps.get(path, {}).get(shown, shown)
            elif kind in ("int", "float"):
                ent = self.entries.get(path)
                out[path] = coerce_number(kind, ent.get() if ent else "")
            else:
                ent = self.entries.get(path)
                out[path] = (ent.get().strip() if ent else "")
        if out.get(KEY_FIELD) == MASK_FOR_UI:
            self.key_touched = False
        elif out.get(KEY_FIELD):
            self.key_touched = True
        return out

    def _on_save(self) -> None:
        from . import webpanel

        if getattr(self, "load_error", ""):
            self._set_msg(f"当前配置文件读不懂：{self.load_error}", "bad")
            return
        try:
            values = self.collect()
        except Exception as exc:
            self._set_msg(f"收集表单失败：{exc}", "bad")
            return
        clear = bool(self.key_clear or (self.key_touched and not values.get(KEY_FIELD)))
        touched = bool(self.key_touched) and not clear
        submitted = apply_values(self.cfg_loaded, values,
                                 key_touched=touched, clear_key=clear)
        try:
            res = webpanel.save_panel_config(self.cfg_path, submitted, self.root_dir)
        except Exception as exc:
            self._set_msg(f"保存失败：{exc}", "bad")
            return

        if not res.get("ok"):
            self._set_msg("没保存：" + "；".join(res.get("errors") or ["原因不明"]),
                          "bad")
            return

        self.reload_from_disk()
        notes = []
        if res.get("note"):
            notes.append(str(res["note"]))
        warn = [w for w in (res.get("warnings") or []) if w]
        if warn:
            notes.append(f"还有 {len(warn)} 条提醒：")
            notes.extend("· " + w for w in warn)
        self._set_msg("\n".join(notes), "warn" if warn else "ok")
        self.hotkey_label.configure(text=hotkey_summary(self.cfg_loaded))
        self._poll_now()

    def _on_reload(self) -> None:
        self.reload_from_disk()
        if getattr(self, "load_error", ""):
            self._set_msg(f"读不懂配置文件：{self.load_error}", "bad")
            return
        self._fill_form()
        self.hotkey_label.configure(text=hotkey_summary(self.cfg_loaded))
        self._set_msg(f"已重新读取 {self.cfg_path.name}", "ok")

    # ---------------- 捕获组合键 ---------------- #
    def _capture(self, path: str) -> None:
        if self._capturing:
            return
        self._capturing = True
        self._set_msg("请按下要用的组合键（12 秒内，按 Esc 取消）…", "warn")

        def work():
            combo, err = "", ""
            try:
                from . import webpanel
                combo = webpanel.capture_combo(12.0, self.W)
            except Exception as exc:
                err = str(exc)
            self._capture_q.put((path, combo, err))

        threading.Thread(target=work, daemon=True).start()

    def _apply_capture(self, path: str, combo: str, err: str) -> None:
        self._capturing = False
        if err:
            self._set_msg(f"捕获失败：{err}", "bad")
            return
        if not combo:
            self._set_msg("没等到按键（超时）", "warn")
            return
        if combo.strip().lower() in ("esc", "escape"):
            self._set_msg("已取消", "warn")
            return
        ent = self.entries.get(path)
        if ent is not None:
            ent.delete(0, "end")
            ent.insert(0, combo)
        self._set_msg(f"已捕获 {combo}（保存后生效）", "ok")

    # ---------------- 状态轮询 ---------------- #
    def _integrity_of(self, pid: Any):
        if not pid or self.W is None:
            return None
        try:
            return self.W.integrity_level_of(int(pid))
        except Exception:
            return None

    def _poll_now(self) -> None:
        st = None
        try:
            st = daemon.read_status(self.root_dir)
        except Exception:
            st = None
        try:
            running = bool(daemon.is_running())
        except Exception:
            running = False
        st = st or {}
        info = diagnose(self.cfg_loaded, st, running,
                        self._integrity_of(st.get("pid")),
                        self._autostart())
        self._info = info
        self._render_status(info, st, running)
        # 捕获线程的结果也在这里取：Tk 不能在别的线程里动
        try:
            while True:
                path, combo, err = self._capture_q.get_nowait()
                self._apply_capture(path, combo, err)
        except queue.Empty:
            pass

    def _autostart(self):
        try:
            return daemon.autostart_status()
        except Exception:
            return "未知"

    def _render_status(self, info: dict, st: dict, running: bool) -> None:
        import tkinter as tk

        colors = {"ok": OK, "warn": WARN, "bad": BAD}
        banner = info["banner"]
        col = colors.get(banner["level"], DIM)
        self.banner_bar.configure(bg=col)
        self.banner_title.configure(text=banner["title"], fg=FG)
        self.banner_detail.configure(text=banner.get("detail") or "")
        if banner.get("action") == "elevate" and not self.banner_btn.winfo_ismapped():
            self.banner_btn.pack(side="right", padx=int(12 * self.scale),
                                 pady=int(10 * self.scale))
        elif banner.get("action") != "elevate" and self.banner_btn.winfo_ismapped():
            self.banner_btn.pack_forget()

        rows = info["rows"]
        if len(self.row_widgets) != len(rows):
            for w in self.rows_box.winfo_children():
                w.destroy()
            self.row_widgets = []
            for r in rows:
                box = tk.Frame(self.rows_box, bg=CARD)
                box.pack(fill="x", pady=int(2 * self.scale))
                lab = tk.Label(box, text=r["label"] + "：", bg=CARD, fg=DIM,
                               font=self.f_small, anchor="w",
                               width=9)
                lab.pack(side="left")
                val = tk.Label(box, text=r["value"], bg=CARD,
                               fg=colors.get(r["level"], FG), font=self.f_small,
                               anchor="w", justify="left",
                               wraplength=int(210 * self.scale))
                val.pack(side="left", fill="x", expand=True)
                self.row_widgets.append((lab, val))
        else:
            for (lab, val), r in zip(self.row_widgets, rows):
                lab.configure(text=r["label"] + "：")
                val.configure(text=r["value"], fg=colors.get(r["level"], FG))

        self.hotkey_label.configure(text=hotkey_summary(self.cfg_loaded))
        self.head_note.configure(
            text=("运行中 · " + str(st.get("config") or "")) if running else "未运行")
        try:
            self.btn_start.configure(state="disabled" if running else "normal")
            self.btn_quit.configure(state="normal" if running else "disabled")
        except Exception:
            pass
        try:
            show_guide = bool(_get(self.cfg_loaded, "ui.guide", True))
            if show_guide:
                self.guide_label.pack(fill="both", expand=True,
                                      padx=int(12 * self.scale),
                                      pady=(int(4 * self.scale), int(12 * self.scale)))
            else:
                self.guide_label.pack_forget()
        except Exception:
            pass

    # ---------------- 命令 ---------------- #
    def _set_msg(self, text: str, level: str = "ok") -> None:
        colors = {"ok": OK, "warn": WARN, "bad": BAD, "dim": DIM}
        try:
            self.msg.configure(text=text, fg=colors.get(level, DIM))
        except Exception:
            pass

    def _on_start(self) -> None:
        ok, detail = spawn_instance(self.root_dir, self.cfg_path)
        self._set_msg(detail, "ok" if ok else "bad")
        self.root.after(1200, self._poll_now)

    def _on_quit(self) -> None:
        try:
            sent = daemon.send_command(self.root_dir, "quit")
        except Exception as exc:
            self._set_msg(f"发送退出命令失败：{exc}", "bad")
            return
        self._set_msg("已发送退出命令" if sent else "发不出去（实例可能已经退了）",
                      "ok" if sent else "warn")
        self.root.after(1000, self._poll_now)

    def _on_elevate(self) -> None:
        ok, detail = restart_elevated(self.root_dir, self.cfg_path)
        self._set_msg(detail, "ok" if ok else "bad")
        if ok:
            # 提权后的实例会自己开一个新窗口，这个窗口留着只会显示「没在运行」。
            self.root.after(1500, self.on_close)

    def _on_clear_key(self) -> None:
        ent = self.entries.get(KEY_FIELD)
        if ent is not None:
            ent.delete(0, "end")
        self.key_clear = True
        self.key_touched = True
        self._set_msg("API key 会在保存后清空（改用离线词表）", "warn")

    # ---------------- 打开文件 ---------------- #
    def _log_path(self) -> Path:
        try:
            return Path(daemon.resolve_log_path(self.root_dir, None, self.cfg_loaded))
        except Exception:
            return self.root_dir / "translate-ime.log"

    def _open_log(self) -> None:
        self._open(self._log_path())

    def _open_config(self) -> None:
        self._open(self.cfg_path)

    def _open(self, path: Path) -> None:
        path = Path(path)
        if not path.exists():
            self._set_msg(f"文件还不存在：{path}", "warn")
            return
        try:
            os.startfile(str(path))  # noqa: S606  (Windows 专用)
        except Exception as exc:
            self._set_msg(f"打不开 {path.name}：{exc}", "bad")

    def _copy_status(self) -> None:
        text = status_text(self._info) if self._info else ""
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self._set_msg("状态已复制到剪贴板", "ok")
        except Exception as exc:
            self._set_msg(f"复制失败：{exc}", "bad")

    # ---------------- 生命周期 ---------------- #
    def _poll(self) -> None:
        try:
            self._poll_now()
        except Exception:
            pass
        try:
            self.root.after(self.POLL_MS, self._poll)
        except Exception:
            pass

    def mainloop(self) -> None:
        self._poll_now()
        self.hotkey_label.configure(text=hotkey_summary(self.cfg_loaded))
        self.root.mainloop()

    def on_close(self) -> None:
        release_lock(self.root_dir)
        try:
            self.root.destroy()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def _log_crash(root_dir: Path, cfg: dict | None) -> None:
    """窗口自己崩了要留痕 —— 它是 pythonw 起的，没人看得见异常。"""
    try:
        path = Path(daemon.resolve_log_path(Path(root_dir), None, cfg or C.DEFAULTS))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n[panel] 设置窗口异常退出：\n")
            fh.write(traceback.format_exc())
            fh.write("\n")
    except Exception:
        pass


def run_panel(cfg_path: Path | str | None = None, root_dir: Path | str | None = None,
              W=None) -> int:
    """打开设置窗口并阻塞到用户关掉它。返回进程退出码。"""
    if W is None:
        try:
            from . import winapi as _W
            W = _W
        except Exception:
            W = None
    root_dir = Path(root_dir or Path(__file__).resolve().parent.parent)
    cfg_path = Path(cfg_path or (root_dir / "config.json"))

    try:
        import tkinter as tk
    except Exception as exc:
        print(f"[panel] 没有 tkinter，打不开设置窗口：{exc}", file=sys.stderr)
        return 1

    scale = 1.0
    if W is not None:
        try:
            scale = float(W.enable_dpi_awareness())
        except Exception:
            scale = 1.0
    if not (0.5 <= scale <= 4.0):
        scale = 1.0

    pid = other_panel(root_dir, W)
    if pid:
        raised = focus_existing(pid)
        print(f"[panel] 设置窗口已经开着了（pid {pid}）"
              f"{'，已提到前面' if raised else '，看任务栏'}。")
        return 0

    try:
        root = tk.Tk()
    except Exception as exc:
        print(f"[panel] 创建窗口失败：{exc}", file=sys.stderr)
        return 1

    write_lock(root_dir)
    cfg = None
    try:
        app = PanelApp(root, cfg_path, root_dir, W=W, scale=scale)
        cfg = app.cfg_loaded
        app.mainloop()
    except Exception:
        _log_crash(root_dir, cfg)
        release_lock(root_dir)
        raise
    return 0
