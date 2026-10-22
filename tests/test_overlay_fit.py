# -*- coding: utf-8 -*-
"""候选行的「塞不下就藏起来」：README 承诺了它，却一行都没测过。

README 写着：

    候选行会按真实像素宽度裁剪：塞不下的候选被**藏起来**，而不是被窗口边缘
    切一刀（切一刀看起来像候选丢了），它们仍然可以用 ``←→`` / 数字键选到。

代码里做这件事的是 ``Overlay._fit_row()`` 与三个 ``*_MAX_CHARS`` 常量（还有
``_clip()``）。``grep`` 遍 ``tests/``：``_fit_row`` / ``_clip`` / ``max_width``
**零命中** —— 也就是说这条被写进文档的界面行为，从来没有被断言过。它要是坏了，
用户看到的是「候选莫名其妙少了一半」，或者「长候选被窗口切掉」，而且没有任何提示。

而且真有一处缺口：被藏起来的候选屏幕上一点痕迹都没有，用户根本不知道「后面还有」。
本测试钉住：

  1. ``_clip()``：中文 10 字 / 译文 30 字 / 缓冲 40 字，超了是「前 N-1 个 + …」；
  2. ``_fit_row()``：只从右边砍、按真实像素算、全塞不下时返回 0、刚好塞满不砍；
  3. 真窗口渲染：``max_width`` 小的时候候选被藏起来、顶栏说「还有 N 个没显示」，
     宽的时候一个都不藏、也绝不多嘴；
  4. 被藏起来的候选**数字键仍然选得到**（README 的承诺，在状态机层验）。

直接跑：``python tests/test_overlay_fit.py``
"""
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
sys.path.insert(0, str(PROJ))

from t_ime.config import load as load_config  # noqa: E402
from t_ime.console import fix_console_encoding  # noqa: E402
from t_ime.core import IMECore, ROW_EN, ROW_ZH, TranslateWorker, Translator  # noqa: E402
from t_ime.overlay import (  # noqa: E402
    BUF_MAX_CHARS,
    EN_CHIP_MAX_CHARS,
    MODE_MAX_CHARS,
    Overlay,
    ZH_CHIP_MAX_CHARS,
    _clip,
)
from t_ime.pinyin import PinyinEngine, load_or_build_index  # noqa: E402

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


def sec(title):
    print(f"\n-- {title} " + "-" * max(0, 70 - len(title)))


def snap(zh, en=None, active=ROW_ZH, sel=(0, 0), buffer="jintiantianqizenmeyang",
         tr_state="done", status="", mock=False, toast=""):
    return {
        "enabled": True,
        "direct": False,
        "caps": False,
        "toast": toast,
        "buffer": buffer,
        "zh": list(zh),
        "en": list(en or []),
        "active": active,
        "sel": list(sel),
        "tr_state": tr_state,
        "tr_text": (zh[0] if zh else ""),
        "status": status,
        "mock": mock,
        "hotkeys": {},
    }


def packed(ov, chips):
    return sum(1 for c in chips if c.winfo_manager() == "pack")


# --------------------------------------------------------------------------- #
# 假的候选块：只实现 _fit_row 用到的那三样东西，不建 Tk
# --------------------------------------------------------------------------- #
class FakeChip:
    def __init__(self, width):
        self.width = width
        self.manager = "pack"
        self.forgotten = 0

    def winfo_manager(self):
        return self.manager

    def winfo_reqwidth(self):
        return self.width

    def pack_forget(self):
        self.manager = None
        self.forgotten += 1


class FakeTag:
    def __init__(self, width):
        self.width = width

    def winfo_reqwidth(self):
        return self.width


def fake_self(max_width, pad_x=6, gap_x=4, row_margin=12):
    return SimpleNamespace(max_width=max_width, pad_x=pad_x, gap_x=gap_x,
                           _row_margin=row_margin)


def fit(max_width, widths, tag_width, **kw):
    me = fake_self(max_width, **kw)
    chips = [FakeChip(w) for w in widths]
    shown = Overlay._fit_row(me, chips, FakeTag(tag_width))
    return shown, chips


cfg, cfg_path = load_config()
print(f"配置来自 {cfg_path}")

