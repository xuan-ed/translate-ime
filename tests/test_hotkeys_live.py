# -*- coding: utf-8 -*-
"""热键的真机验证：``Ctrl+Alt+Space`` / ``Ctrl+Alt+L`` / ``Ctrl+Alt+Z`` / ``Ctrl+Alt+Q``。

为什么要单独一个测试：README 的特性清单里写着「一键直译上屏 —— ``Ctrl+Alt+Space``
跳过选词，直接把最佳译文注入目标窗口」，可**这四个组合键从来没有被真正按下过**：
``tests/test_core.py`` 里的热键用例是直接调 ``core.handle_key``，第 22 轮验挂起 /
换语言走的是命令文件，第 20 轮验的是 Shift 单击。热键恰好是**没有托盘时唯一还能用的
入口**（第 22、24 轮已证明托盘和开机自启在本会话里都验不了），所以它值得一个真机测试：

  * 真的按下修饰键（注入 Ctrl 与 Alt 的**按下**、隔一会儿再抬起），
  * 真的走全局钩子进来（回调里把「注入」标记抹掉，跟 ``test_hook_chain`` 一样），
  * 上屏走真的 ``W.send_text``，字真的落到**另一个进程**的文本框里。

覆盖的坑（每条都是真机才看得出来的）：

  1. 主键必须**被吞**、修饰键必须**放行** —— 吞多了 Alt+Tab 就没了，吞少了
     ``Ctrl+Alt+L`` 会往文档里插一个 ``l``。
  2. 「只按 ``Ctrl`` 再按 ``L``」**不能**触发：热键是「集合包含」而不是「按下顺序」。
  3. 空缓冲按直译上屏只能给提示，不能往目标窗口塞一个空格。
  4. 有缓冲时最佳译文要**原样**落进目标窗口，拼音缓冲清空。
  5. ``Ctrl+Alt+Z`` 挂起之后，字母必须真的透传到目标窗口（第 25 轮的「挂起」是
     直接改 ``core.enabled``，热键那一段没验过）。
  6. 键盘**连发**（按住不放时系统反复补「按下」）只能算一次 —— 不然按住
     ``Ctrl+Alt+Z`` 半秒，开关就抖成一团。
  7. 钩子**装得比修饰键晚**时（用户先按着 Ctrl+Alt，钩子才起来），要靠
     ``GetAsyncKeyState`` 兜底 —— 这条此前只用假 ``mods_source`` 在单元里测过。
  8. ``Ctrl+Alt+Q`` 要发出 ``EV_QUIT``（常驻进程就靠它收摊）。

直接跑：``python tests/test_hotkeys_live.py``（会弹一个靶子窗口，约 20 秒）
"""
import ctypes
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

from t_ime import winapi as W  # noqa: E402
from t_ime.config import load  # noqa: E402
from t_ime.core import EV_QUIT, ROW_EN, ROW_ZH, IMECore, TranslateWorker  # noqa: E402
from t_ime.overlay import Overlay  # noqa: E402
from t_ime.pinyin import PinyinEngine, load_or_build_index  # noqa: E402
from t_ime.translator import Translator  # noqa: E402

u32 = ctypes.windll.user32
u32.FindWindowW.restype = ctypes.c_void_p
u32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
u32.GetForegroundWindow.restype = ctypes.c_void_p

TITLE = "T-IME-CARET"
OUT = HERE / "_hotkeys_target_out.json"
CMD = HERE / "_hotkeys_target_cmd.txt"

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
    """同步等一个条件（简单轮询）。**不要在 Tk 回调里用它等翻译结果**：
    ``core.apply_translation_results()`` 是在 ``tick()`` 里被调用的，而 ``tick``
    得靠 mainloop 跑 —— 在这里死等会把 mainloop 一起堵住，译文永远回不来
    （第 26 轮第一次跑就是这么假失败了一条）。等翻译请用 ``pump_wait``。
    """
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def pump_wait(pred, timeout=8.0):
    """一边 ``root.update()`` 一边等 —— 让 ``tick()`` 继续跑（译文才回得来）。

    这是「在步骤里等异步结果」的正确姿势：不能阻塞 mainloop，也不能只 sleep
    （sleep 期间 ``root.after`` 的回调一个都不会执行）。
    """
    end = time.time() + timeout
    while time.time() < end:
        try:
            root.update()
        except Exception:
            pass
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(0.03)
    return False


