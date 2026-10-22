# -*- coding: utf-8 -*-
"""候选键的真机验证：←→↑↓ 选词、空格/回车上屏 —— 以及「该吞的吞了、该放的放了」。

为什么要单独一个测试：README 的特性清单写着「Tab 在两侧切换高亮，←→ 选词，
1-9 直选，空格/回车上屏」，但``tests/test_core.py`` 里此前只有 Tab / 数字键 /
空格三个，**方向键与回车一个都没验过**。而且单元测试调的是
``core.handle_key`` 本身，方向键最要紧的那句承诺 —— 「这个键不会传给目标程序」——
只有在真钩子 + 真注入 + 真前台窗口下才测得出来。

做法：和 ``tests/test_hook_chain.py`` 一样装**真** KeyboardHook，只在回调里把
「注入」标记摘掉（SendInput 的键于是像真键盘一样从钩子进来）；上屏用真的
``W.send_text``（它带我们自己的标记，钩子会忽略，不会自激）。靶子是另一个进程里的
Tk Text（``tests/caret_target.py``），它**汇报光标位置** —— 方向键不改文本，
「文本框住没变」是弱证据，「**光标没动**」才是硬证据。

对照组在同一个测试里：缓冲为空时，空格 / 回车 / 数字 / 左右方向键**必须**到达靶子
（光标真的动、真的换行），挂起（``enabled=False``）时字母也必须到达靶子。
没有这两组，「光标没动」也可能只是靶子压根收不到键。

直接跑：``python tests/test_candidate_keys.py``（会弹一个靶子窗口，约 10 秒）
"""
import ctypes
import json
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
from t_ime.core import ROW_ZH, IMECore, TranslateWorker  # noqa: E402
from t_ime.overlay import Overlay  # noqa: E402
from t_ime.pinyin import PinyinEngine, load_or_build_index  # noqa: E402
from t_ime.translator import Translator  # noqa: E402

u32 = ctypes.windll.user32
u32.FindWindowW.restype = ctypes.c_void_p
u32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
u32.GetForegroundWindow.restype = ctypes.c_void_p

TITLE = "T-IME-CARET"
OUT = HERE / "_caret_target_out.json"
CMD = HERE / "_caret_target_cmd.txt"

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


def read_dump():
    """读靶子的 dump（它每 100ms 写一次）。读不到/写到一半就返回空壳。"""
    for _ in range(5):
        try:
            with open(OUT, encoding="utf-8") as fh:
                got = json.load(fh)
            if isinstance(got, dict) and "insert" in got:
                return got
        except Exception:
            pass
        time.sleep(0.05)
    return {"insert": "<读不到>", "text": "<读不到>"}


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


_cmd_seq = 0


def clear_target():
    """让靶子清空文本、光标回 1.0；返回是否清成功。

    序号必须每次都变：靶子只在「命令内容变了」时才执行。
    **必须等 ``cmd`` 回显**：只等「文本为空」可能是「命令还没落地」的旧状态，
    接着注入的键会被随后落地的 clear 抹掉（第 26 轮那个 flaky 的真身）。
    """
    global _cmd_seq
    _cmd_seq += 1
    want = f"clear {_cmd_seq}"
    try:
        CMD.write_text(want, encoding="utf-8")
    except Exception:
        return False
    return wait_for(lambda: read_dump().get("cmd") == want
                    and read_dump()["text"] == "" and read_dump()["insert"] == "1.0")


# ---------------------------------------------------------------------- #
# 1. 靶子进程
for p in (OUT, CMD):
    if p.exists():
        p.unlink()
