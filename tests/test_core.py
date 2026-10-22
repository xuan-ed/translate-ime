# -*- coding: utf-8 -*-
"""状态机 + 翻译管线 + 上屏的端到端测试（假 committer，不碰真键盘）。

真键盘钩子与 SendInput 注入由 ``tests/test_winapi_smoke.py`` 与
``tests/test_e2e_inject.py`` 单独验证，这里专注验证
「按键 -> 选词 -> 译文 -> 上屏」这条业务链路是否正确。

直接跑：``python tests/test_core.py``
"""
import sys
import copy
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()  # 输出被重定向时切 UTF-8；真控制台保持原样

from t_ime import winapi as W  # noqa: E402
from t_ime.config import load  # noqa: E402
from t_ime.core import (  # noqa: E402
    MAX_BUFFER, PASSTHROUGH_NUDGE_AFTER, ROW_EN, ROW_ZH, IMECore, TranslateWorker,
)
from t_ime.pinyin import PinyinEngine, load_or_build_index  # noqa: E402
from t_ime.translator import ZH2EN, Translator  # noqa: E402

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


class Harness:
    """一套共享的引擎/翻译器/工人 + 按需新建的状态机，避免线程泄漏。"""

    def __init__(self):
        self.cfg, _ = load()
        index = load_or_build_index(
            ROOT / "pinyin_index.pkl", min_freq=20, max_word_len=6, verbose=False
        )
        self.eng = PinyinEngine(index, beam_width=24, max_candidates=7)
        self.tr = Translator(self.cfg, verbose=False)
        self.worker = TranslateWorker(self.tr, debounce=0.05, verbose=False)
        self.worker.start()
        self.commits: list[str] = []
        # 模拟「系统里物理上真正按着哪些修饰键」。状态机现在会拿它给事件流纠偏，
        # 所以这个假键盘必须跟真键盘一样维护它，否则测的就不是真实行为了。
        self.mods: set[int] = set()

    def _commit(self, text):
        self.commits.append(text)
        return True

    def core(self):
        self.commits = []
        self.notes = []
        return IMECore(
            self.cfg, self.eng, self.tr,
            emit=self._emit, committer=self._commit, verbose=False,
            mods_source=lambda: frozenset(self.mods),
        )

    def _emit(self, ev):
        if ev.get("type") == "commit" and ev.get("note"):
            self.notes.append(ev["note"])

    def stop(self):
        self.worker.stop()


H = Harness()


def letter(c):
    return ord(c.upper())


def tap(core, c):
    """按一下再松开一个字母键。"""
    core.handle_key(letter(c), True, False)
    core.handle_key(letter(c), False, False)


def tapstr(core, s):
    for c in s:
        tap(core, c)


def key(core, vk):
    core.handle_key(vk, True, False)
    core.handle_key(vk, False, False)


def hot(core, spec):
    mods, vk = W.parse_hotkey(spec)
    for m in mods:
        H.mods.add(m)
        core.handle_key(m, True, False)
    core.handle_key(vk, True, False)
    core.handle_key(vk, False, False)
    for m in mods:
        core.handle_key(m, False, False)
        H.mods.discard(m)


def settle(core, seconds=1.5):
    end = time.time() + seconds
    while time.time() < end:
        if core.apply_translation_results():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------- #
print("== 中文输入与上屏 ==")
c = H.core()
tapstr(c, "nihao")
check("buffer == 'nihao'", c.buffer == "nihao", c.buffer)
check("zh[0] == '你好'", c.zh[:1] == ["你好"], c.zh[:3])
check("翻译状态 pending", c.tr_state == "pending", c.tr_state)
settle(c)
print(f"        译文候选: {c.en}")
check("译文非空", bool(c.en), c.en)
check("tr_state == done", c.tr_state == "done", c.tr_state)
key(c, W.VK_SPACE)
check("空格上屏中文", H.commits == ["你好"], H.commits)
check("上屏后缓冲清空", c.buffer == "", c.buffer)

print()
print("== Tab 切到译文行再上屏 ==")
c = H.core()
tapstr(c, "nihao")
settle(c)
check("初始活动行是中文", c.active == ROW_ZH, c.active)
key(c, W.VK_TAB)
check("Tab 后活动行是译文", c.active == ROW_EN, c.active)
en0 = c.en[0]
key(c, W.VK_SPACE)
check("空格上屏译文", H.commits == [en0], H.commits)
check("上屏内容是英文", bool(H.commits) and H.commits[0].isascii(), H.commits)

print()
print("== 上屏语言是粘住的（Tab 之后一直上屏译文）==")
check("active 仍是译文行", c.active == ROW_EN, c.active)
tapstr(c, "xiexie")
settle(c)
key(c, W.VK_SPACE)
check("第二个词也上屏译文", len(H.commits) == 2 and H.commits[-1].isascii(), H.commits)
print(f"        连续上屏: {H.commits}")
check("提交后 active 仍粘在译文行（模式不随提交丢失）", c.active == ROW_EN, c.active)
check("空缓冲时 Tab 放行给目标程序（不吞、不切模式）",
      c.handle_key(W.VK_TAB, True, False) is False and c.active == ROW_EN, c.active)
print("        （换语言请用 toggle_default 热键，见下）")
hot(c, "ctrl+alt+l")
check("toggle_default 热键切回中文行", c.active == ROW_ZH, c.active)

print()
print("== 数字键直选 ==")
c = H.core()
tapstr(c, "nihao")
zh1 = c.zh[1]
key(c, 0x32)  # '2'
check("按 2 上屏第 2 个候选", H.commits == [zh1], H.commits)
check("缓冲已清空", c.buffer == "", c.buffer)

# 先用一份「干净」的权重，跑完再放回去 —— 这一节会真的改选候选，
# 而改选按设计会写进自学习权重，不还原就会污染文件里后面的用例。
_saved_boosts = dict(H.eng.user_boosts())

print()
print("== 方向键选词 / 回车（README 写着「←→ 选词」「空格/回车上屏」，此前没有任何测试）==")
c = H.core()
tapstr(c, "nihao")
settle(c)
zh_arrow = list(c.zh)
check("至少两个中文候选（不然选词无从谈起）", len(zh_arrow) >= 2, zh_arrow[:3])
check("初始选中第 1 个", c.sel[ROW_ZH] == 0, c.sel)
check("右方向键被吞掉（不传给目标程序）",
      c.handle_key(W.VK_RIGHT, True, False) is True)