def wait_text(want, timeout=2.5):
    """等靶子里的文本变成 ``want``。

    靶子每 100ms 才把文本写进文件一次，读早了会读到**上一次**的样子 ——
    所以「注入完立刻读一次」这种断言会随机失败（第 26 轮真机跑吃过这个亏）。
    """
    ok = pump_wait(lambda: read_dump()["text"] == want, timeout=timeout)
    return ok


def wait_insert(want, timeout=2.5):
    """等靶子里的光标位置变成 ``want``（理由同上）。"""
    return pump_wait(lambda: read_dump()["insert"] == want, timeout=timeout)


_cmd_seq = 0


def clear_target():
    """让靶子清空文本、光标回 1.0；返回是否清成功。

    序号必须每次都变：靶子只在「命令内容变了」时才执行。
    **必须等 ``cmd`` 回显**：靶子在 dump 里写「最后执行的命令」，只等「文本为空」
    是不够的 —— 那可能只是「命令还没落地」的旧状态，接着注入的键会被随后落地的
    clear 抹掉（第 26 轮那个时好时坏的 flaky 的真身）。
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
    print("[hotkeys] 找不到靶子窗口，放弃")
    proc.kill()
    sys.exit(1)
print(f"[hotkeys] 靶子 hwnd={hwnd} pid={proc.pid}", flush=True)

time.sleep(0.6)
u32.SetForegroundWindow(ctypes.c_void_p(hwnd))
time.sleep(0.5)
if u32.GetForegroundWindow() != hwnd:
    print("[hotkeys] 抢不到前台，无法做注入测试（沙箱/桌面会话限制）")
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
events = []


def committer(text):
    ok = W.send_text(text)
    sent.append((text, ok))
    return ok


core = IMECore(cfg, eng, tr, emit=events.append, committer=committer, verbose=False)

# 配置里的热键 —— 不写死字符串，按 config.json 解析（解析器本身也随之被测到）
HK = {}
for _name in ("compose", "toggle_default", "toggle_enabled", "quit"):
    _spec = (cfg.get("hotkeys") or {}).get(_name)
    if _spec:
        HK[_name] = W.parse_hotkey(_spec)


def spec_of(name):
    """返回 (修饰键元组, 主键)。"""
    mods, main = HK[name]
    return tuple(sorted(mods)), main


hook_log = []
hook_seen = [0]          # 累计计数：hook_log 每个阶段都会被 clear，收工时看这个


def on_key(vk, is_down, injected):
    """把钩子看到的事件交给状态机。

    ``injected`` **要原样传下去**：本测试注入的键用 ``extra_info=0``（钩子眼里是
    「别的程序转发的」，``injected=False``），状态机就会当成真实按键 —— 这正是我们
    要验的路径。而**产品自己**补的那个无害 Shift（挡系统菜单用的）带
    ``T_IME_INJECT_TAG``，``injected=True``，必须跟生产一样被忽略；否则它会冒充
    「单击 Shift」把输入法切进英文直通（第 26 轮真机跑出来过：阶段 A 一按热键，
    后面整场都在直通模式里）。
    """
    got = core.handle_key(vk, is_down, injected)
    hook_log.append((W.normalize_vk(vk), is_down, bool(got)))
    hook_seen[0] += 1
    return got


hook = W.KeyboardHook(on_key, "t-ime-hotkeys")
if not hook.start(timeout=5.0):
    print(f"[hotkeys] 钩子装不上：{hook.error}")
    worker.stop()
    proc.kill()
    sys.exit(3)
print("[hotkeys] 真钩子已装上", flush=True)

focus_samples = []


def tick():
    core.apply_translation_results()
    overlay.render(core.snapshot())
    focus_samples.append(u32.GetForegroundWindow())
    root.after(16, tick)


# ---------------------------------------------------------------------- #
# 3. 注入：这次要「按住修饰键」，所以不能用 send_vk（它把按下和抬起放在同一批）
def key_down(vk, extended=False):
    scan = W.user32.MapVirtualKeyW(int(vk), W.MAPVK_VK_TO_VSC)
    flags = W.KEYEVENTF_EXTENDEDKEY if extended else 0
    # extra_info=0：仿真「别的程序转发的按键」（改键器/宏）—— 钩子眼里这就是真键盘，
    # 产品会照常处理。**不能**用默认的 T_IME_INJECT_TAG：那是「自己回灌」的标记，
    # 生产会直接忽略（早期版本就是这么写错的，见 README 踩坑第 26 条）。
    return W._send([W._kb_input(int(vk), scan, flags, 0)])


def key_up(vk, extended=False):
    scan = W.user32.MapVirtualKeyW(int(vk), W.MAPVK_VK_TO_VSC)
    flags = (W.KEYEVENTF_EXTENDEDKEY if extended else 0) | W.KEYEVENTF_KEYUP
    return W._send([W._kb_input(int(vk), scan, flags, 0)])


def press_combo(mods, main, hold=0.08, gap=0.09, tail=0.35):
    """真的按住修饰键，再按一下主键，再松开。"""
    hook_log.clear()
    for m in mods:
        key_down(m)
    time.sleep(gap)
    key_down(main)
    time.sleep(hold)
    key_up(main)
    time.sleep(gap)
    for m in reversed(list(mods)):
        key_up(m)
    time.sleep(tail)


def repeat_combo(mods, main, times=3, gap=0.08, tail=0.35):
    """按住不放：系统键盘连发的样子（只有按下、没有中间的抬起）。"""
    hook_log.clear()
    for m in mods:
        key_down(m)
    time.sleep(gap)
    for _ in range(times):
        key_down(main)
        time.sleep(gap)
    key_up(main)
    time.sleep(gap)
    for m in reversed(list(mods)):
        key_up(m)
    time.sleep(tail)


def restart_hook():
    """停掉再装上（模拟「用户先按着 Ctrl+Alt，钩子才起来」）。"""
    hook.start(timeout=5.0)
    # _ready 事件在第一次启动时就置位了，新线程真正装好钩子还要几毫秒
    time.sleep(0.35)


def seen(vk, is_down):
    return [e for e in hook_log if e[0] == vk and e[1] is is_down]


def swallowed(vk, is_down):
    return any(e[2] for e in seen(vk, is_down))


LETTERS = {"n": 0x4E, "i": 0x49, "h": 0x48, "a": 0x41, "o": 0x4F, "b": 0x42}


def tap_vk(vk, hold=0.03, tail=0.30):
    """按一下松开。

    **必须不带自家标记**（``extra_info=0``）：带 ``T_IME_INJECT_TAG`` 的注入在生产里
    会被 ``handle_key`` 当成「自己回灌的」直接放行 —— 我们要验的是「用户真敲了这个键」，
    所以用 ``key_down``/``key_up`` 这套不带标记的注入（钩子眼里它跟真键盘一模一样）。
    """
    key_down(vk)
    time.sleep(hold)
    key_up(vk)
    time.sleep(tail)


def type_keys(chars, gap=0.10):
    for ch in chars:
        tap_vk(LETTERS[ch], hold=0.02, tail=gap)


def toast():
    return core.snapshot().get("toast") or ""


def state():
    """诊断用：状态机此刻的关键状态（断言失败时最想知道的就是这几个）。"""
    return {"enabled": core.enabled, "direct": core.direct, "buf": core.buffer,
            "down": sorted(core._down), "mods": sorted(W.physical_mods()),
            "toast": toast()}


class _GTI(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("flags", ctypes.c_ulong),
                ("hwndActive", ctypes.c_void_p), ("hwndFocus", ctypes.c_void_p),
                ("hwndCapture", ctypes.c_void_p), ("hwndMenuOwner", ctypes.c_void_p),
                ("hwndMoveSize", ctypes.c_void_p), ("hwndCaret", ctypes.c_void_p),
                ("rcCaret", ctypes.c_long * 4)]


def gti_now():
    """前台窗口线程的 Win32 焦点/活动窗口/菜单所有者 —— 键盘到底送给谁了。"""
    fg = u32.GetForegroundWindow()
    if not fg:
        return None
    info = _GTI()
    info.cbSize = ctypes.sizeof(_GTI)
    tid = u32.GetWindowThreadProcessId(ctypes.c_void_p(fg), None)
    if not u32.GetGUIThreadInfo(tid, ctypes.byref(info)):
        return "?"
    return {"active": int(info.hwndActive or 0), "focus": int(info.hwndFocus or 0),
            "menu": int(info.hwndMenuOwner or 0)}


_gti_before = []


def _tap_from_thread(vk):
    """从**另一个线程**敲键，而不是从 Tk 回调里敲。

    真机上「用户的下一个键」是从硬件进来的，不会发生在我们 Tk 回调的上下文里，
    所以这里刻意换个线程敲（第 26 轮实测：这条路和主线程注入一样送得到，
    四种注入方式的对照见 ``space_paths``）。
    """
    th = threading.Thread(target=lambda: (tap_vk(vk), None)[1], daemon=True)
    th.start()
    th.join(timeout=2.0)


def next_key_probe():
    """敲一个空格看它到底进不进得去；第一次没进去就再敲一次并抓现场。

    区分两种可能：系统菜单只吃掉「一个」键（第二次能进去），还是这个前台窗口
    从此不再收注入（第二次也进不去）。
    """
    _tap_from_thread(W.VK_SPACE)
    if wait_text(" ", timeout=1.5):
        return True, {"first": True}
    snap = {"first": False, "menu": W.menu_owner_hwnd(),
            "fg": int(u32.GetForegroundWindow() or 0),
            "state": state(), "text": read_dump(),
            "gti_before": _gti_before[:1], "gti_now": gti_now(),
            "space_events": [e for e in hook_log if e[0] == W.VK_SPACE],
            "stats": {k: hook.stats()[k] for k in ("effective", "swallowed")}}
    clear_target()
    _tap_from_thread(W.VK_SPACE)
    snap["second"] = wait_text(" ", timeout=1.5)
    snap["text_after_second"] = read_dump()
    snap["no_swallow_lands"] = _swallow_control()
    return snap["second"], snap


def _swallow_control():
    """对照：同样按一遍组合键，但这一次热键不生效（主键不被吞、也没有 defuse）。

    用来分辨「是吞键/defuse 这条路径把窗口弄坏了」还是「注入 Ctrl+Alt 本身就会」。
    """
    saved = dict(core.hotkeys)
    core.hotkeys = {}
    try:
        press_combo((W.VK_CONTROL, W.VK_MENU), 0x4C)
        clear_target()
        tap_vk(W.VK_SPACE)
        return wait_text(" ", timeout=1.5)
    finally:
        core.hotkeys = saved


# ---------------------------------------------------------------------- #
# 4. 编排
steps = []
expect_en = []          # 按下直译上屏之前抓住的译文候选（上屏后 core.en 会被清空）
_probe_result = []      # 阶段 A 那条「下一个键」探针的结果（失败时要看现场）


def step(delay_ms, fn):
    steps.append((delay_ms, fn))


# ---- 阶段 0：起点 ----
step(200, lambda: check("靶子起点：文本为空、光标在 1.0",
                        read_dump()["text"] == "" and read_dump()["insert"] == "1.0",
                        read_dump()))
step(10, lambda: check("配置里的四个热键就是 README 写的那四个",
                       sorted(HK) == ["compose", "quit", "toggle_default", "toggle_enabled"]
                       and spec_of("compose") == ((W.VK_CONTROL, W.VK_MENU), 0x20)
                       and spec_of("toggle_default") == ((W.VK_CONTROL, W.VK_MENU), 0x4C)
                       and spec_of("toggle_enabled") == ((W.VK_CONTROL, W.VK_MENU), 0x5A)
                       and spec_of("quit") == ((W.VK_CONTROL, W.VK_MENU), 0x51),
                       {k: spec_of(k) for k in HK}))
step(10, lambda: check("起点：输入法开着、默认上屏行是中文",
                       core.enabled is True and core.active == ROW_ZH,
                       (core.enabled, core.active)))
step(10, lambda: check("起点：没在英文直通里（阶段 A 下面要盯住这一点）",
                       core.direct is False, core.direct))
step(10, lambda: check("起点：_down 里没有修饰键（阶段 G 要靠这一点）",
                       not (core._down & {W.VK_CONTROL, W.VK_MENU}), set(core._down)))

# ---- 阶段 A：Ctrl+Alt+L —— 换默认上屏语言 ----
print("[hotkeys] 阶段 A：Ctrl+Alt+L 换默认上屏语言", flush=True)
def wait_receptive(timeout=15.0):
    """等靶子窗口真的开始收键，返回 ``(等了多少秒, 四路注入结果)``；等不到返回 ``(None, {})``。

    为什么要先握手：靶子进程刚起来的一小会儿还没准备好收键，而它每 100ms 才写一次
    dump —— 不握手的话，「键到底有没有进去」这件事会混进后面每一条断言里，失败现场
    还长得一模一样（第 26 轮在这上面绕了很久）。

    握手之后顺手把四种注入路径各试一遍（:func:`space_paths`）：这样能把
    「注入方式本身送不到」和「产品的处理有问题」分开 —— 前者是测试环境的锅。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        clear_target()
        W.send_text(".")
        time.sleep(0.25)
        if read_dump()["text"] == ".":
            clear_target()
            break
    else:
        return None, {}
    return round(time.time() - t0, 2), space_paths()


