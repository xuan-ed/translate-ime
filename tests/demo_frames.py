# -*- coding: utf-8 -*-
"""现场演示：真悬浮窗 + 真拼音引擎 + 真翻译 + 真 SendInput 注入，逐步截图。

靶子窗口（「记事本」）跑在**另一个进程**里（tests/target_window.py）——
被测输入法判断「哪个窗口是用户真正在用的」靠的是「它属于别的进程」，
同进程的窗口会被判成自己人，所以演示必须跨进程才真实。

直接跑：python tests/demo_frames.py

注意：它会自动开窗、自动打字，全程约 3 秒，这期间别抢鼠标键盘。
截图需要 Pillow；没装也能跑，只是不存图（状态照常打印）。

别拿它当测试 —— 断言在 tests/test_core.py 和 tests/test_e2e_inject.py 里。
"""
import ctypes
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = ROOT / "docs"
DUMP = HERE / "_demo_target_out.txt"
sys.path.insert(0, str(ROOT))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()  # 输出被重定向时切 UTF-8；真控制台保持原样

# 截图才用 Pillow。它只负责抓图，不是运行输入法的依赖，缺了就降级。
try:
    from PIL import ImageGrab
except ImportError:  # pragma: no cover
    ImageGrab = None

from t_ime import winapi as W  # noqa: E402
from t_ime.config import load  # noqa: E402
from t_ime.core import IMECore, TranslateWorker  # noqa: E402
from t_ime.overlay import Overlay  # noqa: E402
from t_ime.pinyin import PinyinEngine, load_or_build_index  # noqa: E402
from t_ime.translator import Translator  # noqa: E402

OUT.mkdir(exist_ok=True)
cfg, _ = load()
if ImageGrab is None:
    print("[demo] 没找到 Pillow，本次不保存截图（其余流程照常）")


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def window_rect(hwnd):
    r = RECT()
    ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


# ------------------------------------------------- 1. 起跨进程靶子窗口
print("[demo] 启动靶子记事本进程…")
if DUMP.exists():
    DUMP.unlink()
proc = subprocess.Popen(
    [sys.executable, str(ROOT / "tests" / "target_window.py"), str(DUMP)],
    stdout=subprocess.PIPE, text=True, encoding="utf-8",
)
assert proc.stdout.readline().strip() == "ready", "靶子窗口没起来"

TARGET = 0
for _ in range(50):
    TARGET = int(W.user32.FindWindowW(None, "T-IME-TARGET") or 0)
    if TARGET:
        break
    time.sleep(0.1)
assert TARGET, "找不到靶子窗口 T-IME-TARGET"
print(f"[demo] 靶子 hwnd = {TARGET}（pid {W.window_pid(TARGET)}，我们 pid {__import__('os').getpid()}）")


def focus_target(attempts=25, delay=0.12, quiet=True):
    """把前台给靶子窗口，最多试 attempts 次。

    ``SetForegroundWindow`` 是**间歇性**的：系统前台锁定会静默拒绝它（不报错、
    只是不生效），所以必须复核 + 重试。靶子刚起来时常常还没拿到前台，
    这时先抢一次往往就成功了。
    """
    for i in range(attempts):
        if W.foreground_hwnd() == TARGET:
            return True
        if W.set_foreground(TARGET) and W.foreground_hwnd() == TARGET:
            return True
        if not quiet and i < 3:
            print(f"[demo] 抢前台第 {i + 1} 次没成（当前前台 {W.foreground_hwnd()}）")
        time.sleep(delay)
    return W.foreground_hwnd() == TARGET


# ------------------------------------------------- 2. 组件（真家伙）
index = load_or_build_index(
    ROOT / cfg["pinyin"]["dict_cache"],
    min_freq=int(cfg["pinyin"]["min_freq"]),
    max_word_len=int(cfg["pinyin"]["max_word_len"]),
    verbose=False,
)
eng = PinyinEngine(index, beam_width=int(cfg["pinyin"]["beam_width"]),
                   max_candidates=int(cfg["pinyin"]["max_candidates"]))
tr = Translator(cfg, verbose=False)
worker = TranslateWorker(tr, debounce=0.05, verbose=False)
worker.start()

# 先让靶子成为前台，再建悬浮窗 —— 这样 Overlay 记下的「用户真正在用的窗口」
# 就是靶子，跟真实使用时的情形一模一样。
print("[demo] 把前台给靶子窗口 ->", focus_target(quiet=False))
overlay = Overlay(cfg, verbose=True)
root = overlay.root

committed = []


def committer(text):
    ok = W.send_text(text)
    committed.append(text)
    print(f"[demo] 上屏注入 {text!r} -> {'ok' if ok else '失败'}")
    return ok


core = IMECore(cfg, eng, tr, emit=lambda e: None, committer=committer, verbose=False)
core.load_boosts()


# ------------------------------------------------- 3. 抓图与驱动
def sync_render():
    """同步跑一帧渲染，保证抓到的图就是 core 当前的状态。

    不能只靠 pump()：pump 是 after(16) 排队的，同一个回调里刚打完字就抓图
    会抓到上一帧（曾因此拍出「buffer 还是 n」的空候选图）。
    """
    core.apply_translation_results()
    overlay.render(core.snapshot())
    root.update_idletasks()
    root.update()


def save(name, box):
    if ImageGrab is None:
        print(f"[demo] {name}  （跳过抓图：没有 Pillow）")
        return
    img = ImageGrab.grab(bbox=box, all_screens=True)
    img.save(OUT / name)
    print(f"[demo] {name}  {img.size[0]}x{img.size[1]}")


def grab_overlay(name, pad=10):
    sync_render()
    x, y = overlay.root.winfo_rootx(), overlay.root.winfo_rooty()
    w, h = overlay.root.winfo_width(), overlay.root.winfo_height()
    save(name, (x - pad, y - pad, x + w + pad, y + h + pad))


def grab_target(name, pad=10):
    sync_render()
    l, t, r, b = window_rect(TARGET)
    save(name, (l - pad, t - pad, r + pad, b + pad))


def fg_name():
    fg = W.foreground_hwnd()
    if fg == TARGET:
        return f"靶子记事本({fg})"
    if fg in overlay._mine():
        return f"悬浮窗自己({fg})"
    return f"别的窗口({fg})"


def type_str(s):
    for ch in s:
        vk = ord(ch.upper())
        core.handle_key(vk, True, False)
        core.handle_key(vk, False, False)


def hit(vk):
    core.handle_key(vk, True, False)
    core.handle_key(vk, False, False)


steps = []


def step(delay, fn):
    steps.append((delay, fn))


def pump():
    core.apply_translation_results()
    overlay.render(core.snapshot())
    root.after(16, pump)


def dump_text():
    try:
        return DUMP.read_text("utf-8") if DUMP.exists() else ""
    except Exception:
        return ""


# ------------------------------------------------------------------ 剧本
step(400, lambda: (focus_target(), print("[demo] 前台 =", fg_name())))
step(150, lambda: type_str("n"))
step(200, lambda: (type_str("ihao"), grab_overlay("demo-1-typing.png"),
                   print("[demo] 打出 nihao ->", core.zh[:5],
                         "译文:", core.en or "（还在路上）")))
step(700, lambda: (grab_overlay("demo-2-translated.png"),
                   print("[demo] 译文候选 ->", core.en)))
step(150, lambda: (hit(W.VK_TAB), print("[demo] Tab 之后活动行 =", core.active)))
step(250, lambda: grab_overlay("demo-3-tab-en.png"))
step(250, lambda: (focus_target(), print("[demo] 上屏前 前台 =", fg_name()),
                   hit(W.VK_SPACE)))
step(400, lambda: (focus_target(), print("[demo] 上屏后 前台 =", fg_name(),
                                         "靶子窗口内容 =", repr(dump_text()))))
step(400, lambda: (grab_target("demo-4-committed.png"),
                   print("[demo] 上屏内容 =", committed)))
step(100, lambda: root.destroy())


def run(i=0):
    if i >= len(steps):
        return
    delay, fn = steps[i]

    def go():
        try:
            fn()
        except Exception as exc:
            print(f"[demo] 步骤 {i} 出错: {exc!r}")
        run(i + 1)

    root.after(delay, go)


root.after(16, pump)
run()
try:
    root.mainloop()
except Exception as exc:
    print(f"[demo] mainloop: {exc!r}")

worker.stop()
proc.terminate()
print("[demo] done")
