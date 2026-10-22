# -*- coding: utf-8 -*-
"""候选窗定位：它必须始终落在**光标所在那块显示器的工作区**里。

「候选窗根本不出现」有一种最难查的原因：窗口句柄有效、``IsWindowVisible``
也是 True，但坐标算错，窗口被摆到了显示器外面 —— 用户那边就是「打字没反应，
候选窗根本不出现」。这个测试把光标位置钉死（猴子补丁），然后直接检查
``place_near_caret()`` 算出来的窗口矩形，不需要任何人敲键盘。

第 3 组用一块**虚构的副屏**（在主屏右边）验证：夹取要按光标所在那块屏算，
而不是拿主屏宽度去量副屏（那样会把窗口顶回主屏，甚至顶出屏幕）。

第 4 组是第 46 轮补的：光标在屏幕底部、窗口下方放不下时，夹取必须按
**工作区**（扣掉任务栏）算。按整块显示器算的话，窗口底边会伸进任务栏底下
—— 任务栏是系统画的，候选窗那半截就被挡住了，用户看到的是「候选窗缺一截」。
本机（2560x1440，150%，任务栏 72 像素）实测：光标 y=1150 时旧算法把窗口摆到
y=1192、底边 1422，比工作区底边 1368 多出 54 像素。

直接跑：``python tests/test_overlay_place.py``
"""
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

from t_ime import winapi as W  # noqa: E402
from t_ime.app import demo_snapshot  # noqa: E402
from t_ime.config import load  # noqa: E402

OK = "[OK]  "
BAD = "[FAIL]"
results: list[tuple[bool, str]] = []


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"  — {detail}" if detail else ""))
    return cond


cfg, _ = load()

from t_ime.overlay import Overlay  # noqa: E402

_real_caret = W.caret_screen_pos
REAL_MONITOR_RECT = W.monitor_rect

ov = Overlay(cfg, verbose=False)

# Overlay.__init__ 里会开 DPI 感知，这之后读到的显示器坐标才是物理像素，
# 所以要在建完 Overlay 之后再量尺寸（建之前拿到的是被系统缩放过的假坐标）。
FULL = REAL_MONITOR_RECT(x=0, y=0) or (0, 0, 1920, 1080)
WORK = REAL_MONITOR_RECT(x=0, y=0, work=True) or FULL
mx, my, _, _ = FULL
wx, wy, ww, wh = WORK
print(f"主显示器 {FULL}，工作区 {WORK}（任务栏占掉的高 = {FULL[3] - WORK[3]}）")

# 渲染一份真候选内容，尺寸才是真的（空窗口算出来的宽度没有意义）
ov.render(demo_snapshot())
ov.root.update_idletasks()


def place_at(px: int, py: int):
    """把光标钉在 (px, py)，摆一次窗口，返回窗口真实矩形。"""
    W.caret_screen_pos = lambda: (px, py)
    ov.place_near_caret()
    ov.root.update_idletasks()
    ov.root.update()
    return W.window_rect(ov.top_hwnd())


def inside_work(r) -> bool:
    if not r:
        return False
    x, y, w, h = r
    return (x >= wx and y >= wy
            and x + w <= wx + ww and y + h <= wy + wh)


print("=" * 74)
print("1. 光标在工作区右下角 —— 最容易算到屏幕外 / 压到任务栏的情况")
print("=" * 74)
corner = place_at(wx + ww - 2, wy + wh - 2)
check(corner is not None, "拿到窗口矩形", repr(corner))
if corner:
    x, y, w, h = corner
    check(x >= wx, "左边缘没跑出工作区左边", f"x={x} >= {wx}")
    check(y >= wy, "上边缘没跑出工作区上边", f"y={y} >= {wy}")
    check(x + w <= wx + ww, "右边缘没跑出工作区右边（原本会顶出去）",
          f"{x + w} <= {wx + ww}")
    check(y + h <= wy + wh, "下边缘没跑到任务栏底下（应该翻到光标上方）",
          f"{y + h} <= 工作区底边 {wy + wh}")
    check(inside_work(corner), "整个窗口都在工作区里", repr(corner))