check("右方向键后选中第 2 个", c.sel[ROW_ZH] == 1, c.sel)
key(c, W.VK_RIGHT)
check("再按一次到第 3 个", c.sel[ROW_ZH] == 2, c.sel)
key(c, W.VK_LEFT)
check("左方向键退回来", c.sel[ROW_ZH] == 1, c.sel)
for _ in range(4):
    key(c, W.VK_LEFT)
check("左到头不越界（夹在 0）", c.sel[ROW_ZH] == 0, c.sel)
for _ in range(len(zh_arrow) + 5):
    key(c, W.VK_RIGHT)
check("右到尾不越界（夹在最后一个）",
      c.sel[ROW_ZH] == len(zh_arrow) - 1, (c.sel, len(zh_arrow)))
key(c, W.VK_UP)
check("上方向键跟左方向键一样（回退）", c.sel[ROW_ZH] == len(zh_arrow) - 2, c.sel)
key(c, W.VK_DOWN)
check("下方向键跟右方向键一样（前进）", c.sel[ROW_ZH] == len(zh_arrow) - 1, c.sel)
check("选词不动缓冲", c.buffer == "nihao", c.buffer)
check("光按方向键不会自己上屏", H.commits == [], H.commits)
key(c, W.VK_SPACE)
check("空格上屏的是方向键选中的那个候选（不是首选项）",
      H.commits == [zh_arrow[-1]], (H.commits, zh_arrow))
check("上屏后缓冲清空", c.buffer == "", c.buffer)
check("空缓冲时方向键放行给目标程序",
      c.handle_key(W.VK_RIGHT, True, False) is False)

print()
print("== 回车也能上屏（README：空格/回车上屏）==")
c = H.core()
tapstr(c, "xiexie")
settle(c)
zh_ret = list(c.zh)
key(c, W.VK_RETURN)
check("回车键上屏首选项", bool(H.commits) and H.commits[:1] == zh_ret[:1],
      (H.commits, zh_ret[:2]))
check("回车后缓冲清空", c.buffer == "", c.buffer)
check("空缓冲时回车放行给目标程序",
      c.handle_key(W.VK_RETURN, True, False) is False)

print()
print("== 译文行上也能用方向键挑译法 ==")
c = H.core()
tapstr(c, "nihao")
settle(c)
key(c, W.VK_TAB)
check("Tab 后活动行是译文", c.active == ROW_EN, c.active)
en_arrow = list(c.en)
check("译文候选不止一个", len(en_arrow) >= 2, en_arrow)
check("译文行初始选中第 1 条", c.sel[ROW_EN] == 0, c.sel)
key(c, W.VK_RIGHT)
check("译文行的方向键选第 2 条译法", c.sel[ROW_EN] == 1, c.sel)
check("中文行的选中不受影响", c.sel[ROW_ZH] == 0, c.sel)
key(c, W.VK_SPACE)
check("空格上屏的是被选中的那条译法", H.commits == [en_arrow[1]],
      (H.commits, en_arrow))

H.eng.load_boosts(_saved_boosts)  # 这一节的改选不留痕

print()
print("== 译文未就绪时给出准确提示，且不上屏 ==")
c = H.core()
tapstr(c, "beijing")
c.active = ROW_EN
c.en = []
c.tr_state = "pending"
key(c, W.VK_SPACE)
check("未就绪时不上屏", H.commits == [], H.commits)
check("提示是『译文还在路上』", "译文还在路上" in c.status, c.status)
key(c, 0x32)
check("数字键也给同一提示", "译文还在路上" in c.status, c.status)
c.active = ROW_ZH

print()
print("== 自学习：改选会被记住 ==")
saved = dict(H.eng.user_boosts())
c = H.core()
tapstr(c, "shangchuan")
before = list(c.zh)
print(f"        改选前: {before}")
pick = next((i for i, w in enumerate(before) if w == "上传"), 0)
if pick == 0:
    print("        （『上传』已在首位，跳过改选场景）")
else:
    key(c, 0x31 + pick)
    check("上屏了『上传』", H.commits == ["上传"], H.commits)
    c.buffer = "shangchuan"
    c._recompute()
    print(f"        改选后: {c.zh[:3]}")
    check("『上传』被顶到首位", c.zh[0] == "上传", c.zh[:3])
H.eng.load_boosts(saved)  # 不污染后面的用例

print()
print("== Backspace / Esc ==")
c = H.core()
tapstr(c, "niha")
key(c, W.VK_BACK)
check("退格后 buffer == 'nih'", c.buffer == "nih", c.buffer)
key(c, W.VK_BACK)
key(c, W.VK_BACK)
key(c, W.VK_BACK)
check("退到空缓冲", c.buffer == "", c.buffer)
check("空缓冲时退格放行给系统", c.handle_key(W.VK_BACK, True, False) is False)

tapstr(c, "nihao")
key(c, W.VK_ESCAPE)
check("Esc 清空缓冲", c.buffer == "", c.buffer)
check("Esc 后无候选", c.zh == [], c.zh)

print()
print("== 透传规则 ==")
c = H.core()
check("注入事件直接放行", c.handle_key(letter("n"), True, True) is False)
H.mods.add(W.VK_CONTROL)
c.handle_key(W.VK_CONTROL, True, False)
check("按住 Ctrl 时字母放行", c.handle_key(0x4E, True, False) is False)
check("按住 Ctrl 时不产生输入", c.buffer == "", c.buffer)
c.handle_key(W.VK_CONTROL, False, False)
H.mods.discard(W.VK_CONTROL)
check("松开 Ctrl 后恢复接管", c.handle_key(0x4E, True, False) is True)
check("F1 等功能键放行", c.handle_key(0x70, True, False) is False)
check("Win 键放行", c.handle_key(W.VK_LWIN, True, False) is False)

