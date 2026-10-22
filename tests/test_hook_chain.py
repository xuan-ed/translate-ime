# -*- coding: utf-8 -*-
"""钩子 → 状态机 → 候选窗：把「真实按键进不进得来」这一段补上。

为什么要单独一个测试：``IMECore.handle_key`` 第一行就是「注入事件一律放行」
（防自激），所以**所有**注入类测试都绕过了钩子，直接调状态机。
于是「装上的低级键盘钩子到底能不能把按键喂进状态机」这一段一直是空白 ——
而用户报的「打字没反应、候选窗根本不出现」正好落在这段上。

做法：装**真的** KeyboardHook，只在钩子回调里把「注入」这个标记摘掉
（于是 SendInput 产生的按键会像真键盘一样从钩子进来），上屏用一个只记不发的
committer（否则提交的文本会被自己钩到，自激）。靶子窗口跑在另一个进程里，
万一有键没被吞掉，也是落进靶子而不是你正在用的窗口。

直接跑：``python tests/test_hook_chain.py``（会弹一个靶子窗口，约 8 秒）
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

fix_console_encoding()

from t_ime import winapi as W  # noqa: E402
from t_ime.config import load  # noqa: E402
from t_ime.core import ROW_EN, IMECore, TranslateWorker  # noqa: E402
from t_ime.overlay import Overlay  # noqa: E402
from t_ime.pinyin import PinyinEngine, load_or_build_index  # noqa: E402
from t_ime.translator import Translator  # noqa: E402

u32 = ctypes.windll.user32
u32.FindWindowW.restype = ctypes.c_void_p
u32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]

TITLE = "T-IME-TARGET"
DUMP = HERE / "_hookchain_target_out.txt"

PASS = 0
FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok ] {label}", flush=True)
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  <-- {extra!r}", flush=True)


# ---------------------------------------------------------------------- #
# 1. 靶子进程：只是为了让没被吞掉的键有个安全的地方落
if DUMP.exists():
    DUMP.unlink()
proc = subprocess.Popen(
    [sys.executable, str(HERE / "target_window.py"), str(DUMP)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
hwnd = None
for _ in range(100):
    hwnd = u32.FindWindowW(None, TITLE)
    if hwnd:
        break
    time.sleep(0.1)
if not hwnd:
    print("[hookchain] 找不到靶子窗口，放弃")
    proc.kill()
    sys.exit(1)
print(f"[hookchain] 靶子 hwnd={hwnd} pid={proc.pid}", flush=True)

time.sleep(0.6)
u32.SetForegroundWindow(ctypes.c_void_p(hwnd))
time.sleep(0.5)
if u32.GetForegroundWindow() != hwnd:
    print("[hookchain] 抢不到前台，无法做注入测试")
    proc.kill()
    sys.exit(2)

# ---------------------------------------------------------------------- #
# 2. 真组件
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

overlay = Overlay(cfg, verbose=False)      # 在靶子已经拿到前台之后再建，_real_fg 才正确
root = overlay.root

committed = []


def committer(text):
    committed.append(text)                  # 只记不发：发了会被自己的钩子钩回来
    return True


core = IMECore(cfg, eng, tr, emit=lambda e: None, committer=committer, verbose=False)

hook_events = []
# 关键的一行：把「注入」标记抹掉，让注入的键走成真实按键。
# 生产里 handle_key 会因为 injected=True 直接放行，所以这里是测试专用的旁路。
def on_key(vk, is_down, injected):
    hook_events.append((vk, is_down))
    return core.handle_key(vk, is_down, False)


hook = W.KeyboardHook(on_key, "t-ime-hookchain")
if not hook.start(timeout=5.0):
    print(f"[hookchain] 钩子装不上：{hook.error}")
    worker.stop()
    proc.kill()
    sys.exit(3)
print("[hookchain] 真钩子已装上", flush=True)

focus_samples = []


def tick():
    core.apply_translation_results()
    overlay.render(core.snapshot())
    focus_samples.append(u32.GetForegroundWindow())
    root.after(16, tick)


LETTERS = {"n": 0x4E, "i": 0x49, "h": 0x48, "a": 0x41, "o": 0x4F}


def type_keys(chars, gap=0.14):
    for ch in chars:
        W.send_vk(LETTERS[ch])
        time.sleep(gap)                     # 让钩子线程把事件处理完


def press(vk):
    W.send_vk(vk)
    time.sleep(0.15)


steps = []


def step(delay_ms, fn):
    steps.append((delay_ms, fn))


step(300, lambda: check("初始悬浮窗不可见", overlay.visible is False, overlay.visible))
step(50, lambda: type_keys("nihao"))
step(250, lambda: check(
    f"真钩子收到了按键（{len(hook_events)} 个事件）", len(hook_events) >= 10, hook_events[:8]))
step(10, lambda: check("按键进了拼音缓冲", core.buffer == "nihao", core.buffer))
step(10, lambda: check("候选窗弹出来了", overlay.visible is True, overlay.visible))
step(10, lambda: check("中文首候选 == 你好", core.zh[:1] == ["你好"], core.zh[:3]))
step(10, lambda: check(
    "候选窗没抢走焦点",
    u32.GetForegroundWindow() == hwnd,
    (u32.GetForegroundWindow(), hwnd, overlay._mine())))
step(10, lambda: check("窗口真的 mapped", bool(root.winfo_ismapped()), root.winfo_ismapped()))
step(1200, lambda: check("译文已就绪", bool(core.en), core.en))
step(10, lambda: print(f"[hookchain] 英文候选 = {core.en}", flush=True))
step(10, lambda: press(W.VK_TAB))
step(250, lambda: check("Tab 后活动行是译文", core.active == ROW_EN, core.active))
step(10, lambda: check(
    "Tab 之后焦点仍在靶子窗口",
    u32.GetForegroundWindow() == hwnd,
    (u32.GetForegroundWindow(), hwnd, overlay._mine())))
step(10, lambda: setattr(core, "_en_at_commit", core.en[0] if core.en else None))
step(10, lambda: press(W.VK_SPACE))
step(400, lambda: check("上屏内容 == 英文候选", committed[-1:] == [core._en_at_commit],
                        (committed, core._en_at_commit)))
step(10, lambda: check("上屏后悬浮窗隐藏", overlay.visible is False, overlay.visible))
step(10, lambda: check("缓冲已清空", core.buffer == "", core.buffer))


def finish():
    time.sleep(0.5)
    got = ""
    if DUMP.exists():
        got = DUMP.read_text(encoding="utf-8", errors="replace")
    print(f"[hookchain] 靶子窗口内容 = {got!r}", flush=True)
    check("一个字母都没漏进靶子（全被输入法吞了）", got.strip() == "", got)
    bad = [h for h in focus_samples if h != hwnd]
    check(f"全程焦点都没被抢走（{len(focus_samples)} 次采样）", not bad,
          (bad[:6], overlay._mine()))


step(100, finish)

# ---------------------------------------------------------------------- #
# 3. 第二阶段：**不走旁路**，用生产里的接法（钩子回调直接就是 core.handle_key）
#    验证「自己回灌的按键要被忽略、别的程序转发的按键要被当成真按键」。
#
#    动机：装了改键器 / 宏 / 软键盘的机器上，手敲的每个键都带 LLKHF_INJECTED。
#    早先的版本一律当「注入」丢掉 → 症状正是**打字完全没反应**。
#    反过来，我们上屏用的 send_text 也是注入，绝不能自激。
core2 = IMECore(cfg, eng, tr, emit=lambda e: None,
                committer=lambda t: True, verbose=False)
hook2_box: list = []


def type_keys_foreign(chars, gap=0.14, extra=0xBEEF):
    for ch in chars:
        W.send_vk(LETTERS[ch], extra_info=extra)   # 冒充「别的程序转发的按键」
        time.sleep(gap)


def install_phase2():
    h2 = W.KeyboardHook(core2.handle_key, "t-ime-hookchain-2")   # 生产接法，不包装
    if not h2.start(timeout=5.0):
        check("第二阶段钩子装上了（回调直接是 handle_key）", False, h2.error)
        return
    hook2_box.append(h2)
    check("第二阶段钩子装上了（回调直接是 handle_key）", True)


def stats2():
    return hook2_box[0].stats() if hook2_box else {}


step(50, lambda: hook.stop())
step(10, lambda: check("旁路钩子已停（避免两个钩子互相干扰）", hook.alive is False, hook.alive))
step(10, install_phase2)
step(200, lambda: type_keys_foreign("ni"))
step(250, lambda: check("别的程序转发的按键 → 真的进了状态机",
                        core2.buffer == "ni", core2.buffer))
step(10, lambda: check("并且算出了中文候选", core2.zh[:1] == ["你"], core2.zh[:3]))
step(10, lambda: check("钩子把它记成 foreign_injected（不是我们自己的）",
                       stats2().get("foreign_injected", 0) >= 4
                       and stats2().get("self_injected", 0) == 0, stats2()))
step(10, lambda: W.send_vk(LETTERS["i"]))        # 默认带我们自己的标记
step(250, lambda: check("我们自己注入的按键 → 被忽略，不会自激",
                        core2.buffer == "ni", core2.buffer))
step(10, lambda: check("它被记成 self_injected",
                       stats2().get("self_injected", 0) >= 2, stats2()))
step(10, lambda: check("effective = 真实 + 别人转发的（自己注入的不算）",
                       stats2().get("effective", 0) >= 4, stats2()))

step(100, lambda: root.destroy())


def run(i=0):
    if i >= len(steps):
        return
    delay, fn = steps[i]

    def go():
        try:
            fn()
        except Exception as exc:
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
    print(f"[hookchain] mainloop 异常: {exc!r}")

hook.stop()
for _h in hook2_box:
    _h.stop()
worker.stop()
try:
    proc.kill()
except Exception:
    pass

print()
print(f"  结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