print()
print("=" * 74)
print("2. 光标在工作区左上角 —— 正常位置应该贴着光标下方 offset_y")
print("=" * 74)
near = place_at(wx + 40, wy + 40)
check(near is not None, "拿到窗口矩形", repr(near))
if near:
    x, y, w, h = near
    check(x == wx + 40, "x 就用光标位置（右侧放得下，不需要夹）", f"x={x}")
    check(y == wy + 40 + ov.offset_y, f"y = 光标 + offset_y({ov.offset_y})", f"y={y}")
    check(w >= ov.min_width,
          "宽度不小于 min_width（min_width 在 __init__ 里已经乘过缩放）",
          f"w={w} min_width={ov.min_width} scale={ov.scale:.2f}")

print()
print("=" * 74)
print("3. 虚构的副屏（主屏右边一块）—— 夹取必须按光标所在那块屏算")
print("=" * 74)
# 副屏从主屏右边缘开始，至少要放得下主屏那么宽：第 5 组会把窗口按 200%
# 放大（约 1400px 宽），副屏太窄的话窗口必然放不下。
fake_w = max(2000, FULL[2])
fake_h = FULL[3]
fake_taskbar = FULL[3] - WORK[3]
fake = (mx + FULL[2], my, fake_w, fake_h)


def fake_monitor_rect(hwnd=0, *, x=None, y=None, work=False):
    """把主屏右边那块「当成」存在的副屏。"""
    if x is not None and y is not None and x >= mx + FULL[2]:
        if work:
            return (fake[0], fake[1], fake[2], fake[3] - fake_taskbar)
        return fake
    return REAL_MONITOR_RECT(hwnd, x=x, y=y, work=work)


W.monitor_rect = fake_monitor_rect
try:
    # 光标在副屏工作区的右下角：窗口必须被夹在副屏里，而不是被主屏宽度顶回去
    on2 = place_at(fake[0] + fake_w - 2, fake[1] + fake_h - fake_taskbar - 2)
    check(on2 is not None, "拿到窗口矩形", repr(on2))
    if on2:
        x, y, w, h = on2
        check(x >= fake[0], "窗口留在副屏上（没被主屏宽度顶回左边）",
              f"x={x} >= 副屏左边缘 {fake[0]}")
        check(x + w <= fake[0] + fake_w, "右边缘没越出副屏",
              f"{x + w} <= {fake[0] + fake_w}")
        check(y + h <= fake[1] + fake_h - fake_taskbar,
              "下边缘没越出副屏工作区（不压任务栏）",
              f"{y + h} <= 副屏工作区底边 {fake[1] + fake_h - fake_taskbar}")

    # 光标在副屏左上角：应该原样贴着光标，不被主屏边界干扰
    on2b = place_at(fake[0] + 30, fake[1] + 30)
    if on2b:
        x, y, w, h = on2b
        check(x == fake[0] + 30 and y == fake[1] + 30 + ov.offset_y,
              "副屏左上角光标：原样贴光标", f"{w}x{h}+{x}+{y}")
finally:
    W.monitor_rect = REAL_MONITOR_RECT

print()
print("=" * 74)
print("4. 光标在屏幕底部（底部聊天框那种位置）—— 不能把窗口塞进任务栏底下")
print("=" * 74)
# 这个高度上，窗口下方放不下、但要按整块显示器算又「刚好放得下」：
# 旧算法会把底边摆到 1422（工作区底边 1368 + 54），任务栏盖掉一截。
for delta in (240, 220, 200):
    cy = wy + wh - delta
    r = place_at(wx + 300, cy)
    if check(r is not None, f"光标 y={cy}：拿到窗口矩形", repr(r)):
        x, y, w, h = r
        check(y + h <= wy + wh,
              f"光标 y={cy}：下边缘没跑到任务栏底下",
              f"底边 {y + h} <= 工作区底边 {wy + wh}")
        check(inside_work(r), f"光标 y={cy}：整个窗口都在工作区里", repr(r))