print()
print("== 字母抬起也要吞掉（否则系统会收到半个组合键）==")
c = H.core()
c.handle_key(0x4E, True, False)
check("字母按下被吞", c.buffer == "n", c.buffer)
check("字母抬起也被吞", c.handle_key(0x4E, False, False) is True)
check("第二次抬起不再吞", c.handle_key(0x4E, False, False) is False)

print()
print("== 热键：挂起 / 恢复 ==")
c = H.core()
hot(c, "ctrl+alt+z")
check("挂起后 enabled == False", c.enabled is False, c.enabled)
check("挂起后字母全透传", c.handle_key(0x4E, True, False) is False)
hot(c, "ctrl+alt+z")
check("再按恢复 enabled == True", c.enabled is True, c.enabled)
check("恢复后又能接管", c.handle_key(0x4E, True, False) is True)

print()
print("== 挂起时半截拼音要先上屏，不许静默扔掉 ==")
# 第 80 轮真机：打 nihao 再按 Ctrl+Alt+Z（挂起），恢复后按空格只掉出一个空格，
# 那五个字母凭空消失 —— 它们已经被钩子吞掉了，用户没有第二次机会。
c = H.core()
tapstr(c, "nihao")
check("打字后缓冲里有 nihao", c.buffer == "nihao", c.buffer)
hot(c, "ctrl+alt+z")
check("挂起后 enabled == False", c.enabled is False, c.enabled)
for _ in range(80):  # 修饰键松开后才真正投递
    if H.commits:
        break
    time.sleep(0.01)
check("★ 挂起前打的拼音先上屏了（没有凭空消失）", H.commits == ["你好"], H.commits)
check("挂起后缓冲清空", c.buffer == "", c.buffer)
check("挂起后字母透传", c.handle_key(0x4E, True, False) is False)

print()
print("== 译文还没回来时按挂起：不许把半截拼音吃掉（第 98 轮）==")
# 第 98 轮真机：在译文行、译文还在路上时按 Ctrl+Alt+Z，_commit(ROW_EN) 拒绝交付，
# 紧接着 _reset() 把缓冲整条清掉 —— 敲过的五个字母既没上屏也没留下。
c = H.core()
tapstr(c, "nihao")
c.active = ROW_EN
c.en = []
c.tr_state = "pending"
check("译文行且译文还没回来", c.active == ROW_EN and c.tr_state == "pending")
hot(c, "ctrl+alt+z")
check("挂起后 enabled == False", c.enabled is False, c.enabled)
for _ in range(80):  # 修饰键松开后才真正投递
    if H.commits:
        break
    time.sleep(0.01)
check("★ 挂起前打的拼音仍然上屏（按中文首候选）", H.commits == ["你好"], H.commits)
check("挂起后缓冲清空", c.buffer == "", c.buffer)
check("状态里说明了为什么先上屏中文", "译文还没回来" in c.status, c.status)
check("挂起后字母透传", c.handle_key(0x4E, True, False) is False)

print()
print("== 译文 pending 时切直通 / 打大写：半截拼音也要先落下（第 101 轮）==")
# 第 101 轮进程内复现：译文行上译文还在路上时
#   a) 切「英文直通」—— ``_commit(ROW_EN)`` 拒绝交付，缓冲就留在原地；直通里再按一次
#      切回拼音，那半截 'nihao' 仍在，会在后面某个动作里冒出来（实测：buffer 一直是
#      'nihao'、commits 空，用户敲的五个字母既没上屏也没消失）。
#   b) 直接打大写字母（Shift+Q）—— 同样留下 'nihao'，而 Q 已经上屏，顺序还会错位。
# 修法：三条入口（挂起 / 切直通 / 上屏大写）统一走 _settle_buffer_before_switch()。
cfg_direct = copy.deepcopy(H.cfg)
cfg_direct["hotkeys"]["toggle_direct"] = "ctrl+alt+m"


def core_direct():
    H.commits = []
    H.notes = []
    return IMECore(
        cfg_direct, H.eng, H.tr,
        emit=H._emit, committer=H._commit, verbose=False,
        mods_source=lambda: frozenset(H.mods),
    )


c = core_direct()
tapstr(c, "nihao")
c.active = ROW_EN
c.en = []
c.tr_state = "pending"
hot(c, "ctrl+alt+m")
check("切到了英文直通", c.direct is True, c.direct)
for _ in range(80):
    if H.commits:
        break
    time.sleep(0.01)
check("★ 切直通前半截拼音先上屏（按中文首候选）", H.commits == ["你好"], H.commits)
check("切直通后缓冲干净", c.buffer == "", c.buffer)
check("状态说明了为什么先上屏中文", "译文还没回来" in c.status, c.status)
tapstr(c, "abc")
check("直通里打的字母不碰缓冲", c.buffer == "" and c.direct is True,
      (c.buffer, c.direct))
hot(c, "ctrl+alt+m")
tap(c, "n")
check("切回拼音后缓冲里只有刚打的那一个字母（旧拼音不会复活）",
      c.buffer == "n", c.buffer)

c = core_direct()
tapstr(c, "nihao")
c.active = ROW_EN
c.en = []
c.tr_state = "pending"
H.mods.add(W.VK_SHIFT)
c.handle_key(W.VK_SHIFT, True, False)
c.handle_key(0x51, True, False)
c.handle_key(0x51, False, False)
H.mods.discard(W.VK_SHIFT)
c.handle_key(W.VK_SHIFT, False, False)
for _ in range(80):
    if len(H.commits) >= 2:
        break
    time.sleep(0.01)
check("★ 大写字母前先把拼音落下，顺序不乱", H.commits == ["你好", "Q"], H.commits)
check("大写之后缓冲也干净", c.buffer == "", c.buffer)

print()
print("== 译文 pending 但缓冲里没有中文候选时切直通，照样不许丢字（第 102 轮）==")
# 第 102 轮进程内复现：第 101 轮那层兜底还要求「有中文候选」（``and self.zh``），
# 而整串都不是拼音时（qqq）zh 就是空的 —— 兜底被跳过，切直通后 buffer 一直留着
# 'qqq'、commits 空：三个字母既没上屏，也没消失，之后某次别的动作才把它带出去。
c = core_direct()
tap(c, "q")
tap(c, "q")
tap(c, "q")
check("qqq 在缓冲里、没有中文候选（这一节的前提）",
      c.buffer == "qqq" and c.zh == [], (c.buffer, c.zh))