def space_paths():
    """握手之后，同一时刻把「空格」的几种注入方式各试一遍。"""
    out = {}
    for name, fn in (("unicode字符", lambda: W.send_text(".")),
                     ("空格-不带标记-主线程", lambda: tap_vk(W.VK_SPACE)),
                     ("空格-带自家标记", lambda: W.send_vk(W.VK_SPACE)),
                     ("空格-不带标记-子线程", lambda: _tap_from_thread(W.VK_SPACE))):
        clear_target()
        fn()
        time.sleep(0.35)
        got = read_dump()
        out[name] = got["text"]
    clear_target()
    return out


step(10, lambda: check("阶段 A 前清空靶子", clear_target(), read_dump()))
# 对照组：还没按任何热键的时候，空格必须能进靶子。不然下面那条「下一个键」如果
# 失败，就只是「靶子压根收不到空格」，跟系统菜单没关系。
step(10, lambda: (_tap_from_thread(W.VK_SPACE), time.sleep(0.4)))
step(10, lambda: check("对照组：按热键之前，空格能进靶子",
                       read_dump()["text"] == " ", read_dump()))
step(10, lambda: check("对照组之后清空靶子", clear_target(), read_dump()))
step(10, lambda: _gti_before.append(gti_now()))
step(10, lambda: press_combo(*spec_of("toggle_default")))
step(10, lambda: check("★ 真组合 Ctrl+Alt+L：默认上屏行换成了译文",
                       core.active == ROW_EN, core.active))