print()
print("=" * 74)
print("5. 换到 DPI 不同的显示器 —— 字号、边距、宽度上限都要按那块屏重算")
print("=" * 74)
# 第 47 轮：以前缩放比只在窗口建出来时取一次 GetDpiForSystem()（主屏），
# 窗口搬到缩放比不同的副屏上时字号会偏大/偏小。现在每次定位都问一次光标
# 所在显示器的 DPI（GetDpiForMonitor），变了就销毁重建控件、按新缩放比重排。
# 本机只有一块屏，所以这条用两部分验证：
#   ① 真实 API：进程开了 DPI 感知之后，主屏 monitor_dpi 必须等于启动时的缩放比；
#   ② 打桩：把「副屏」的 DPI 报成 192（200%），断言真 Tk 控件的字号/宽度/偏移
#      都按 2.0 重算过，并且窗口仍然落在那块屏的工作区里。
base_font = ov.f_main[1]
base_min_width = ov.min_width
base_offset_y = ov.offset_y
base_scale = ov.scale
real_dpi = W.monitor_dpi(x=wx + 10, y=wy + 10)
check(real_dpi == round(ov.scale * 96),
      "真机：主屏 monitor_dpi 与启动时的缩放比一致",
      f"monitor_dpi={real_dpi} scale={ov.scale:.2f}")

REAL_DPI_FN = W.monitor_dpi


def fake_monitor_dpi(x=None, y=None, hwnd=0):
    if x is not None and y is not None and x >= fake[0]:
        return 192  # 副屏 200%
    return REAL_DPI_FN(x=x, y=y, hwnd=hwnd)


W.monitor_rect = fake_monitor_rect
W.monitor_dpi = fake_monitor_dpi
try:
    # 先在工作区里摆一份内容：确认「换屏重排」不会把正在显示的东西搞丢
    ov.render(demo_snapshot())
    on_primary = place_at(wx + 40, wy + 40)
    check(ov.f_main[1] == base_font and ov.min_width == base_min_width,
          "主屏上缩放比没变（没有多余的重排）",
          f"字体 {ov.f_main[1]} min_width={ov.min_width}")

    # 光标挪到 200% 的副屏上：重排必须发生
    on_2 = place_at(fake[0] + 30, fake[1] + 30)
    check(on_2 is not None, "副屏上拿到窗口矩形", repr(on_2))
    expect_font = -max(8, round(abs(base_font) / base_scale * 2.0))
    check(abs(ov.f_main[1]) > abs(base_font),
          "副屏字号按 200% 放大（不是沿用主屏的 150%）",
          f"主屏 {base_font}px -> 副屏 {ov.f_main[1]}px，期望 {expect_font}px")
    check(ov.min_width > base_min_width,
          "宽度下限也跟着换屏放大",
          f"min_width {base_min_width} -> {ov.min_width}")
    check(ov.offset_y > base_offset_y,
          "光标上下的偏移也跟着换屏放大",
          f"offset_y {base_offset_y} -> {ov.offset_y}")
    check(any(c.cget("text") for c in ov.zh_chips),
          "换屏重排之后候选内容还在（没有丢状态）",
          f"第一块={ov.zh_chips[0].cget('text')!r}")
    if on_2:
        inside_fake = (on_2[0] >= fake[0]
                       and on_2[0] + on_2[2] <= fake[0] + fake_w
                       and on_2[1] + on_2[3] <= fake[1] + fake_h - fake_taskbar)
        check(inside_fake, "放大之后窗口仍落在副屏工作区里", repr(on_2))

    # 挪回主屏：缩放比要还原，不能一次换屏就永久变大
    back = place_at(wx + 40, wy + 40)
    check(abs(ov.f_main[1]) == abs(base_font) and ov.min_width == base_min_width,
          "挪回主屏后字号和宽度都还原",
          f"字体 {ov.f_main[1]} min_width={ov.min_width}")
    check(inside_work(back), "挪回主屏后仍在主屏工作区里", repr(back))
finally:
    W.monitor_rect = REAL_MONITOR_RECT
    W.monitor_dpi = REAL_DPI_FN

W.caret_screen_pos = _real_caret
ov.destroy()

print()
print("=" * 74)
passed = sum(1 for ok, _ in results if ok)
print(f"结果：{passed}/{len(results)} 通过")
for ok, label in results:
    if not ok:
        print(f"  未通过：{label}")
print("=" * 74)
sys.exit(0 if passed == len(results) else 1)