c.active = ROW_EN
c.en = []
c.tr_state = "pending"
hot(c, "ctrl+alt+m")
check("切到了英文直通", c.direct is True, c.direct)
for _ in range(80):
    if H.commits:
        break
    time.sleep(0.01)
check("★ 没有中文候选时整串原样上屏（不丢字）", H.commits == ["qqq"], H.commits)
check("切直通后缓冲干净", c.buffer == "", c.buffer)
check("状态同样说明了先上屏的是什么", "译文还没回来" in c.status, c.status)

print()
print("== 热键：一键直译上屏 ==")
c = H.core()
tapstr(c, "nihao")
settle(c)
hot(c, "ctrl+alt+space")
# 直译上屏是**推迟**投出去的：热键按下那一刻 Ctrl+Alt 还按在手里，而那时候注入
# 会被系统吃掉（见下面那节与 README 踩坑 26）。所以这里得等一等。
check("compose 的译文不在按下的那一瞬间投出去（那时修饰键还按着）",
      H.commits == [], H.commits)
for _ in range(60):
    if H.commits:
        break
    time.sleep(0.01)
check("compose 直接上屏译文", bool(H.commits) and H.commits[0].isascii(), H.commits)
check("compose 后缓冲清空", c.buffer == "", c.buffer)

print()
print("== 热键：上屏要等修饰键松开（第 26 轮真机挖出来的第三个 bug）==")
# 真机上按着 Ctrl+Alt 时往目标窗口注入的文本会被系统的「菜单模式」整个吃掉，
# 而 compose 恰恰是在热键按下那一瞬间注入的 -> 译文永远上不了屏。
# 修法：修饰键还按着就先排队，等松开之后再投。
c = H.core()
tapstr(c, "nihao")
settle(c)
mods_hk, vk_hk = W.parse_hotkey("ctrl+alt+space")
for vk_mod in (W.VK_CONTROL, W.VK_MENU):
    H.mods.add(vk_mod)
    c.handle_key(vk_mod, True, False)
c.handle_key(vk_hk, True, False)
c.handle_key(vk_hk, False, False)
check("修饰键还按着：一个字都没投出去", H.commits == [], H.commits)
check("但缓冲已经清了（界面该立刻收起来）", c.buffer == "", c.buffer)
time.sleep(0.35)          # 修饰键一直按着，超过「余震」那段也不许投
check("★ 修饰键按着的时候一直忍着不投", H.commits == [], H.commits)
for vk_mod in (W.VK_MENU, W.VK_CONTROL):
    c.handle_key(vk_mod, False, False)
    H.mods.discard(vk_mod)
for _ in range(60):
    if H.commits:
        break
    time.sleep(0.01)
check("★ 松开之后才投出去（而且真的投了）", len(H.commits) == 1, H.commits)
check("投出去的还是那句译文", bool(H.commits) and H.commits[0].isascii(), H.commits)

# 同一批排队的两句要按顺序投，不能丢
H.commits = []
c = H.core()
tapstr(c, "nihao")
settle(c)
c._down.add(W.VK_MENU)
c._deliver("first", "")
c._deliver("second", "")
check("修饰键按着时两次上屏都排着队", H.commits == [] and len(c._out_queue) == 2,
      (H.commits, c._out_queue))
c._down.discard(W.VK_MENU)
for _ in range(80):
    if len(H.commits) >= 2:
        break
    time.sleep(0.01)
check("松开后按顺序补齐", H.commits == ["first", "second"], H.commits)

print()
print("== 热键：切换默认上屏语言 ==")
c = H.core()
hot(c, "ctrl+alt+l")
check("toggle_default 切到译文行", c.active == ROW_EN, c.active)
hot(c, "ctrl+alt+l")
check("再切回中文行", c.active == ROW_ZH, c.active)

print()
print("== 热键：退出 ==")
c = H.core()
hot(c, "ctrl+alt+q")
check("quit_requested == True", c.quit_requested is True)

print()
print("== 边界 ==")
c = H.core()
for _ in range(80):
    tap(c, "a")
check("超长输入被截断", len(c.buffer) <= 48, len(c.buffer))

c = H.core()
tapstr(c, "nihao")
check("越界数字给出提示", (key(c, 0x39), "没有第 9 个" in c.status)[1], c.status)
check("越界不上屏", c.buffer == "nihao", c.buffer)

c = H.core()
tapstr(c, "woaini")
settle(c)
check("woaini 候选正确", c.zh[:1] == ["我爱你"], c.zh[:3])

c = H.core()
tapstr(c, "nih")
check("打到一半也能出候选（最长可切分前缀）", bool(c.zh), c.zh[:3])
print(f"        'nih' -> {c.zh[:3]}")

c = H.core()
tapstr(c, "zhongguo")
check("zhongguo 候选正确", c.zh[:1] == ["中国"], c.zh[:3])

print()
print("== 翻译引擎双向 ==")
en2zh = H.tr.translate_many("Hello", "en2zh", n=3)
check("en2zh 有输出", bool(en2zh), en2zh)
print(f"        Hello -> {en2zh}")
print(f"        你好   -> {H.tr.translate_many('你好', ZH2EN, n=3)}")
check("mock 不产生 'xxx (2)' 假变体",
      not any("(2)" in v for v in H.tr.translate_many("你好", ZH2EN, n=4)),
      H.tr.translate_many("你好", ZH2EN, n=4))

print()
print("== 翻译失败降级 ==")
c = H.core()
tapstr(c, "nihao")
c.active = ROW_EN
c.en = []
c.tr_state = "failed"
key(c, W.VK_SPACE)
check("翻译失败时降级上屏中文", H.commits == ["你好"], H.commits)
check("降级原因随事件上报", any("离线词表" in n for n in H.notes), H.notes)
check("降级原因也留在 status 上", "离线词表" in c.status, c.status)

# 第 64 轮：以前四种完全不同的原因被写成同一句「翻译不可用（检查 API key / 网络）」。
# 用户把 translate.enabled 关掉、或者本来就用离线词表时，去查 key 和网络是白费。
print()
print("== 翻译降级的四种原因要分开说 ==")
from types import SimpleNamespace  # noqa: E402