step(10, lambda: check("气泡跟着换（含「空格上屏：译文」）", "译文" in toast(), toast()))
step(10, lambda: check("主键 L 的**按下**被吞了（目标窗口收不到）",
                       swallowed(0x4C, True), hook_log))
step(10, lambda: check("主键 L 的**抬起**也被吞了（不能只吞一半）",
                       swallowed(0x4C, False), hook_log))
step(10, lambda: check("修饰键 Ctrl/Alt 自己没被吞（不然 Alt+Tab 那种组合就废了）",
                       seen(W.VK_CONTROL, True) and seen(W.VK_MENU, True)
                       and not any(e[2] for e in hook_log if e[0] in (W.VK_CONTROL, W.VK_MENU)),
                       hook_log[:8]))
step(10, lambda: check("靶子里一个字都没多", read_dump()["text"] == "", read_dump()))
# 第 26 轮真机挖出来的 bug 的回归断言：主键被钩子吞掉之后，系统看到的只是
# 「Alt 自己松开了」，于是它激活前台窗口的系统菜单 —— 用户按完热键紧跟着敲的
# 下一个键会被菜单吃掉。拿「空格」当探针（缓冲是空的，它本来就该透传给靶子）：
# 它必须真的变成靶子里的一个空格。
step(10, lambda: check("阶段 A 中清空靶子（给菜单探针腾地方）", clear_target(), read_dump()))
# 用 wait_text 而不是读一次：靶子每 100ms 才写一次 dump，而 defuse 是丢在子线程里做的，
# 快慢会飘 —— 「被吃掉」的键是**永远**不会来的，所以等 1.5 秒并不放水。
step(10, lambda: _probe_result.append(next_key_probe()))
step(10, lambda: check("★ 热键按完，紧接着的下一个键没被吃掉（系统菜单没被弹开）",
                       _probe_result[0][0], _probe_result[0][1]))
