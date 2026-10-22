# -*- coding: utf-8 -*-
"""悬浮候选窗：中文一行、译文一行，并排多选。

界面长这样::

    ┌──────────────────────────────────────────────────────────┐
    │ nihao                                    第 1 行 · 中文  │
    │ 中  1 你好   2 你号   3 泥好   4 你豪   5 尼好           │
    │ EN  1 Hello  2 Hi there  3 Greetings                     │
    │ Tab 换行 · ←→ 选择 · 1-9 直选 · 空格 上屏 · Esc 清空      │
    └──────────────────────────────────────────────────────────┘

关键约束：这个窗口**永远不能抢焦点**。一旦它拿到焦点，后续按键就全进候选窗了，
整个输入法直接废掉。所以它同时带 ``WS_EX_NOACTIVATE`` 和 ``overrideredirect``，
并且全程不调用任何 focus 相关的 API。
"""
from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import font as tkfont

from . import winapi as W
from .core import ROW_EN, ROW_ZH
from .hotkey import pretty as _pretty_hotkey, spec_problem  # pretty 仍是转发，老名字外面还在用


# 候选块 / 缓冲行最多显示多少个字符。
# 候选窗宽度有上限（overlay.max_width，默认 900px），而候选可能是一整句
# （`jintiantianqizenmeyang` 能出「今天天气怎么样」）。不截断的话整行会超出
# 窗口右边缘被硬切掉，看起来像候选丢了 —— 实测复现过，所以在这里卡住长度。
ZH_CHIP_MAX_CHARS = 10
EN_CHIP_MAX_CHARS = 30
BUF_MAX_CHARS = 40
# 顶栏（第几行 + mock + 状态 + 「还有 N 个没显示」）最多显示多少个字符 ——
# 顶栏一长，整个窗口会跟着变宽，把好不容易按像素砍好的候选行又推出去。
MODE_MAX_CHARS = 56


def _clip(text: str, limit: int) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