def fallback_status(tr) -> str:
    """只跑 _commit 的降级分支，不提交翻译、不碰真键盘。"""
    c = IMECore(
        H.cfg, H.eng, tr,
        emit=lambda ev: None, committer=lambda text: True, verbose=False,
        mods_source=lambda: frozenset(),
    )
    try:
        c.buffer = "nihao"
        c.zh = ["你好"]
        c.en = []
        c.active = ROW_EN
        c.tr_state = "failed"
        c._commit(ROW_EN, None)
        return c.status
    finally:
        c.worker.stop()


def stub_tr(**kw):
    base = dict(enabled=True, is_mock=False, mock_reason="", stats={},
                translate_many=lambda *a, **k: [])
    base.update(kw)
    return SimpleNamespace(**base)


s = fallback_status(stub_tr(enabled=False))
check("翻译开关关着 → 点出 translate.enabled", "translate.enabled" in s, s)
check("翻译开关关着 → 不再让人查 API key / 网络",
      "API key / 网络" not in s, s)

s = fallback_status(stub_tr(is_mock=True, mock_reason="没配 API key"))
check("离线词表（没配 key）→ 说清没配 key", "没配" in s and "离线词表" in s, s)

s = fallback_status(stub_tr(is_mock=True,
                            mock_reason="api.provider=mock，你主动选的离线词表"))
check("用户主动选 mock → 点出 api.provider", "api.provider" in s, s)

s = fallback_status(stub_tr(stats={"last_error": "HTTP 401 Unauthorized"}))
check("在线失败 → 带上真实错误", "HTTP 401" in s, s)

s = fallback_status(stub_tr(stats={}))
check("在线失败但没记原因 → 仍然给出下一步", "检查网络" in s, s)

# ---------------------------------------------------------------------- #
print()
print("== Shift 单击：中/英直通切换 ==")
c = H.core()
check("默认是拼音模式", c.direct is False)
key(c, W.VK_SHIFT)
check("单击 Shift 切到英文直通", c.direct is True)
key(c, W.VK_SHIFT)
check("再单击 Shift 切回拼音", c.direct is False)

c = H.core()
tapstr(c, "nihao")
H.mods.add(W.VK_SHIFT)
c.handle_key(W.VK_SHIFT, True, False)
c.handle_key(0x41, True, False)          # 按着 Shift 又按了 A
check("Shift+字母不再算单击", c.direct is False, c.direct)
check("Shift+A 直接上屏大写英文", H.commits == ["你好", "A"], H.commits)
H.mods.discard(W.VK_SHIFT)
c.handle_key(W.VK_SHIFT, False, False)

c = H.core()
c.handle_key(W.VK_SHIFT, True, False)
c.handle_key(W.VK_SHIFT, False, False)
check("已切到直通", c.direct is True)
check("直通下字母全透传", c.handle_key(0x4E, True, False) is False)
check("直通下不产生候选缓冲", c.buffer == "", c.buffer)

print()
print("== 直通/挂起时不能静默（用户报的「打字没反应」）==")
# 直通模式下连打字母，前两个不该吵，第三个必须给个回声
c = H.core()
c.toggle_direct()
for ch in "abc":
    tap(c, ch)
check("直通下连打 3 个字母会提示一次", "英文直通中" in c.toast, c.toast)
check("提示文案指明怎么切回去", "Shift" in c.toast, c.toast)
check("连打前两个不打扰", PASSTHROUGH_NUDGE_AFTER == 3, PASSTHROUGH_NUDGE_AFTER)

# 第 90 轮：input.shift_tap_ms=0 是合法值，但这时 held_ms <= 0 永不成立，
# Shift 单击根本切不回来 —— 直通提示再写「单击 Shift（0.0 秒内）」就是教用户按死键。
_cfg_tap0 = dict(H.cfg)
_cfg_tap0["input"] = dict(H.cfg["input"], shift_tap_toggles=True, shift_tap_ms=0)
c = IMECore(_cfg_tap0, H.eng, H.tr, emit=lambda ev: None,
            committer=lambda text: True, verbose=False,
            mods_source=lambda: frozenset())
c.toggle_direct()
c.toast = ""
for ch in "abc":
    tap(c, ch)
check("shift_tap_ms=0 时直通提示不提「Shift」", "Shift" not in c.toast, c.toast)
check("但仍然说清「英文直通中」", "英文直通中" in c.toast, c.toast)
check("这时候提示不再出现「0.0 秒」", "0.0 秒" not in c.toast, c.toast)

# 第 92 轮：切换「拼音 / 英文直通」那两句 toast 也写死了 Shift ——
# input.shift_tap_toggles=false 的人按 Shift 没反应，hotkeys.toggle_direct
# 配了也不提。这里按三种配置核对措辞。
def switch_toasts(over: dict) -> list[str]:
    cfg2 = copy.deepcopy(H.cfg)
    for sec, vals in over.items():
        cfg2.setdefault(sec, {}).update(vals)
    c2 = IMECore(cfg2, H.eng, H.tr, emit=lambda ev: None,
                 committer=lambda text: True, verbose=False,
                 mods_source=lambda: frozenset())
    got: list[str] = []
    c2._toast = lambda msg, *a, **k: got.append(msg)
    c2.toggle_direct()
    c2.toggle_direct()
    return got


entering, leaving = switch_toasts({})
check("默认配置：进直通的 toast 教「点一下 Shift 回拼音」",
      "点一下 Shift" in entering and "回拼音" in entering, entering)
check("默认配置：回拼音的 toast 教「点一下 Shift 切英文」",
      "点一下 Shift" in leaving and "切英文" in leaving, leaving)

only_key = switch_toasts({"input": {"shift_tap_toggles": False},
                          "hotkeys": {"toggle_direct": "ctrl+alt+m"}})
check("Shift 单击关掉时：toast 改教配置里的热键（Ctrl+Alt+M）",
      len(only_key) == 2 and all("Ctrl+Alt+M" in t for t in only_key), only_key)
check("Shift 单击关掉时：toast 里一个「Shift」都不许有",
      all("Shift" not in t for t in only_key), only_key)

no_way = switch_toasts({"input": {"shift_tap_toggles": False},
                        "hotkeys": {"toggle_direct": ""}})