step(10, lambda: check("★ 补的那个无害 Shift 没把「单击 Shift 切直通」带偏",
                       core.direct is False, core.direct))
step(10, lambda: check("阶段 A 之后清空靶子", clear_target(), read_dump()))
step(10, lambda: press_combo(*spec_of("toggle_default")))
step(10, lambda: check("再按一次换回中文", core.active == ROW_ZH, core.active))
step(10, lambda: check("气泡也换回来（含「空格上屏：中文」）", "中文" in toast(), toast()))

# ---- 阶段 B：只按 Ctrl+L —— 不许触发 ----
print("[hotkeys] 阶段 B：少一个修饰键就不算热键", flush=True)
step(10, lambda: check("阶段 B 前清空靶子", clear_target(), read_dump()))
step(10, lambda: press_combo((W.VK_CONTROL,), 0x4C))
step(10, lambda: check("★ 只按 Ctrl+L：不触发（热键是「集合包含」，不是「按下顺序」）",
                       core.active == ROW_ZH, core.active))
step(10, lambda: check("而且这个 L 没被吞（放给目标窗口了）",
                       seen(0x4C, True) and not swallowed(0x4C, True), hook_log))

# ---- 阶段 C：Ctrl+Alt+Space，缓冲是空的 ----
print("[hotkeys] 阶段 C：空缓冲按直译上屏，只能给提示", flush=True)
step(10, lambda: check("阶段 C 前清空靶子", clear_target(), read_dump()))
step(10, lambda: press_combo(*spec_of("compose")))
step(10, lambda: check("★ 空缓冲按 Ctrl+Alt+Space：给一句提示，不上屏",
                       core.status == "先打几个拼音，再按直译上屏", core.status))