class Overlay:
    def __init__(self, cfg: dict, verbose: bool = True, switch_hint: str | None = None):
        self.cfg = cfg.get("overlay", {})
        self.verbose = verbose
        self.max_zh = int(cfg.get("pinyin", {}).get("max_candidates", 7))
        self.max_en = max(1, int(cfg.get("translate", {}).get("variants", 4)))
        self.visible = False
        self._last_key = None
        self._state: dict | None = None
        self._top: int | None = None
        # 「候选窗到底出现过没有」必须是可观测事实，不能靠猜。
        # 用户报的「打字没反应、候选窗根本不出现」在出口处就是这三个字段：
        # shows 一直是 0 = 窗口一次都没显示过；shows > 0 但 on_monitor 为假
        # = 显示到屏幕外了；show_error 非空 = 显示本身失败了。
        self.shows = 0
        # ``shows`` 数的是「窗口显示过几次」，短提示（toast）也算一次 —— 启动引导
        # 一出现它就 > 0，于是「候选窗一次都没弹过」永远判不出来，面板还会把提示条
        # 的显示次数说成「候选窗 显示过 N 次」（第 86 轮真机：静置不起键，shows=2、
        # 一个候选都没出现过）。候选窗的次数单独数：只有非 toast 布局才 +1。
        self.cand_shows = 0
        self.last_rect: tuple[int, int, int, int] | None = None
        self.on_monitor = False
        self.show_error: str | None = None
        # True 表示当前是「只显示一条短提示」的紧凑布局
        self._toast_mode = False
        # 语言切换只能靠热键（空缓冲时 Tab 要留给目标程序），所以把热键写进提示行，
        # 否则用户根本发现不了怎么切回中文。
        #
        # 第 115 轮：这里以前直接读配置原文再 pretty()，于是「播报读的是注册表、
        # 这里读的是原文」两条口径又分叉了 —— `toggle_default: "l"` 被 core 拒绝
        # 注册（没有修饰键的打字键），底部提示照样写「L 换语言」，用户按 L 只会
        # 打出一个 l；配成空串时更糟，提示变成「 ·  换语言」，等于指着一个不存在的键。
        # 现在优先用 App 传进来的、`core.registered_hotkeys` 里真正注册上的写法；
        # 没人传（--demo、测试直接构造）时退化成「先按同一套规则验一遍，验不过就不提」。
        self.switch_hint = self._pick_switch_hint(cfg, switch_hint)
        self.hint = self._hint_switch_part()

        c = self.cfg
        self.bg = c.get("bg", "#1b1c22")
        self.fg = c.get("fg", "#eceef5")
        self.dim = c.get("dim", "#8b8d9a")
        self.border = c.get("border", "#3a3c47")
        self.accent_bg = c.get("accent_bg", "#2f6fd0")
        self.accent_fg = c.get("accent_fg", "#ffffff")
        self.row_bg = c.get("row_bg", "#24262e")
        self.row_active_bg = c.get("row_active_bg", "#2b2e39")
        self.sel_soft = c.get("sel_soft", "#3d4150")
        self.offset_y = int(c.get("offset_y", 28))

        # 先把 Tk 根窗口建出来：tkfont.families() 需要默认 root，否则会抛
        # RuntimeError: too early to use font.families(): no default root window
        self._top: int | None = None
        # 必须在 tk.Tk() 之前开启 DPI 感知，否则 Windows 会把整个窗口位图拉伸：
        # 本机 2560x1440 @150% 实测，未开启时 winfo_screenwidth() 谎报 1707、
        # 字是拉伸出来的（发虚），屏幕坐标与实际像素差 1.5 倍。
        # 开启后窗口按物理像素绘制，所有像素尺寸要自己乘上 self.scale。
        self.scale = W.enable_dpi_awareness()
        # Tk 建根窗口那一刻会把自己的窗口顶成前台窗口（即使随后立刻 withdraw），
        # 所以先把当前真正的前台窗口记下来，建完立刻还回去。
        self._real_fg = W.foreground_hwnd()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", float(c.get("alpha", 0.97)))
        except Exception:
            pass
        self.root.configure(bg=self.border)

        fam = c.get("font_family", "Microsoft YaHei UI")
        try:
            available = set(tkfont.families())
        except Exception:
            available = set()
        if fam not in available:
            for alt in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "SimHei", "Arial"):
                if alt in available:
                    fam = alt
                    break
        self._fam = fam
        # 配置里的数值都按「96 DPI 下的像素」理解，真正落到屏幕上时再乘缩放比。
        # 缩放比不是常量：候选窗搬到另一块缩放比不同的显示器上时要重算一次
        # （第 47 轮：以前只在启动时取主屏的缩放比，副屏字号就不对）。
        self._base = {
            "font_size": int(c.get("font_size", 16)),
            "secondary_font_size": int(c.get("secondary_font_size", 14)),
            "hint_font_size": int(c.get("hint_font_size", 9)),
            "pad_x": 9,
            "pad_y": 4,
            "gap_x": 2,
            "margin": 12,
            "min_width": int(c.get("min_width", 520)),
            "max_width": int(c.get("max_width", 900)),
            "offset_y": int(c.get("offset_y", 28)),
        }
        self._apply_metrics(self.scale)

        self._build()
        self.root.update_idletasks()
        self._apply_noactivate()
        self._restore_real_foreground()

    # ------------------------------------------------------------------ #
    # 底部提示行里的「换语言」热键（第 115 轮）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pick_switch_hint(cfg: dict, switch_hint: str | None) -> str:
        """挑出「换语言」这一格该写什么；没有真正可用的键就返回空串。

        ``switch_hint`` 由 App 从 ``core.registered_hotkeys`` 取（唯一权威：
        被拒绝注册的、撞车输掉的都已经被 core 筛掉了）。没人传时才回退到
        「拿配置原文按同一套规则验一遍」—— ``--demo`` 和测试直接构造
        ``Overlay`` 走的就是这条，不该因为少一个参数就重新学会撒谎。
        """
        if switch_hint is not None:
            return str(switch_hint or "").strip()
        spec = str((cfg.get("hotkeys") or {}).get("toggle_default") or "").strip()
        if not spec:
            return ""
        try:
            problem = spec_problem(spec, W.parse_hotkey)
        except Exception:
            return ""
        return "" if problem else _pretty_hotkey(spec)

    def _hint_switch_part(self) -> str:
        base = "Tab 换行 · ←→ 选择 · 1-9 直选 · 空格/回车 上屏 · Esc 清空"
        if not self.switch_hint:
            return base
        return f"{base} · {self.switch_hint} 换语言"

    def set_switch_hint(self, text: str) -> None:
        """热重载之后换掉这一格（``hotkeys.*`` 改完不用重启实例）。"""
        new = str(text or "").strip()
        if new == self.switch_hint:
            return
        self.switch_hint = new
        self.hint = self._hint_switch_part()
        # 提示行是构建窗口时就画好的 Label；已经显示过的窗口要立刻换成新的字。
        lbl = getattr(self, "hint_label", None)
        if lbl is not None:
            try:
                lbl.configure(text=self.hint)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 缩放比（逐屏 DPI）
    # ------------------------------------------------------------------ #
    def _apply_metrics(self, s: float) -> None:
        """按缩放比 ``s`` 重算字号与所有像素尺寸（不动 Tk 控件本身）。"""
        self.scale = float(s)
        b = self._base
        # 字号一律用**负值**（Tk 里负数表示像素，正数表示磅）。正数会被 Tk 再乘一遍
        # `tk scaling`（本机 2560x1440 @150% 下是 2.0 = 144/72），和我这里的 s 叠起来
        # 字会大 3 倍 —— 实测候选块宽到 410px、7 个候选整整要 1953px 就是这么来的。
        self.f_main = (self._fam, -max(8, int(round(b["font_size"] * s))))
        self.f_sub = (self._fam, -max(8, int(round(b["secondary_font_size"] * s))))
        self.f_hint = (self._fam, -max(7, int(round(b["hint_font_size"] * s))))
        self.pad_x = max(6, int(round(b["pad_x"] * s)))
        self.pad_y = max(3, int(round(b["pad_y"] * s)))
        self.gap_x = max(1, int(round(b["gap_x"] * s)))
        self.min_width = int(round(b["min_width"] * s))
        self.max_width = int(round(b["max_width"] * s))
        # 第 99 轮：min_width 是下限、max_width 是上限。两个值矛盾（min > max）时
        # **下限优先** —— 否则会渲染出比用户要求的最小宽度还窄的窗口：真机
        # min_width=1200 / max_width=200（@1.5x → 1800 / 300）实测窗口只有 300px，
        # 候选词被 _fit_row 按 300 裁掉一半，而 --check 一个字都不说。
        if self.max_width < self.min_width:
            self.max_width = self.min_width
        self.offset_y = int(round(b["offset_y"] * s))
        self._row_margin = max(8, int(round(b["margin"] * s)))

    def _sync_scale(self, x: int, y: int) -> None:
        """候选窗换到另一块显示器时，把缩放比换成那块屏的。

        以前缩放比只在 ``__init__`` 里取一次 ``GetDpiForSystem()``（主屏）。多显示器
        且两块屏缩放比不同时，窗口在副屏上的字号偏大/偏小，而位置分隔、宽度上限
        也都跟着主屏算 —— README 把它写成「已知限制」写了很久。现在每次定位都问
        一次光标所在显示器的 DPI（``GetDpiForMonitor``，取当前生效值），变了就重排。
        """
        try:
            dpi = int(W.monitor_dpi(x=x, y=y))
        except Exception:
            return
        if dpi <= 0:
            return
        s = max(1.0, dpi / 96.0)
        if abs(s - self.scale) <= 0.02:
            return
        old = self.scale
        state = getattr(self, "_state", None)
        try:
            for child in list(self.root.winfo_children()):
                child.destroy()
            self._apply_metrics(s)
            self._build()
            self.root.update_idletasks()
            self._apply_noactivate()
        except Exception as exc:  # 重排失败就退回原缩放比，别把候选窗搞没
            print(f"[overlay] 按显示器 DPI 重排失败：{exc!r}", file=sys.stderr)
            try:
                for child in list(self.root.winfo_children()):
                    child.destroy()
                self._apply_metrics(old)
                self._build()
                self.root.update_idletasks()
            except Exception:
                pass
            return
        self._last_key = None
        if state:
            # 控件换了，但用户正在看的内容不变：按同一份状态重画一遍
            self.render(state)
        if self.verbose:
            print(f"[overlay] 显示器 DPI 变了：{old:.2f}x -> {s:.2f}x，候选窗已按新字号重排")

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        px, py, gx = self.pad_x, self.pad_y, self.gap_x
        mar = max(8, int(round(12 * self.scale)))
        self._row_margin = mar

        outer = tk.Frame(self.root, bg=self.border)
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        inner = tk.Frame(outer, bg=self.bg)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.inner = inner

        # ---- 短提示（模式切换时才露脸，平时不占位置）----
        self.toast_label = tk.Label(
            inner, text="", bg=self.bg, fg=self.accent_fg, font=self.f_main, anchor="w"
        )

        # ---- 顶栏：拼音缓冲 + 当前行提示 ----
        head = tk.Frame(inner, bg=self.bg)
        head.pack(fill="x", padx=mar, pady=(py * 2, py))
        self.head = head
        self.buf_label = tk.Label(
            head, text="", bg=self.bg, fg=self.fg, font=self.f_main, anchor="w"
        )
        self.buf_label.pack(side="left")
        self.mode_label = tk.Label(
            head, text="", bg=self.bg, fg=self.dim, font=self.f_hint, anchor="e"
        )
        self.mode_label.pack(side="right")

        # ---- 中文行 ----
        self.zh_row = tk.Frame(inner, bg=self.row_bg)
        self.zh_row.pack(fill="x", padx=mar, pady=gx)
        self.zh_tag = tk.Label(
            self.zh_row, text="中", bg=self.row_bg, fg=self.dim, font=self.f_hint, width=3
        )
        self.zh_tag.pack(side="left", padx=(px - gx, px), pady=py)
        self.zh_chips: list[tk.Label] = []
        for i in range(self.max_zh):
            chip = tk.Label(
                self.zh_row, text="", bg=self.row_bg, fg=self.fg,
                font=self.f_main, padx=px, pady=py,
            )
            chip.pack(side="left", padx=gx, pady=py)
            self.zh_chips.append(chip)

        # ---- 译文行 ----
        self.en_row = tk.Frame(inner, bg=self.row_bg)
        self.en_row.pack(fill="x", padx=mar, pady=gx)
        self.en_tag = tk.Label(
            self.en_row, text="EN", bg=self.row_bg, fg=self.dim, font=self.f_hint, width=3
        )
        self.en_tag.pack(side="left", padx=(px - gx, px), pady=py)
        self.en_chips: list[tk.Label] = []
        for i in range(self.max_en):
            chip = tk.Label(
                self.en_row, text="", bg=self.row_bg, fg=self.fg,
                font=self.f_sub, padx=px, pady=py,
            )
            chip.pack(side="left", padx=gx, pady=py)
            self.en_chips.append(chip)

        # ---- 底栏提示 ----
        self.hint_label = tk.Label(
            inner, text=self.hint, bg=self.bg, fg=self.dim, font=self.f_hint, anchor="w"
        )
        self.hint_label.pack(fill="x", padx=mar, pady=(py, py * 2))

    def _apply_noactivate(self) -> None:
        try:
            touched = W.make_noactivate_deep(int(self.root.winfo_id()))
            if self.verbose:
                print(f"[overlay] 已对 {len(touched)} 层窗口加 NOACTIVATE: {touched}")
        except Exception as exc:
            print(f"[overlay] 设置 NOACTIVATE 失败：{exc!r}")

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def render(self, st: dict) -> None:
        """按状态快照重画。状态没变就直接返回，避免无谓的重排。"""
        # 换到另一块 DPI 不同的显示器时，控件会被销毁重建，重排完要按这份状态重画；
        # 所以哪怕是「没变、直接返回」的早退分支，也要先把最新状态记下来。
        self._state = st
        if not st.get("buffer"):
            toast = st.get("toast") or ""
            key = ("__toast__", toast)
            if key == self._last_key and self.visible:
                return
            self._last_key = key
            if toast:
                self._render_toast(toast)
            else:
                self.hide()
            return

        key = (
            st["buffer"], tuple(st["zh"]), tuple(st["en"]), st["active"],
            tuple(st["sel"]), st.get("tr_state"), st.get("status"), st.get("mock"),
            st.get("toast") or "",
        )
        if key == self._last_key and self.visible:
            return
        self._last_key = key
        self._restore_rows()

        active = st["active"]
        zh_active = active == ROW_ZH
        en_active = active == ROW_EN

        # 顶栏
        self.buf_label.config(text=_clip(st["buffer"], BUF_MAX_CHARS))
        row_name = "译文行" if en_active else "中文行"
        bits = [f"第 {active + 1} 行 · {row_name}"]
        if st.get("mock"):
            bits.append("mock 翻译")
        if st.get("status"):
            bits.append(st["status"])
        self.mode_label.config(text=_clip("   ".join(bits), MODE_MAX_CHARS))

        self.zh_row.configure(bg=self.row_active_bg if zh_active else self.row_bg)
        self.zh_tag.configure(
            bg=self.row_active_bg if zh_active else self.row_bg,
            fg=self.accent_fg if zh_active else self.dim,
        )
        self._fill_row(
            self.zh_chips, st["zh"], st["sel"][ROW_ZH], zh_active,
            self.f_main, self.row_active_bg if zh_active else self.row_bg,
            max_chars=ZH_CHIP_MAX_CHARS,
        )

        self.en_row.configure(bg=self.row_active_bg if en_active else self.row_bg)
        self.en_tag.configure(
            bg=self.row_active_bg if en_active else self.row_bg,
            fg=self.accent_fg if en_active else self.dim,
        )
        en_items = list(st["en"])
        tr_state = st.get("tr_state")
        if not en_items:
            if tr_state == "pending":
                en_items = ["翻译中…"]
            elif tr_state == "failed":
                en_items = ["翻译失败"]
            elif tr_state == "off":
                en_items = ["翻译已关闭"]
            else:
                en_items = ["…"]
        self._fill_row(
            self.en_chips, en_items, st["sel"][ROW_EN], en_active,
            self.f_sub, self.row_active_bg if en_active else self.row_bg,
            placeholder=not st["en"], max_chars=EN_CHIP_MAX_CHARS,
        )

        # 量一遍真实像素宽度，把塞不下的候选藏起来（而不是让窗口边缘切掉它们）
        self.root.update_idletasks()
        self.n_zh_shown = self._fit_row(self.zh_chips, self.zh_tag)
        self.n_en_shown = self._fit_row(self.en_chips, self.en_tag)

        # 被藏起来的候选仍然能用 ←→ / 数字键选到，但屏幕上一点痕迹都没有 ——
        # 用户只会以为「后面没有候选了」。这里在当前行顶栏说实话，没有就不提。
        if zh_active:
            hidden = max(0, len(st["zh"]) - self.n_zh_shown)
        elif en_active:
            hidden = max(0, len(en_items) - self.n_en_shown)
        else:
            hidden = 0
        if hidden:
            bits.append(f"还有 {hidden} 个没显示（←→ 仍可选）")
            self.mode_label.config(text=_clip("   ".join(bits), MODE_MAX_CHARS))

        self.show()

    def _fill_row(
        self,
        chips: list[tk.Label],
        items: list[str],
        sel: int,
        is_active: bool,
        font,
        base_bg: str,
        placeholder: bool = False,
        max_chars: int = 0,
    ) -> None:
        for i, chip in enumerate(chips):
            if i < len(items):
                item = _clip(items[i], max_chars) if max_chars else items[i]
                text = f"{i + 1} {item}"
                chip.configure(text=text, font=font)
                if placeholder:
                    chip.configure(bg=base_bg, fg=self.dim)
                elif is_active and i == sel:
                    chip.configure(bg=self.accent_bg, fg=self.accent_fg)
                elif not is_active and i == sel:
                    chip.configure(bg=self.sel_soft, fg=self.fg)
                else:
                    chip.configure(bg=base_bg, fg=self.fg)
                chip.pack(side="left", padx=self.gap_x, pady=self.pad_y)
            else:
                chip.pack_forget()

    def _fit_row(self, chips: list[tk.Label], tag: tk.Label) -> int:
        """只保留塞得进窗口宽度上限的候选块，多余的藏起来。

        候选可能很长（`jintiantianqizenmeyang` 能出「今天天气怎么样」），7 个候选
        横排会远超 ``max_width``。硬排的结果是右边几个被窗口边缘**切掉** ——
        看起来像候选丢了。这里按实际像素宽度从右往左砍，被砍掉的候选仍然能用
        ←→ / 数字键选到，只是不显示。返回实际显示了几个。
        """
        px, gx = self.pad_x, self.gap_x
        avail = self.max_width - 2 * getattr(self, "_row_margin", 18) - 4
        shown = [c for c in chips if c.winfo_manager() == "pack"]
        fixed = tag.winfo_reqwidth() + (px - gx) + px + 2 * gx
        while shown:
            need = fixed + sum(c.winfo_reqwidth() + 2 * gx for c in shown)
            if need <= avail:
                break
            shown.pop().pack_forget()
        return len(shown)

    # ------------------------------------------------------------------ #
    # 显隐
    # ------------------------------------------------------------------ #
    def top_hwnd(self) -> int:
        if self._top is None:
            self._top = W.top_level_hwnd(int(self.root.winfo_id()))
        return self._top

    def _mine(self) -> tuple[int, int]:
        return self.top_hwnd(), int(self.root.winfo_id())

    def _is_ours(self, hwnd: int) -> bool:
        """这个前台窗口是不是我们自己（本进程）的？

        不能只比对两个已知 hwnd：Tk 会额外造包装窗口，而且 Windows 还可能
        把前台给到本进程的别的窗口。凡是本进程的窗口都算「我们的」，
        否则焦点被自己抢走时反而判不出来、白白漏掉还原。
        """
        if not hwnd:
            return False
        if hwnd in self._mine():
            return True
        return W.window_pid(hwnd) == os.getpid()

    def _restore_real_foreground(self) -> bool:
        """如果前台被我们自己占了，还给用户原来的窗口。

        这是**必须**的：Tk 建根窗口那一刻就会把悬浮窗顶成前台窗口，
        而这时它自己还是 withdrawn 的、用户根本看不见。若不还回去，
        用户当前正在用的窗口就丢了焦点，紧接着的 SendInput 会注错地方。
        """
        if not self._real_fg:
            return False
        cur = W.foreground_hwnd()
        if cur and not self._is_ours(cur):
            return False  # 前台本来就是别人的，不用多事
        ok = W.set_foreground(self._real_fg)
        if not ok:
            # 先分清「谁不理人」：想还回去的那个窗口卡住，和「当前前台卡住不敢碰」，
            # 都不是「还原失败」这种故障，而是**故意跳过** —— 真去碰它会同步阻塞，
            # 把整个启动流程一起拖死（见 winapi.set_foreground 的实测记录）。
            stuck = 0
            if not W.is_responsive(self._real_fg):
                stuck = self._real_fg
                why = "要还回去的窗口"
            elif cur and not W.is_responsive(cur):
                stuck = cur
                why = "当前前台窗口"
            if stuck:
                if getattr(self, "_restore_warned", None) != stuck:
                    self._restore_warned = stuck
                    print(f"[overlay] {why}没响应（{stuck}），跳过还原 —— 不碰卡住的窗口",
                          file=sys.stderr)
            # 这条**必须**打出来：还原失败意味着接下来注入的字会掉进我们自己
            # 的窗口里，用户看到的现象是「打字没反应」，很难往这儿想。
            elif getattr(self, "_restore_warned", None) != cur:
                self._restore_warned = cur
                print(f"[overlay] 警告：前台窗口还原失败（{cur} -> {self._real_fg}）", file=sys.stderr)
        elif self.verbose and getattr(self, "_restore_logged", None) != self._real_fg:
            self._restore_logged = self._real_fg
            print(f"[overlay] 已把前台窗口还给 {self._real_fg}")
        return ok

    # ------------------------------------------------------------------ #
    # 短提示：缓冲为空时也能显示一条消息（模式切换、挂起/恢复）
    # ------------------------------------------------------------------ #
    def _render_toast(self, text: str) -> None:
        """只显示提示，把候选行收起来 —— 不然空窗口看起来像坏了。

        带换行的文本按多行原样显示（启动引导就是多行的），单行文本才按
        ``BUF_MAX_CHARS`` 截断。多行时让 Tk 自己去量宽度，别横着撑爆屏幕。
        """
        multi = "\n" in text
        if not self._toast_mode:
            self.head.pack_forget()
            self.zh_row.pack_forget()
            self.en_row.pack_forget()
            self.hint_label.pack_forget()
            self.toast_label.pack(
                fill="x", padx=self._row_margin, pady=(self.pad_y * 2, self.pad_y * 2)
            )
            self._toast_mode = True
        self.toast_label.config(
            text=text if multi else _clip(text, BUF_MAX_CHARS),
            justify="left",
            anchor="w",
        )
        self.show()

    def _restore_rows(self) -> None:
        """从「只有一条提示」切回正常的候选窗布局。"""
        if not self._toast_mode:
            return
        px, py, gx = self.pad_x, self.pad_y, self.gap_x
        mar = self._row_margin
        self.toast_label.pack_forget()
        self.head.pack(fill="x", padx=mar, pady=(py * 2, py))
        self.zh_row.pack(fill="x", padx=mar, pady=gx)
        self.en_row.pack(fill="x", padx=mar, pady=gx)
        self.hint_label.pack(fill="x", padx=mar, pady=(py, py * 2))
        self._toast_mode = False
        if self.visible:
            # 提示条还挂在屏幕上时用户就开始打字：窗口没有重新 show（还是可见的），
            # 但这一刻起屏幕上真的是候选窗了 —— 不在这里记一笔，快速打字的人会
            # 永远得到 cand_shows=0，诊断又会反过来说「候选窗一次都没弹过」。
            self.cand_shows += 1

    def is_ours(self, hwnd: int) -> bool:
        """这个窗口是不是本进程的（含 Tk 的包装窗口）。"""
        return self._is_ours(hwnd)

    def restore_foreground(self) -> bool:
        """如果前台被我们自己的窗口占了，还给用户真正在用的那个窗口。

        ``SendInput`` 只送到当前前台窗口，所以上屏之前必须先确认前台不是自己 ——
        否则注入的字会掉进候选窗里，用户看到的是「打了字没反应」。
        """
        return self._restore_real_foreground()

    def show(self) -> None:
        """显示候选窗，**且绝不抢焦点**。

        这里刻意不用 Tk 的 ``deiconify()`` —— 它内部是 ``ShowWindow(SW_SHOW)``，
        会把悬浮窗顶成前台窗口；之后 ``SendInput`` 注入的字符就全落进悬浮窗自己，
        目标程序一个字都收不到（已在本机复现）。
        """
        first = not self.visible
        cur = W.foreground_hwnd()
        if cur and not self._is_ours(cur):
            self._real_fg = cur  # 记住用户此刻真正在用的窗口

        # 每次状态变化都要重算位置和宽度，**不能只在第一次显示时算**：
        # 候选是随着打字一个个冒出来的，窗口宽度得跟着长。以前只在首次
        # show() 时定位，窗口就永远停在「刚打第一个字母」时的宽度，后面
        # 的候选全被右边缘切掉。（render() 自带变更检测，所以这里并不频繁。）
        self.place_near_caret()

        if first:
            # 窗口每次 withdraw 之后再显示，扩展样式有可能被 Tk 重置，显示前再刷一遍
            W.make_noactivate_deep(int(self.root.winfo_id()))
            ok = W.show_no_activate(self.top_hwnd())
            self.root.attributes("-topmost", True)
            self.visible = True
            self.shows += 1
            if not self._toast_mode:
                # 只数「候选窗真的弹出来」的次数，短提示不算（第 86 轮）。
                self.cand_shows += 1
            self._refresh_visibility()
            if ok:
                self.show_error = None
            else:
                # 显示之后 IsWindowVisible 还是假 = 用户什么都不会看见，而这是静默的
                self.show_error = "窗口显示后 IsWindowVisible 仍为假（用户看不到候选窗）"

        self._restore_real_foreground()

    def _refresh_visibility(self) -> None:
        """把「窗口此刻在不在屏幕上」记成事实，供状态文件 / 控制面板 / 体检读。"""
        hwnd = self.top_hwnd()
        rect = W.window_rect(hwnd) if hwnd else None
        self.last_rect = rect
        self.on_monitor = bool(hwnd) and bool(W.monitor_of_window(hwnd))

    @property
    def mode(self) -> str:
        """窗口现在是什么：``hidden`` / ``candidates`` / ``toast``。

        只有 ``visible`` 一个布尔值时，外面（状态文件、面板、测试）没法区分
        「候选窗真的开着」和「屏幕上只有一条提示」—— 第 57 轮实机复验里，
        切窗口之后候选确实收起来了，但托盘失败的提示条正好接着显示，
        ``visible`` 仍是真，就被误判成「候选窗没清理」。
        """
        if not self.visible:
            return "hidden"
        return "toast" if self._toast_mode else "candidates"

    def set_clipboard(self, text: str) -> bool:
        """把文本放进系统剪贴板（上屏失败时的兜底）。

        上屏走的是 ``SendInput``，而 UIPI 会把它拦在**比本程序权限高**的窗口外面，
        而且拦住时 ``SendInput`` 还返回成功（见 ``winapi.can_inject_into``）。这时候
        把文字放进剪贴板、让用户自己 Ctrl+V，是唯一还能把活儿干完的办法。

        必须在 Tk 线程上调用（它会碰窗口对象）。失败返回 ``False``，不抛。
        """
        if not text:
            return False
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            return True
        except Exception as exc:
            print(f"[overlay] 写剪贴板失败：{exc!r}", file=sys.stderr)
            return False

    def hide(self) -> None:
        if not self.visible:
            return
        # 同样绕开 Tk 的 withdraw()，直接 SW_HIDE，避免 Tk 顺手改前台
        W.hide_window(self.top_hwnd())
        self.root.withdraw()
        self.visible = False
        self._last_key = None
        self._restore_real_foreground()

    def place_near_caret(self) -> None:
        """贴着目标窗口的输入光标放；拿不到 caret 就退回鼠标位置。"""
        x, y = W.caret_screen_pos() or W.cursor_pos()
        # 先按「光标那块屏」的 DPI 对齐缩放比：换屏之后字号、边距、宽度上限
        # 都得跟着变，而且必须赶在下面量 winfo_reqwidth() 之前完成重排，
        # 否则量到的还是旧字号下的宽度。
        self._sync_scale(x, y)
        self.root.update_idletasks()
        w = max(self.root.winfo_reqwidth(), self.min_width)
        h = self.root.winfo_reqheight()
        if w > self.max_width:
            w = self.max_width
        # 按「光标所在那块显示器」来夹，而不是主显示器：
        # 光标在副屏上时，用主屏宽度去量副屏，会把窗口顶回主屏（甚至屏幕外）。
        # 这里要的是**工作区**（work=True），不是整块屏：任务栏占掉的那一条
        # 系统会算进显示器、但不会画任何东西，用整块屏去夹就会把候选窗摆到
        # 任务栏底下 —— 用户看到的是「候选窗缺了半截」，这是第 46 轮实机
        # 抓到的（光标在底部聊天框里时，窗口底边被任务栏遮住几十像素）。
        left, top, mw, mh = W.monitor_rect(x=x, y=y, work=True) or (
            0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        y = y + self.offset_y
        if x + w > left + mw - 8:
            x = max(left + 8, left + mw - w - 8)
        if y + h > top + mh - 8:  # 下方放不下就翻到光标上面
            y = max(top + 8, y - h - self.offset_y * 2)
            # 翻上去还是压着工作区底边（窗口比光标上方的空间还高）就贴着
            # 工作区底边 —— 与光标重叠一下，也好过被任务栏吃掉一截。
            if y + h > top + mh - 8:
                y = max(top + 8, top + mh - h - 8)
        self.root.geometry(f"{w}x{h}+{int(x)}+{int(y)}")

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def after(self, ms: int, fn) -> None:
        self.root.after(ms, fn)

    def run(self) -> None:
        self.root.mainloop()

    def destroy(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass
