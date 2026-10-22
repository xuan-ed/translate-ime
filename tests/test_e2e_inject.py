# -*- coding: utf-8 -*-
"""真·端到端：独立靶子进程 + 真悬浮窗 + 真 SendInput 注入 + 焦点不被抢。

验证的是普通单元测试测不到的那一段（因为钩子会按设计忽略注入事件）：
  1. Overlay 在真 Tk 主循环里能正常渲染，且**不抢焦点**
  2. `W.send_text` 真的把字送进了**另一个进程**的前台窗口
  3. 整条链路 打字 -> 候选 -> 翻译 -> Tab -> 上屏 在真实焦点下成立

这个测试会短暂弹出一个靶子窗口并在结束后关掉它；需要能在当前桌面会话
抢到前台窗口，抢不到会直接退出码 2。

直接跑：``python tests/test_e2e_inject.py``
"""
import ctypes
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()  # 输出被重定向时切 UTF-8；真控制台保持原样

from t_ime import winapi as W  # noqa: E402
from t_ime.config import load  # noqa: E402
from t_ime.core import ROW_EN, IMECore, TranslateWorker  # noqa: E402
from t_ime.overlay import Overlay  # noqa: E402
from t_ime.pinyin import PinyinEngine, load_or_build_index  # noqa: E402
from t_ime.translator import Translator  # noqa: E402

u32 = ctypes.windll.user32
u32.FindWindowW.restype = ctypes.c_void_p
u32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
u32.GetForegroundWindow.restype = ctypes.c_void_p

TITLE = "T-IME-TARGET"
DUMP = HERE / "_e2e_target_out.txt"

PASS = 0
FAIL = 0
LOGS = []


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        LOGS.append(f"  [ok ] {label}")
    else:
        FAIL += 1
        LOGS.append(f"  [FAIL] {label}  <-- {extra!r}")
    print(LOGS[-1], flush=True)


# ---------------------------------------------------------------------- #
# 1. 起靶子进程
if DUMP.exists():
    DUMP.unlink()