step(10, lambda: check("Space 被吞了（没在靶子里变成一个空格）",
                       read_dump()["text"] == "", read_dump()))
step(10, lambda: check("也真的没有注入任何东西", sent == [], sent))

# ---- 阶段 D：Ctrl+Alt+Space，缓冲里有拼音 ----
print("[hotkeys] 阶段 D：一键直译上屏（真注入到另一个进程）", flush=True)
step(10, lambda: check("阶段 D 前清空靶子", clear_target(), read_dump()))
step(10, lambda: type_keys("nihao"))
step(10, lambda: check("五个字母全进了拼音缓冲、靶子还是空的",
                       core.buffer == "nihao" and read_dump()["text"] == "",
                       (core.buffer, read_dump(), state())))
step(10, lambda: check("译文回来了（mock 词表：你好 -> Hello / Hi there / Greetings）",
                       # 必须 pump_wait：apply_translation_results 是 tick() 在跑，
                       # 在这里死等会把 mainloop 堵住，译文永远回不来
                       pump_wait(lambda: bool(core.en), timeout=8.0) and core.en[0] == "Hello",
                       core.en[:3]))
step(10, lambda: expect_en.__setitem__(slice(None), list(core.en)))
step(10, lambda: press_combo(*spec_of("compose"), hold=0.10))
step(10, lambda: check("★ 一键直译：最佳译文**原样**落进目标窗口",
                       bool(expect_en) and wait_text(expect_en[0]),
                       (read_dump(), expect_en[:2])))
step(10, lambda: check("光标落在译文之后",
                       bool(expect_en)
                       and wait_insert(f"1.{len(expect_en[0])}"),
                       read_dump()))
step(10, lambda: check("拼音缓冲清空", core.buffer == "", core.buffer))
step(10, lambda: check("上屏走的是真注入且成功了", sent[-1:] == [(expect_en[0], True)], sent[-2:]))
step(10, lambda: check("候选窗收起了", overlay.visible is False, overlay.visible))