check("两个键都没有时：toast 明说「没有切回拼音的键」",
      "没有切回拼音的键" in no_way[0], no_way[0])
check("两个键都没有时：也明说「没有切到英文直通的键」",
      "没有切到英文直通的键" in no_way[1], no_way[1])

c = H.core()
c.toggle_direct()
c.toast = ""               # 切换本身也会提示一条，先清掉才测得到「连打」这条
for ch in "ab":
    tap(c, ch)
check("只打两个字母时不提示", c.toast == "", c.toast)

# 切回拼音之后不该再提
c = H.core()
c.toggle_direct()
for ch in "abc":
    tap(c, ch)
c.toggle_direct()
c.toast = ""
for ch in "abc":
    tap(c, ch)
check("切回拼音后不再提示", c.toast == "", (c.toast, c.direct))

# 挂起状态下也要提示
c = H.core()
c.enabled = False
c.toast = ""
for ch in "abc":
    tap(c, ch)
check("挂起时连打 3 个字母会提示", "挂起" in c.toast, c.toast)
check("挂起提示说了怎么恢复", "Z" in c.toast, c.toast)

# 一旦有字真的进了缓冲，计数就该归零
c = H.core()
c.toggle_direct()
tap(c, "a")
c.toggle_direct()          # 切回拼音
tap(c, "n")
check("进缓冲后计数归零", c._pass_streak == 0, c._pass_streak)

print()
print("== 修饰键失同步（锁屏 / Ctrl+Alt+Del 之后 keyup 丢失）==")
c = H.core()
c.handle_key(W.VK_CONTROL, True, False)
# 系统里 Ctrl 其实已经松开了，但这个抬起事件钩子永远收不到
check("_down 里残留了 Ctrl", W.VK_CONTROL in c._down, c._down)
c.handle_key(0x4E, True, False)          # 下一个键就该自愈
check("卡住的 Ctrl 不会让输入法变哑巴", c.buffer == "n", c.buffer)
check("自愈后 _down 已清掉 Ctrl", W.VK_CONTROL not in c._down, c._down)

c = H.core()
# 钩子是在用户已经按着 Ctrl+Alt 时才装上的：keydown 从没被看到，但物理上真按着
H.mods.update({W.VK_CONTROL, W.VK_MENU})
hot_spec = "ctrl+alt+l"
mods_hk, vk_hk = W.parse_hotkey(hot_spec)
c.handle_key(vk_hk, True, False)
check("热键：事件流没看到修饰键也照样触发", c.active == ROW_EN, c.active)
H.mods.clear()

print()
print("== 热键：键盘连发不能重复触发 + 挡住系统菜单（第 26 轮真机挖出来的两条）==")
c = H.core()
mods_hk, vk_hk = W.parse_hotkey("ctrl+alt+l")
for vk_mod in (W.VK_CONTROL, W.VK_MENU):
    c.handle_key(vk_mod, True, False)
check("起点：默认上屏行是中文", c.active == ROW_ZH, c.active)
c.handle_key(vk_hk, True, False)
check("第一次按下：切到译文", c.active == ROW_EN, c.active)
# 真实键盘「按住不放」就是这个样子：只有按下，中间没有抬起
c.handle_key(vk_hk, True, False)
c.handle_key(vk_hk, True, False)
check("★ 连发两次按下不再切换（不然按住半秒开关就抖成一团）",
      c.active == ROW_EN, c.active)
check("连发的按下照样被吞掉（不漏给目标窗口）",
      c.handle_key(vk_hk, True, False) is True)
c.handle_key(vk_hk, False, False)
c.handle_key(vk_hk, True, False)
check("松手之后再按仍然有效（切回中文）", c.active == ROW_ZH, c.active)
for vk_mod in (W.VK_MENU, W.VK_CONTROL):
    c.handle_key(vk_mod, False, False)

# 真机 bug：主键被吞掉之后，系统只看到「Alt 自己松开了」-> 激活前台窗口的系统菜单，
# 把用户紧接着敲的下一个键吃掉。修法是在 Alt 松开之前补一个无害键（Shift）。
# 这里把那个函数换成假的：单元测试不许真往系统里打键。
_defuse_calls: list[int] = []
_real_defuse = W.defuse_system_menu
W.defuse_system_menu = lambda: _defuse_calls.append(1) or True
try:
    c = H.core()
    c.handle_key(W.VK_CONTROL, True, False)
    c.handle_key(W.VK_MENU, True, False)
    c.handle_key(vk_hk, True, False)
    c.handle_key(vk_hk, False, False)
    c.handle_key(W.VK_MENU, False, False)
    c.handle_key(W.VK_CONTROL, False, False)
    ok_defuse = False
    for _ in range(50):          # defuse 在子线程里做，等它一下（最多 0.5 秒）
        if _defuse_calls:
            ok_defuse = True
            break
        time.sleep(0.01)
    check("★ 热键带 Alt：会补一个无害键挡住系统菜单（第 26 轮的真机 bug）",
          ok_defuse, _defuse_calls)
finally:
    W.defuse_system_menu = _real_defuse

print()
print("== 标点：先把挂起的候选上屏，别插进拼音中间 ==")
c = H.core()
tapstr(c, "nihao")
res = c.handle_key(W.VK_OEM_PERIOD, True, False)
check("句号透传给目标程序", res is False)
check("句号先把拼音落下去", H.commits == ["你好"], H.commits)
check("落下去之后缓冲清空", c.buffer == "", c.buffer)

c = H.core()
res = c.handle_key(W.VK_OEM_PERIOD, True, False)
check("没打拼音时句号不动提交", res is False and H.commits == [], H.commits)

print()
print("== 引号：没在打拼音时它必须原样交给程序（第 35 轮的真机 bug）==")
# 早先 VK_OEM_7 无条件被吞：空缓冲里按一下引号，会冒出一个只有 "'" 的拼音行、
# 候选窗跟着弹出来，再按空格才把那个 "'" 打出去 —— 用户看到的是「按了没反应」。
c = H.core()
res = c.handle_key(W.VK_OEM_7, True, False)
check("空缓冲按单引号：不吞这个键", res is False)
check("空缓冲按单引号：缓冲仍然是空的", c.buffer == "", c.buffer)
check("空缓冲按单引号：没有候选行（不会弹空窗）",
      c.snapshot().get("zh") == [], c.snapshot().get("zh"))