proc = subprocess.Popen(
    [sys.executable, str(HERE / "target_window.py"), str(DUMP)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print(f"[e2e] 靶子进程 pid={proc.pid}，找窗口 {TITLE} …")

hwnd = None
for _ in range(100):
    hwnd = u32.FindWindowW(None, TITLE)
    if hwnd:
        break
    time.sleep(0.1)
if not hwnd:
    print("[e2e] 找不到靶子窗口，放弃")
    proc.kill()
    sys.exit(1)
print(f"[e2e] 靶子窗口 hwnd={hwnd}")

time.sleep(0.6)
u32.SetForegroundWindow(ctypes.c_void_p(hwnd))
time.sleep(0.5)
fg = u32.GetForegroundWindow()
print(f"[e2e] 前台窗口 hwnd={fg}  (靶子={hwnd}, 一致={fg == hwnd})")
if fg != hwnd:
    print("[e2e] 抢不到前台，无法做注入测试（沙箱/桌面会话限制）")
    proc.kill()
    sys.exit(2)

# ---------------------------------------------------------------------- #
# 2. 组装真实组件
cfg, _ = load()
index = load_or_build_index(
    ROOT / cfg["pinyin"]["dict_cache"],
    min_freq=int(cfg["pinyin"]["min_freq"]),
    max_word_len=int(cfg["pinyin"]["max_word_len"]),
    verbose=False,
)
eng = PinyinEngine(index, beam_width=int(cfg["pinyin"]["beam_width"]),
                   max_candidates=int(cfg["pinyin"]["max_candidates"]))
tr = Translator(cfg, verbose=False)
worker = TranslateWorker(tr, debounce=cfg["translate"]["debounce_ms"] / 1000.0, verbose=False)
worker.start()

overlay = Overlay(cfg, verbose=False)
root = overlay.root

sent = []


def committer(text):
    ok = W.send_text(text)
    sent.append((text, ok))
    return ok


core = IMECore(cfg, eng, tr, emit=lambda e: None, committer=committer, verbose=False)

focus_checks = []


def tick():
    core.apply_translation_results()
    overlay.render(core.snapshot())
    focus_checks.append(u32.GetForegroundWindow())
    root.after(16, tick)


# ---------------------------------------------------------------------- #
# 3. 编排
steps = []


def step(delay_ms, fn):
    steps.append((delay_ms, fn))


def press_seq(text, delay=25):
    def fn():
        for ch in text:
            vk = ord(ch.upper())
            core.handle_key(vk, True, False)
            core.handle_key(vk, False, False)
            root.update()
            time.sleep(0.02)
    return fn


def press(vk):
    def fn():
        core.handle_key(vk, True, False)
        core.handle_key(vk, False, False)
    return fn


step(300, lambda: check("悬浮窗初始不可见", overlay.visible is False, overlay.visible))
step(50, press_seq("nihao"))
step(400, lambda: check("打拼音后悬浮窗可见", overlay.visible is True, overlay.visible))
step(10, lambda: check("中文候选首项 == 你好", core.zh[:1] == ["你好"], core.zh[:3]))
step(10, lambda: check(
    "渲染后窗口已 mapped",
    bool(root.winfo_ismapped()), root.winfo_ismapped()))
step(10, lambda: check(
    "悬浮窗没抢走焦点",
    u32.GetForegroundWindow() == hwnd,
    (u32.GetForegroundWindow(), hwnd, overlay._mine())))
step(1000, lambda: check("译文已就绪", bool(core.en), core.en))
step(10, lambda: print(f"[e2e] 英文候选: {core.en}", flush=True))
step(10, press(W.VK_TAB))
step(100, lambda: check("Tab 后活动行是译文", core.active == ROW_EN, core.active))
step(10, lambda: check(
    "Tab 之后焦点仍在靶子窗口",
    u32.GetForegroundWindow() == hwnd,
    (u32.GetForegroundWindow(), hwnd, overlay._mine())))
step(10, lambda: setattr(core, "_en_at_commit", core.en[0] if core.en else None))
step(10, press(W.VK_SPACE))
step(700, lambda: check(
    "上屏后悬浮窗隐藏", overlay.visible is False, overlay.visible))
step(10, lambda: check(
    "注入报告成功", bool(sent) and sent[-1][1] is True, sent))
step(10, lambda: check(
    "注入内容 == 英文候选", bool(sent) and sent[-1][0] == core._en_at_commit,
    (sent, core._en_at_commit)))
step(10, lambda: check(
    "缓冲已清空", core.buffer == "", core.buffer))

finish_holder = {}


def finish():
    # 从靶子进程的落盘文件里确认字真的进了另一个进程的窗口
    time.sleep(0.6)
    got = ""
    for _ in range(30):
        if DUMP.exists():
            got = DUMP.read_text(encoding="utf-8", errors="replace")
            if got.strip():
                break
        time.sleep(0.2)
    print(f"[e2e] 靶子窗口内容 = {got!r}", flush=True)
    check("字真的进了另一个进程的窗口", bool(got.strip()), got)
    check("落进去的就是那句译文", got.strip() == (core._en_at_commit or "").strip(),
          (got, core._en_at_commit))
    bad = [h for h in focus_checks if h != hwnd]
    check(f"全程焦点都没被悬浮窗抢走（{len(focus_checks)} 次采样）", not bad,
          (bad[:6], overlay._mine()))


step(100, finish)
step(100, lambda: root.destroy())


def run(i=0):
    if i >= len(steps):
        return
    delay, fn = steps[i]

    def go():
        try:
            fn()
        except Exception as exc:  # 单步炸了也别卡住整条链
            global FAIL
            FAIL += 1
            print(f"  [FAIL] 步骤 {i} 抛异常: {exc!r}", flush=True)
        run(i + 1)

    root.after(delay, go)


root.after(16, tick)
run()

try:
    root.mainloop()
except Exception as exc:
    print(f"[e2e] mainloop 异常: {exc!r}")

worker.stop()
try:
    proc.kill()
except Exception:
    pass

print()
print(f"  结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