# ---- 阶段 E：Ctrl+Alt+Z —— 真热键挂起 ----
print("[hotkeys] 阶段 E：Ctrl+Alt+Z 挂起（热键那一段此前没验过）", flush=True)
step(10, lambda: check("挂起前：开着", core.enabled is True, core.enabled))
step(10, lambda: press_combo(*spec_of("toggle_enabled")))
step(10, lambda: check("★ 真组合 Ctrl+Alt+Z 把输入法挂起了", core.enabled is False, core.enabled))
step(10, lambda: check("气泡说清楚了（含「挂起」）", "挂起" in toast(), toast()))
step(10, lambda: check("主键 Z 的按下与抬起都被吞了",
                       swallowed(0x5A, True) and swallowed(0x5A, False), hook_log))
step(10, lambda: check("阶段 E 前清空靶子", clear_target(), read_dump()))
step(10, lambda: type_keys("ab"))
step(10, lambda: check("★ 挂起后字母**原样落进靶子**（透传是真的，不只是回调返回 False）",
                       wait_text("ab") and core.buffer == "",
                       (read_dump(), core.buffer, state())))
step(10, lambda: press_combo(*spec_of("toggle_enabled")))
step(10, lambda: check("再按一次恢复", core.enabled is True, core.enabled))
step(10, lambda: check("气泡也换了（含「开启」）", "开启" in toast(), toast()))
step(10, lambda: check("阶段 E 后清空靶子", clear_target(), read_dump()))
step(10, lambda: type_keys("ni"))


def restore_letters_ok():
    """恢复之后字母要回拼音缓冲，靶子里一个字都不该有。"""
    if not pump_wait(lambda: core.buffer == "ni", timeout=2.0):
        return False
    time.sleep(0.4)          # 多等一会儿：字母真漏进靶子的话，这时就该露出来了
    return read_dump()["text"] == ""


step(10, lambda: check("恢复后字母又回拼音缓冲、靶子里没字",
                       restore_letters_ok(),
                       (core.buffer, read_dump(), state())))

# ---- 阶段 F：键盘连发（按住不放）只能算一次 ----
print("[hotkeys] 阶段 F：按住不放时的连发不能反复触发", flush=True)
step(10, lambda: tap_vk(W.VK_ESCAPE))
step(10, lambda: check("Esc 清掉了拼音缓冲", core.buffer == "", core.buffer))
step(10, lambda: check("阶段 F 起点：默认行是中文", core.active == ROW_ZH, core.active))
step(10, lambda: repeat_combo(*spec_of("toggle_default"), times=3))
step(10, lambda: check("★ 连发 3 次按下只算一次：默认行只换了一格（不然按住半秒就抖成一团）",
                       core.active == ROW_EN, core.active))
step(10, lambda: check(f"连发的按下真的到了钩子（{len(seen(0x4C, True))} 次按下，"
                       "不然上一条就是空过）",
                       len(seen(0x4C, True)) >= 2, hook_log))
step(10, lambda: press_combo(*spec_of("toggle_default")))
step(10, lambda: check("松手后再按一次，仍然能正常切回中文", core.active == ROW_ZH, core.active))

# ---- 阶段 G：兜底 —— 钩子起得比修饰键晚 ----
print("[hotkeys] 阶段 G：用户先按着 Ctrl+Alt，钩子才起来", flush=True)
step(10, lambda: hook.stop())
step(10, lambda: check("钩子已停（接下来按住的修饰键，没人看得见）", hook.alive is False))
step(10, lambda: key_down(W.VK_CONTROL))
step(10, lambda: key_down(W.VK_MENU))
step(10, lambda: check("★ 物理状态真的按着 Ctrl+Alt（GetAsyncKeyState 读得到）",
                       W.VK_CONTROL in W.physical_mods() and W.VK_MENU in W.physical_mods(),
                       W.physical_mods()))
step(10, lambda: restart_hook())
step(10, lambda: check("钩子重新装上并自检通过", hook.alive and hook.probe(timeout=0.5),
                       hook.stats()))