check("空缓冲按单引号：没有状态提示", c.status == "", c.status)
check("空缓冲按单引号：抬起也不吞", c.handle_key(W.VK_OEM_7, False, False) is False)
res = c.handle_key(W.VK_SPACE, True, False)
check("空缓冲按单引号之后按空格：不会凭空打出引号",
      H.commits == [] and res is False, H.commits)

c = H.core()
tapstr(c, "ni")
res = c.handle_key(W.VK_OEM_7, True, False)
check("打拼音时单引号被吞（它是分词符）", res is True)
check("打拼音时单引号进缓冲", c.buffer == "ni'", c.buffer)
check("带分词符仍然出候选", len(c.snapshot().get("zh") or []) > 0,
      c.snapshot().get("zh"))
c.handle_key(W.VK_SPACE, True, False)
check("带分词符上屏的是「你」，不是那个引号", H.commits == ["你"], H.commits)

c = H.core()
tapstr(c, "ni")
H.mods = {W.VK_SHIFT}
res = c.handle_key(W.VK_OEM_7, True, False)
H.mods = set()
check("Shift+引号（双引号）：先把挂起的候选上屏", H.commits == ["你"], H.commits)
check("Shift+引号：然后放行给程序", res is False)
check("Shift+引号：缓冲清空", c.buffer == "", c.buffer)

c = H.core()
H.mods = {W.VK_SHIFT}
res = c.handle_key(W.VK_OEM_7, True, False)
H.mods = set()
check("空缓冲 Shift+引号：也不吞、也不进缓冲",
      res is False and c.buffer == "", (res, c.buffer))

c = H.core()
tapstr(c, "ni")
c.handle_key(W.VK_OEM_7, True, False)
c.handle_key(W.VK_OEM_7, True, False)
check("连按两个引号：缓冲里是 ni''（引擎会忽略空段）", c.buffer == "ni''", c.buffer)
check("连按两个引号：候选还是「你」",
      (c.snapshot().get("zh") or [""])[0] == "你", c.snapshot().get("zh"))
c.handle_key(W.VK_SPACE, True, False)
check("连按两个引号后空格：上屏「你」", H.commits == ["你"], H.commits)

print()
print("== 切窗口：丢掉挂起的合成，别让它落进新窗口 ==")
c = H.core()
tapstr(c, "nih")
check("切窗口前有挂起的缓冲", c.buffer == "nih", c.buffer)
check("切窗口清掉缓冲", c.on_foreground_changed() is True)
check("切窗口后缓冲为空", c.buffer == "", c.buffer)
check("切窗口不改上屏语言", c.active == ROW_ZH, c.active)
check("本来就没东西时不算变化", c.on_foreground_changed() is False)

print()
print("== Shift 单击的时间窗（第 20 轮：300ms 太紧，从容一按就什么都不发生）==")
# 真机探针里按住 Shift 500ms 再松开，结果是「什么都没发生、也没有提示」——
# 用户心里的动作是「按一下去切模式」，那比飞快弹一下慢得多。这里用平移过的
# 单调时钟把「按多久」变成可控量，不靠真的 sleep（也就不会拖慢测试）。
from t_ime.core import SHIFT_TAP_MS  # noqa: E402
import t_ime.core as core_mod  # noqa: E402


class _ShiftClock:
    """core 眼里的单调时钟 = 真时钟 + offset；worker 线程照常走，不受影响。"""

    def __init__(self):
        self.offset = 0.0

    def monotonic(self):
        return time.monotonic() + self.offset


class fake_clock:
    def __enter__(self):
        self.clock = _ShiftClock()
        self.real = core_mod.time
        core_mod.time = self.clock
        return self.clock

    def __exit__(self, *exc):
        core_mod.time = self.real
        return False


def shift_press(core, clock, hold_ms, middle=None):
    """按住 Shift 这么「久」再松开；middle 给定时，按住期间还敲一下那个字母。"""
    core.handle_key(W.VK_SHIFT, True, False)
    if middle is not None:
        core.handle_key(letter(middle), True, False)
        core.handle_key(letter(middle), False, False)
    clock.offset += hold_ms / 1000.0
    core.handle_key(W.VK_SHIFT, False, False)


check("默认时间窗是 600ms", SHIFT_TAP_MS == 600, SHIFT_TAP_MS)
check("配置默认值与实现一致（两边不许各写一个数）",
      int(H.cfg["input"]["shift_tap_ms"]) == SHIFT_TAP_MS,
      H.cfg["input"]["shift_tap_ms"])

with fake_clock() as clock:
    c = H.core()
    shift_press(c, clock, 500)
    check("从容按 500ms 也算单击（用户抱怨的正是这种）", c.direct is True, c.direct)

    c = H.core()
    shift_press(c, clock, 599)
    check("599ms 仍在窗内", c.direct is True, c.direct)

    c = H.core()
    shift_press(c, clock, 601)
    check("601ms 出窗：不切换", c.direct is False, c.direct)

    c = H.core()
    shift_press(c, clock, 1500)
    check("按着 1.5 秒不切换（Shift+点选那种用法不许误切）", c.direct is False, c.direct)

    c = H.core()
    shift_press(c, clock, 500)
    shift_press(c, clock, 500)
    check("再从容按一下能切回拼音", c.direct is False, c.direct)

    c = H.core()
    shift_press(c, clock, 100, middle="a")
    check("按住期间敲了字母就不算单击", c.direct is False, c.direct)
    check("那一下仍然是「打大写」", H.commits == ["A"], H.commits)

    c = H.core()
    cfg_off = dict(H.cfg)
    cfg_off["input"] = dict(H.cfg["input"], shift_tap_toggles=False)
    c_off = IMECore(cfg_off, H.eng, H.tr, emit=H._emit, committer=H._commit,
                    verbose=False, mods_source=lambda: frozenset(H.mods))
    shift_press(c_off, clock, 200)
    check("配置关掉 shift_tap_toggles 就完全不响应",
          c_off.direct is False, c_off.direct)