try:
    # ----------------------------------------------------------------------- #
    sec("1. _clip()：三种长度上限（中文 10 / 译文 30 / 缓冲 40）")
    # ----------------------------------------------------------------------- #
    check("短文本原样不动", _clip("你好", ZH_CHIP_MAX_CHARS) == "你好")
    check("刚好等于上限也不截", _clip("x" * ZH_CHIP_MAX_CHARS, ZH_CHIP_MAX_CHARS)
          == "x" * ZH_CHIP_MAX_CHARS)
    long_zh = _clip("中华人民共和国万岁万万岁", ZH_CHIP_MAX_CHARS)
    check(f"中文超 {ZH_CHIP_MAX_CHARS} 字被截", len(long_zh) == ZH_CHIP_MAX_CHARS,
          len(long_zh))
    check("截断用省略号收尾", long_zh.endswith("…"), long_zh)
    check("截断保留前面 max-1 个字",
          long_zh == "中华人民共和国万岁万万岁"[: ZH_CHIP_MAX_CHARS - 1] + "…", long_zh)
    long_en = _clip("The quick brown fox jumps over the lazy dog", EN_CHIP_MAX_CHARS)
    check(f"译文按 {EN_CHIP_MAX_CHARS} 字截", len(long_en) == EN_CHIP_MAX_CHARS,
          len(long_en))
    long_buf = _clip("a" * 60, BUF_MAX_CHARS)
    check(f"缓冲行按 {BUF_MAX_CHARS} 字截", len(long_buf) == BUF_MAX_CHARS, len(long_buf))
    # 退化情况：limit=1 时留 1 个字 + 省略号 = 2 个字（多出 1 个，但 1 字宽本来就放不下省略号）
    check("limit=1 时是「一个字 + 省略号」", _clip("abc", 1) == "a…", _clip("abc", 1))
    check("非字符串也能处理（数字转成文本）", _clip(12345, 3) == "12…",
          _clip(12345, 3))
    check("中文按字符数算（不是字节）",
          len(_clip("今天天气怎么样啊哈哈哈", ZH_CHIP_MAX_CHARS)) == ZH_CHIP_MAX_CHARS)
    check("空串原样返回", _clip("", ZH_CHIP_MAX_CHARS) == "")

    # ----------------------------------------------------------------------- #
    sec("2. _fit_row()：按像素从右往左砍，边界条件")
    # ----------------------------------------------------------------------- #
    # avail = max_width - 2*row_margin - 4；fixed = tag + (pad-gap) + pad + 2*gap
    n, chips = fit(2000, [30, 30, 30, 30], 40)
    check("宽窗口：一个都不砍", n == 4 and all(c.forgotten == 0 for c in chips), n)
    # avail=172, fixed=40+2+6+8=56, 每个块 +8：56+4*38=208 > 172 → 砍到 3 个（132 ≤ 172）
    n, chips = fit(200, [30, 30, 30, 30], 40)
    check("窄窗口：从右边开始砍", n == 3, n)
    check("砍掉的是最后一个", chips[3].manager is None and chips[2].manager == "pack")
    check("前面的块顺序不变、一个都没被藏",
          all(c.manager == "pack" for c in chips[:n]))
    check("被藏起来的块只 forget 一次", chips[3].forgotten == 1, chips[3].forgotten)
    # 刚好塞满：fixed = 40+2+6+8 = 56，每块 30+8=38，两块 132；avail = max-2*12-4
    # 要 avail == 132 ⇒ max_width == 160
    n, chips = fit(160, [30, 30], 40)
    check("刚好塞满时一个都不砍（need <= avail 就算塞得下）",
          n == 2 and all(c.forgotten == 0 for c in chips), n)
    n, chips = fit(159, [30, 30], 40)
    check("少 1 像素就要砍（边界是 <=，不是 <）", n == 1, n)
    n, chips = fit(60, [30, 30], 40)
    check("连一个都塞不下时返回 0", n == 0, n)
    check("都塞不下时所有块都被藏", all(c.manager is None for c in chips))
    n, chips = fit(200, [10, 10, 400], 40)
    check("第一块就超宽：前面的小字留着、超宽的砍掉",
          chips[2].manager is None and chips[0].manager == "pack", (n, [c.manager for c in chips]))
    # tag（中 / EN）本身也占宽度：tag 很宽时能放下的块更少
    n_wide_tag, _ = fit(200, [30, 30, 30, 30], 300)
    check("行首标签也占宽度（标签太宽就放不下候选）", n_wide_tag == 0, n_wide_tag)

    # ----------------------------------------------------------------------- #
    sec("3. 真窗口：max_width 小时藏候选并在顶栏说实话")
    # ----------------------------------------------------------------------- #
    zh_long = ["今天天气怎么样", "中华人民共和国", "中国人民解放军", "数据结构与算法"]
    narrow = copy.deepcopy(cfg)
    narrow["overlay"]["max_width"] = 260
    narrow["overlay"]["min_width"] = 120
    ov = Overlay(narrow, verbose=False)
    ov.render(snap(zh_long, en=["How is the weather today when it rains",
                                "The People's Republic of China is large",
                                "Chinese People's Liberation Army salutes",
                                "Data structures and algorithms matter"]))
    check("候选窗真的显示了", ov.visible is True)
    check("窄窗口下确实藏了候选", ov.n_zh_shown < len(zh_long), ov.n_zh_shown)
    hidden = len(zh_long) - ov.n_zh_shown
    check("顶栏说出了藏了几个",
          f"还有 {hidden} 个没显示" in ov.mode_label.cget("text"),
          ov.mode_label.cget("text"))
    check("藏了几个和实际打包数对得上",
          packed(ov, ov.zh_chips) == ov.n_zh_shown, packed(ov, ov.zh_chips))
    ov.root.update_idletasks()  # Tk 的 geometry 要一拍才生效，否则读到上一版宽度
    w = ov.root.winfo_width()
    check("窗口宽度没有超过 max_width（按缩放后的像素上限）",
          w <= ov.max_width + 2, f"{w} vs {ov.max_width}")
    check("顶栏文本自己也受长度上限约束",
          len(ov.mode_label.cget("text")) <= MODE_MAX_CHARS,
          len(ov.mode_label.cget("text")))

    # 切到译文行：这时报的是译文行藏了几个
    ov.render(snap(zh_long, en=["How is the weather today when it rains",
                                "The People's Republic of China is large",
                                "Chinese People's Liberation Army salutes",
                                "Data structures and algorithms matter"],
                   active=ROW_EN, sel=(0, 1)))
    en_hidden = 4 - ov.n_en_shown
    if en_hidden > 0:
        check("切到译文行后，报的是译文行藏了几个",
              f"还有 {en_hidden} 个没显示" in ov.mode_label.cget("text"),
              ov.mode_label.cget("text"))
    else:
        check("译文行全塞得下时不多嘴",
              "没显示" not in ov.mode_label.cget("text"),
              ov.mode_label.cget("text"))
    ov.destroy()

    wide = copy.deepcopy(cfg)
    wide["overlay"]["max_width"] = 1400
    ov2 = Overlay(wide, verbose=False)
    ov2.render(snap(zh_long, en=["How is the weather today", "What's the weather like"]))
    check("宽窗口下一个都不藏", ov2.n_zh_shown == len(zh_long), ov2.n_zh_shown)
    check("全都显示得下时不提『还有 N 个』",
          "没显示" not in ov2.mode_label.cget("text"),
          ov2.mode_label.cget("text"))
    check("全部打包了", packed(ov2, ov2.zh_chips) == len(zh_long))
    ov2.destroy()

    # ----------------------------------------------------------------------- #
    sec("3b. min_width > max_width：两个值各自合法，但不许渲染得比下限还窄")
    # ----------------------------------------------------------------------- #
    # 第 99 轮真机数据：min_width=1200 / max_width=200（@1.5x → 1800 / 300）时，
    # 宽度链是 `w = max(req, min_width)` 再 `if w > max_width: w = max_width`，
    # 于是最终只有 300px —— 比用户要求的最小宽度还窄，候选被 _fit_row 按 300 裁掉。
    conflict = copy.deepcopy(cfg)
    conflict["overlay"]["min_width"] = 1200
    conflict["overlay"]["max_width"] = 200
    ov3b = Overlay(conflict, verbose=False)
    ov3b.render(snap(zh_long))
    ov3b.root.update_idletasks()
    check("max_width 被抬到 min_width（下限优先）",
          ov3b.max_width >= ov3b.min_width, (ov3b.min_width, ov3b.max_width))
    check("真实窗口宽度不小于 min_width（不许比你要的最小宽度还窄）",
          ov3b.root.winfo_width() >= ov3b.min_width,
          (ov3b.root.winfo_width(), ov3b.min_width))
    ov3b.destroy()

    # ----------------------------------------------------------------------- #
    sec("4. 被藏起来的候选，数字键仍然选得到（README 的承诺）")
    # ----------------------------------------------------------------------- #
    index = load_or_build_index(
        PROJ / "pinyin_index.pkl", min_freq=20, max_word_len=6, verbose=False
    )
    eng = PinyinEngine(index, beam_width=24, max_candidates=7)
    tr = Translator(cfg, verbose=False)
    worker = TranslateWorker(tr, debounce=0.05, verbose=False)
    worker.start()
    commits: list[str] = []
    try:
        core = IMECore(cfg, eng, tr, emit=lambda st: None,
                       committer=lambda text: (commits.append(text), True)[1],
                       verbose=False)
        core._input("nihao")
        zh = list(core.zh)
        check("nihao 有 7 个候选（够验证「藏起来的第 7 个」）", len(zh) == 7, len(zh))
        swallowed = core.handle_key(0x37, True, False)  # 数字 7
        check("第 7 个候选的键被吞掉（不进缓冲）", swallowed is True)
        check("数字键把第 7 个候选上屏了", commits == [zh[6]], (commits, zh[6]))
        check("上屏后缓冲清空", core.buffer == "")
        # 超出候选数
        core._input("nihao")
        commits.clear()
        core.handle_key(0x39, True, False)  # 数字 9
        check("按没有的第 9 个不会上屏", commits == [])
        check("按没有的第 9 个会给出人话提示",
              "没有第 9 个候选" in core.status, core.status)
        check("按没有的第 9 个时缓冲还在（不把用户打了一半的字弄丢）",
              core.buffer == "nihao", core.buffer)
    finally:
        worker.stop()
        worker.join(timeout=3.0)

    # ----------------------------------------------------------------------- #
    sec("5. mode：说清楚现在是「隐藏 / 候选 / 提示条」")
    # ----------------------------------------------------------------------- #
    # 只有一个 visible 布尔值时，外面分不清「候选窗真开着」和「屏幕上只有一条
    # 提示」。第 57 轮实机复验就踩到了：切窗口后候选收起了，托盘失败的提示条
    # 正好接着显示，visible 仍是真，测试误判成「候选窗没清理」。
    ov3 = Overlay(copy.deepcopy(cfg), verbose=False)
    check("刚建好是 hidden", ov3.mode == "hidden", ov3.mode)
    check("hidden 时 visible 也是假", ov3.visible is False, ov3.visible)
    ov3.render(snap(["你好", "你号"], buffer="nihao"))
    check("有候选时是 candidates", ov3.mode == "candidates", ov3.mode)
    ov3.render(snap([], buffer="", toast="托盘图标没装上（不影响打字）"))
    check("只显示一条提示时是 toast（不能当成候选窗）", ov3.mode == "toast",
          (ov3.mode, ov3.visible))
    ov3.render(snap([], buffer="", toast=""))
    check("提示条也收掉后回到 hidden", ov3.mode == "hidden",
          (ov3.mode, ov3.visible))
    # 第 86 轮：shows 数的是「窗口显示过几次」，提示条也算 —— 拿它当「候选窗出现过」
    # 的证据，就会在只弹过启动引导的机器上判出「候选窗显示过 2 次」。
    check("候选窗次数与窗口次数分开数：有候选时两边都 +1",
          (ov3.shows, ov3.cand_shows) == (1, 1), (ov3.shows, ov3.cand_shows))
    ov4 = Overlay(copy.deepcopy(cfg), verbose=False)
    ov4.render(snap([], buffer="", toast="启动引导：语言栏里没有它是正常的"))
    check("只弹提示：shows 涨了，但候选窗一次都没出现过",
          (ov4.shows, ov4.cand_shows) == (1, 0), (ov4.shows, ov4.cand_shows))
    ov4.render(snap(["你好", "你号"], buffer="nihao"))
    check("提示条还在屏幕上时开始打字，也算候选窗出现过一次",
          (ov4.shows, ov4.cand_shows) == (1, 1), (ov4.shows, ov4.cand_shows))
    ov4.destroy()
    ov3.destroy()

    # ----------------------------------------------------------------------- #
    sec("6. 收尾")
    # ----------------------------------------------------------------------- #
    check("模块常量与 README 写的一致（10 / 30 / 40）",
          (ZH_CHIP_MAX_CHARS, EN_CHIP_MAX_CHARS, BUF_MAX_CHARS) == (10, 30, 40),
          (ZH_CHIP_MAX_CHARS, EN_CHIP_MAX_CHARS, BUF_MAX_CHARS))
finally:
    print("\n" + "=" * 74)
    total = PASS + FAIL
    print(f"结果：{PASS}/{total} 通过" + ("" if not FAIL else f"  ({FAIL} 项失败)"))
    print("=" * 74)
    sys.exit(0 if FAIL == 0 else 1)