step(10, lambda: check("core 的 _down 里**没有** Ctrl/Alt（模拟「装钩子时键已经按着」）",
                       not (core._down & {W.VK_CONTROL, W.VK_MENU}), set(core._down)))
step(10, lambda: check("阶段 G 起点：默认行是中文", core.active == ROW_ZH, core.active))


def fallback_press():
    hook_log.clear()
    key_down(0x4C)
    time.sleep(0.09)
    key_up(0x4C)
    time.sleep(0.35)


step(10, fallback_press)
step(10, lambda: check("★ 兜底生效：事件流里没有 Ctrl/Alt，靠物理按键状态也认得这个热键",
                       core.active == ROW_EN, (core.active, W.physical_mods())))
step(10, lambda: key_up(W.VK_MENU))
step(10, lambda: key_up(W.VK_CONTROL))
step(10, lambda: check("松开之后 _down 里依然没有修饰键残留",
                       not (core._down & {W.VK_CONTROL, W.VK_MENU}), set(core._down)))

# ---- 阶段 H：Ctrl+Alt+Q —— 退出（放最后） ----
print("[hotkeys] 阶段 H：Ctrl+Alt+Q 退出", flush=True)
step(10, lambda: check("退出前：还没人要求退出", core.quit_requested is False))
step(10, lambda: press_combo(*spec_of("quit")))
step(10, lambda: check("★ 真组合 Ctrl+Alt+Q 触发了退出", core.quit_requested is True,
                       core.quit_requested))
step(10, lambda: check("并且发出了 EV_QUIT 事件（常驻进程就靠它收摊）",
                       any(e.get("type") == EV_QUIT for e in events), events[-3:]))
step(10, lambda: check("主键 Q 的按下与抬起都被吞了",
                       swallowed(0x51, True) and swallowed(0x51, False), hook_log))
step(10, lambda: check("靶子里没多字", read_dump()["text"] == "", read_dump()))


def finish():
    time.sleep(0.4)
    stats = hook.stats()
    print(f"[hotkeys] 钩子统计 = {stats}", flush=True)
    check(f"钩子自己记下了吞键（swallowed={stats.get('swallowed')}）",
          stats.get("swallowed", 0) >= 20, stats)
    check(f"钩子确实收到了按键（累计 {hook_seen[0]} 条）", hook_seen[0] >= 20, hook_seen[0])
    bad = [h for h in focus_samples if h != hwnd]
    check(f"全程焦点都在靶子上（{len(focus_samples)} 次采样，注入 Ctrl/Alt 没把窗口顶走）",
          not bad, (bad[:6], overlay._mine()))
    hook.stop()
    check("钩子已停", hook.alive is False)
    worker.stop()
    check("收工：靶子里没有漏进来的字", read_dump()["text"] == "", read_dump())
    print(f"[hotkeys] 结果：{PASS} 通过 / {FAIL} 失败", flush=True)
    root.destroy()


step(200, finish)


def run(i=0):
    if i >= len(steps):
        return
    delay, fn = steps[i]

    def go():
        # 步骤里抛异常绝不能只让 Tk 打印一行就停在这儿 ——
        # 那样 mainloop 会一直转，看起来像「测试卡住了」，第 25 轮吃过这个亏。
        try:
            fn()
        except Exception:
            import traceback
            check(f"步骤 {i} 抛异常（后面不再执行）", False, traceback.format_exc()[-400:])
            print(traceback.format_exc(), flush=True)
        run(i + 1)

    root.after(delay, go)


root.after(16, tick)
root.after(120000, root.destroy)         # 看门狗：无论如何 120 秒后收工，不许挂住
_receptive, _space_paths = wait_receptive()
print(f"[hotkeys] 靶子开始收键用了 {_receptive!r} 秒；握手后空格各路径 = "
      + json.dumps(_space_paths, ensure_ascii=False), flush=True)
steps.insert(0, (10, lambda: check(
    "握手：四路注入（unicode / 空格带不带标记 / 子线程）都进得了靶子",
    _space_paths == {"unicode字符": ".", "空格-不带标记-主线程": " ",
                     "空格-带自家标记": " ", "空格-不带标记-子线程": " "},
    _space_paths)))
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