print()
print("== 拼音缓冲到上限（48 个字母）==")
c = H.core()
for _ in range(MAX_BUFFER):
    tap(c, "a")
check("打满 48 个字母：缓冲正好到上限", len(c.buffer) == MAX_BUFFER, len(c.buffer))
check("打满时不提前上屏", H.commits == [], H.commits)
tap(c, "a")  # 第 49 个
check("第 49 个字母没有被吞：先把上一段上屏、再开新缓冲",
      len(H.commits) == 1 and len(c.buffer) == 1, (H.commits, c.buffer))
check("上屏的内容非空", bool(H.commits) and H.commits[0] != "", H.commits)
check("状态栏说清了为什么突然上屏", "太长" in c.status, c.status)
before = len(c.buffer)
for _ in range(11):
    tap(c, "a")
check("再敲 11 个都进了缓冲，一个都没吞", len(c.buffer) == before + 11, len(c.buffer))
check("总共只额外上屏一次（不是每敲一下都上屏）", len(H.commits) == 1, H.commits)
tap(c, "a")
check("之后照常逐字增长", len(c.buffer) == before + 12, len(c.buffer))
key(c, W.VK_BACK)
check("满过之后退格照常可用", len(c.buffer) == before + 11, len(c.buffer))

print("== 上屏时缓冲尾巴上的字母不能丢（合法前缀 + 多余字母）==")
# 第 85 轮真机：候选是按「最长可切分前缀」算的，``nihaoz`` 的候选属于 ``nihao``；
# 以前按空格上屏「你好」之后 _reset() 把整个缓冲清掉，用户敲的那个 z 既没上屏
# 也没留下 —— 字母凭空消失。现在没用掉的那一截留在缓冲里，当下一段组合的开头。
c = H.core()
tapstr(c, "nihaoz")
check("尾巴上有字母时仍给出前缀的候选", c.zh[:1] == ["你好"], c.zh[:3])
key(c, W.VK_SPACE)
check("空格只上屏前缀对应的词", H.commits == ["你好"], H.commits)
check("没用掉的字母留在缓冲里，没被吞掉", c.buffer == "z", c.buffer)
key(c, W.VK_SPACE)
check("再按空格把留着的字母原样上屏", H.commits == ["你好", "z"], H.commits)
check("这之后缓冲才清空", c.buffer == "", c.buffer)

c = H.core()
tapstr(c, "nihaozh")
check("两个多余字母也一个都不丢（候选照出）", c.zh[:1] == ["你好"], c.zh[:3])
key(c, W.VK_SPACE)
check("上屏后缓冲留下 zh", c.buffer == "zh", c.buffer)
key(c, W.VK_SPACE)
check("再空格把 zh 原样上屏", H.commits == ["你好", "zh"], H.commits)

c = H.core()
tapstr(c, "xian")
key(c, W.VK_SPACE)
check("整串都被吸收时缓冲照常清空", c.buffer == "" and H.commits[:1] == ["先"],
      (c.buffer, H.commits))

c = H.core()
tapstr(c, "qqqq")
key(c, W.VK_SPACE)
check("非拼音串仍整串上屏、不留残渣",
      c.buffer == "" and H.commits == ["qqqq"], (c.buffer, H.commits))

print()
print("== 翻译关着时，Ctrl+Alt+L 不许切进「译文」死胡同（第 91 轮）==")


def toggle_with(tr):
    """用指定的翻译器建一个 core，按一次 Ctrl+Alt+L，返回 (core, toasts)。"""
    c = IMECore(
        H.cfg, H.eng, tr,
        emit=lambda ev: None, committer=lambda text: True, verbose=False,
        mods_source=lambda: frozenset(),
    )
    toasts: list[str] = []
    c._toast = toasts.append
    try:
        hot(c, "ctrl+alt+l")
        return c, toasts
    finally:
        c.worker.stop()


c_off, toasts_off = toggle_with(stub_tr(enabled=False))
check("翻译关着 → 还留在中文行", c_off.active == ROW_ZH, c_off.active)
check("翻译关着 → toast 说清切不到译文",
      bool(toasts_off) and "切不到译文" in toasts_off[-1], toasts_off)

c_on, toasts_on = toggle_with(stub_tr(enabled=True))
check("翻译开着 → 照旧切到译文行", c_on.active == ROW_EN, c_on.active)
check("翻译开着 → toast 说空格上屏译文",
      bool(toasts_on) and "空格上屏：译文" in toasts_on[-1], toasts_on)

print()
print("== 翻译关着时，Tab 也不许切进「译文」死胡同（第 97 轮）==")


def tab_with(tr):
    """打 nihao 再按一下 Tab，返回 (core, toasts, commits)；用完要 stop worker。"""
    commits: list[str] = []
    c = IMECore(
        H.cfg, H.eng, tr,
        emit=lambda ev: None, committer=lambda text: commits.append(text) or True,
        verbose=False, mods_source=lambda: frozenset(),
    )
    toasts: list[str] = []
    c._toast = toasts.append
    tapstr(c, "nihao")
    key(c, W.VK_TAB)
    return c, toasts, commits


c_tabo, toasts_tabo, commits_tabo = tab_with(stub_tr(enabled=False))
try:
    check("翻译关着 → Tab 之后还留在中文行", c_tabo.active == ROW_ZH, c_tabo.active)
    check("翻译关着 → Tab 的 toast 说清切不到译文",
          bool(toasts_tabo) and "切不到译文" in toasts_tabo[-1], toasts_tabo)
    check("翻译关着 → Tab 没把拼音缓冲弄丢", c_tabo.buffer == "nihao", c_tabo.buffer)
    key(c_tabo, W.VK_SPACE)
    check("翻译关着 → 之后按空格照常上屏中文", commits_tabo == ["你好"], commits_tabo)
finally:
    c_tabo.worker.stop()

c_tabn, toasts_tabn, _ = tab_with(stub_tr(enabled=True))
try:
    check("翻译开着 → Tab 照旧切到译文行", c_tabn.active == ROW_EN, c_tabn.active)
    check("翻译开着 → Tab 不弹 toast（别遮住候选窗）", not toasts_tabn, toasts_tabn)
finally:
    c_tabn.worker.stop()

H.stop()
print()
print(f"  结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