proc = subprocess.Popen(
    [sys.executable, str(HERE / "caret_target.py"), str(OUT), str(CMD)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
hwnd = None
for _ in range(100):
    hwnd = u32.FindWindowW(None, TITLE)
    if hwnd:
        break
    time.sleep(0.1)
if not hwnd:
    print("[candkeys] 找不到靶子窗口，放弃")
    proc.kill()
    sys.exit(1)
print(f"[candkeys] 靶子 hwnd={hwnd} pid={proc.pid}", flush=True)

time.sleep(0.6)
u32.SetForegroundWindow(ctypes.c_void_p(hwnd))
time.sleep(0.5)
if u32.GetForegroundWindow() != hwnd:
    print("[candkeys] 抢不到前台，无法做注入测试（沙箱/桌面会话限制）")
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

overlay = Overlay(cfg, verbose=False)      # 靶子拿到前台之后再建
root = overlay.root

# 上屏走真注入：靶子里的文本才算「用户真的看到了字」。
# 它带 T_IME_INJECT_TAG，钩子认得出是自己人，不会自激（test_hook_chain 第二阶段验过）。
sent = []


def committer(text):
    ok = W.send_text(text)
    sent.append((text, ok))
    return ok


core = IMECore(cfg, eng, tr, emit=lambda e: None, committer=committer, verbose=False)

# 本地算一份「nihao 的候选」，用来知道第 2 个候选到底该是什么字。
# 用同一个 eng（同一份词典、同一份自学习权重），所以排序和钩子里的那个一致。
local = IMECore(cfg, eng, tr, emit=lambda e: None, committer=lambda t: True, verbose=False)
for ch in "nihao":
    local.handle_key(ord(ch.upper()), True, False)
    local.handle_key(ord(ch.upper()), False, False)
EXPECT_ZH = list(local.zh)

hook_events = []


def on_key(vk, is_down, injected):
    # 关键的一行：把「注入」标记抹掉，让注入的键走成真实按键
    #（生产里 handle_key 会因为 own_injection=True 直接放行，这里是测试专用旁路）
    hook_events.append((vk, is_down))
    return core.handle_key(vk, is_down, False)


hook = W.KeyboardHook(on_key, "t-ime-candkeys")
if not hook.start(timeout=5.0):
    print(f"[candkeys] 钩子装不上：{hook.error}")
    worker.stop()
    proc.kill()
    sys.exit(3)
print("[candkeys] 真钩子已装上", flush=True)

focus_samples = []


def tick():
    core.apply_translation_results()
    overlay.render(core.snapshot())
    focus_samples.append(u32.GetForegroundWindow())
    root.after(16, tick)


LETTERS = {"n": 0x4E, "i": 0x49, "h": 0x48, "a": 0x41, "o": 0x4F, "b": 0x42}


def type_keys(chars, gap=0.14):
    for ch in chars:
        W.send_vk(LETTERS[ch])
        time.sleep(gap)


def press(vk, wait=0.35):
    W.send_vk(vk)
    time.sleep(wait)          # 让钩子线程把事件处理完、靶子把 dump 刷新出来


# ---------------------------------------------------------------------- #
# 3. 编排
steps = []
zh_len = [0]


def step(delay_ms, fn):
    steps.append((delay_ms, fn))


step(200, lambda: check("靶子起点：文本为空、光标在 1.0",
                        read_dump()["text"] == "" and read_dump()["insert"] == "1.0",
                        read_dump()))
step(10, lambda: check("本地候选表算得出来（第 1 个是「你好」，至少 2 个）",
                       len(EXPECT_ZH) >= 2 and EXPECT_ZH[0] == "你好", EXPECT_ZH[:3]))

# ---- 阶段 A：空缓冲 —— 对照组，这些键**必须**到达靶子 ----
print("[candkeys] 阶段 A：空缓冲时，这些键应该原样落到靶子里", flush=True)
step(10, lambda: press(W.VK_SPACE))
step(10, lambda: check("空缓冲：空格就是空格（放行）",
                       read_dump()["text"] == " " and read_dump()["insert"] == "1.1",
                       read_dump()))
step(10, lambda: press(W.VK_LEFT))
step(10, lambda: check("★对照组：空缓冲时 ← 真的把光标移走了",
                       read_dump()["insert"] == "1.0", read_dump()))
step(10, lambda: press(W.VK_RIGHT))
step(10, lambda: check("对照组：→ 也真的移动光标", read_dump()["insert"] == "1.1", read_dump()))
step(10, lambda: press(W.VK_RETURN))
step(10, lambda: check("空缓冲：回车就是换行（没被吞掉）",
                       read_dump()["text"] == " \n" and read_dump()["insert"] == "2.0",
                       read_dump()))
step(10, lambda: press(0x31))          # 数字 1；winapi 里没给 0x30..0x39 起名字
step(10, lambda: check("空缓冲：数字键就是数字（放行）",
                       read_dump()["text"] == " \n1" and read_dump()["insert"] == "2.1",
                       read_dump()))

# ---- 阶段 B：拼音缓冲非空 —— 该吞的必须吞 ----
print("[candkeys] 阶段 B：缓冲非空，方向键归输入法，不许落到靶子里", flush=True)
step(10, lambda: check("阶段 B 前先把靶子清干净", clear_target(), read_dump()))
step(10, lambda: type_keys("nihao"))
step(300, lambda: check("五个字母全进了拼音缓冲", core.buffer == "nihao", core.buffer))
step(10, lambda: check("而靶子里一个字都没多（字母被吞了）", read_dump()["text"] == "",
                       read_dump()))
step(10, lambda: zh_len.__setitem__(0, len(core.zh)))
step(10, lambda: check("候选算出来了，至少两个", zh_len[0] >= 2, core.zh[:3]))
step(10, lambda: check("初始选中第 1 个", core.sel[ROW_ZH] == 0, core.sel))
step(10, lambda: press(W.VK_LEFT))
step(10, lambda: check("← 在头上：选中夹在 0，没越界也没崩",
                       core.sel[ROW_ZH] == 0, core.sel))
step(10, lambda: press(W.VK_RIGHT))
step(10, lambda: check("→ 把选中挪到第 2 个", core.sel[ROW_ZH] == 1, core.sel))
step(10, lambda: check("★ 靶子里的光标纹丝不动（方向键被输入法吞了）",
                       read_dump()["insert"] == "1.0" and read_dump()["text"] == "",
                       read_dump()))
step(10, lambda: press(W.VK_DOWN))
step(10, lambda: check("↓ 跟 → 一样往后走（到头夹住）",
                       core.sel[ROW_ZH] == min(2, zh_len[0] - 1), (core.sel, zh_len[0])))
step(10, lambda: press(W.VK_UP))
step(10, lambda: check("↑ 跟 ← 一样退回来", core.sel[ROW_ZH] == 1, core.sel))
step(10, lambda: check("折腾这么多下，光标还是 1.0、文本还是空",
                       read_dump()["insert"] == "1.0" and read_dump()["text"] == "",
                       read_dump()))
step(10, lambda: press(W.VK_RETURN, wait=0.5))
step(10, lambda: check("回车把**选中的第 2 个**候选上屏了（不是首选项「你好」）",
                       read_dump()["text"] == EXPECT_ZH[1] and EXPECT_ZH[1] != EXPECT_ZH[0],
                       (read_dump(), EXPECT_ZH[:3])))
step(10, lambda: check("回车没有变成换行（它被输入法用掉了）",
                       "\n" not in read_dump()["text"], read_dump()))
step(10, lambda: check("上屏后拼音缓冲清空", core.buffer == "", core.buffer))
step(10, lambda: check("光标落在上屏文本之后",
                       read_dump()["insert"] == f"1.{len(EXPECT_ZH[1])}", read_dump()))
step(10, lambda: check("上屏走的是真注入，且成功了", sent[-1:] == [(EXPECT_ZH[1], True)], sent))

# ---- 阶段 C：挂起 —— 又一个对照组，字母与方向键都必须落到靶子 ----
print("[candkeys] 阶段 C：挂起（enabled=False）时一切都透传", flush=True)
step(10, lambda: check("阶段 C 前清空靶子", clear_target(), read_dump()))
step(10, lambda: setattr(core, "enabled", False))
step(10, lambda: check("已挂起", core.enabled is False))
step(10, lambda: type_keys("ab"))
step(10, lambda: check("挂起时字母原样进靶子（不再进拼音缓冲）",
                       read_dump()["text"] == "ab" and core.buffer == "",
                       (read_dump(), core.buffer)))
# 方向键也得真的落到靶子上 —— 但**空文本上光标本来就在末尾**，这时候按 → / ←
# 都不动，那是靶子的性质、不是输入法的问题（第一版就在这里误判过一次：
# 光标在 1.0 上按 → 然后断言 1.1，当然失败）。所以先让 "ab" 落进去再看。
step(10, lambda: check("字母落地后光标在 1.2", read_dump()["insert"] == "1.2", read_dump()))
step(10, lambda: press(W.VK_LEFT))
step(10, lambda: check("★对照组：挂起时 ← 直接落进靶子（光标从 1.2 走到 1.1）",
                       read_dump()["insert"] == "1.1", read_dump()))
step(10, lambda: press(W.VK_RIGHT))
step(10, lambda: check("挂起时 → 也落进靶子（光标回到 1.2）",
                       read_dump()["insert"] == "1.2", read_dump()))
step(10, lambda: setattr(core, "enabled", True))
step(10, lambda: check("恢复后开关又开了", core.enabled is True))


def finish():
    time.sleep(0.4)
    stats = hook.stats()
    print(f"[candkeys] 钩子统计 = {stats}", flush=True)
    check(f"钩子自己记下了吞键（swallowed={stats.get('swallowed')}）",
          stats.get("swallowed", 0) >= 8, stats)
    check("钩子确实收到了按键", len(hook_events) >= 20, len(hook_events))
    bad = [h for h in focus_samples if h != hwnd]
    check(f"全程焦点都在靶子上（{len(focus_samples)} 次采样，候选窗没抢）",
          not bad, (bad[:6], overlay._mine()))
    hook.stop()
    check("钩子已停", hook.alive is False)
    worker.stop()
    check("收工：靶子里的文本 == 挂起阶段留下的 ab（没有别的漏进去）",
          read_dump()["text"] == "ab", read_dump())
    print(f"[candkeys] 结果：{PASS} 通过 / {FAIL} 失败", flush=True)
    root.destroy()


step(200, finish)


def run(i=0):
    if i >= len(steps):
        return
    delay, fn = steps[i]

    def go():
        # 步骤里抛异常绝不能只让 Tk 打印一行就停在这儿 ——
        # 那样 mainloop 会一直转，看起来像「测试卡住了」，第一次跑就吃过这个亏。
        try:
            fn()
        except Exception:
            import traceback
            check(f"步骤 {i} 抛异常（后面不再执行）", False, traceback.format_exc()[-400:])
            print(traceback.format_exc(), flush=True)
        run(i + 1)

    root.after(delay, go)


root.after(16, tick)
root.after(90000, root.destroy)          # 看门狗：无论如何 90 秒后收工，不许挂住
run()

try:
    root.mainloop()
finally:
    try:
        hook.stop()
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    for p in (OUT, CMD):
        try:
            p.unlink()
        except Exception:
            pass

sys.exit(1 if FAIL else 0)
