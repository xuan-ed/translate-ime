# -*- coding: utf-8 -*-
"""Windows 底层 API 封装（纯 ctypes，不依赖 pywin32）。

只做三件事：

1. ``KeyboardHook`` —— 全局低级键盘钩子 WH_KEYBOARD_LL，能看见并**吞掉**任意按键，
   且可以只吞我们关心的键（系统热键照常放行）。
2. ``send_text`` —— 用 SendInput + KEYEVENTF_UNICODE 把文本直接注入前台窗口，
   绕开输入法，任意程序都能收到。注入的事件带 LLKHF_INJECTED 标志，钩子会放行，
   不会自激。
3. ``make_noactivate`` —— 给 Tk 窗口加 WS_EX_NOACTIVATE|WS_EX_TOOLWINDOW，
   让悬浮候选窗永远不抢焦点（否则键盘输入会跑进候选窗里，整个输入法就废了）。
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
WH_KEYBOARD_LL = 13

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
WM_CANCELMODE = 0x001F   # 「把你的模态状态（菜单 / 拖动 / 消息框）收掉」
WM_NULL = 0x0000         # 无害的空消息，用来「敲敲门」看对方理不理我们
# SendMessageTimeout 的选项：对方已被系统判为没响应就立刻返回 + 期间不处理自己的消息，
# 这样「问一句」既有超时上限、也不会顺手把别的消息插进来。（responds_within 用）
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002

KEYDOWN_MSGS = frozenset({WM_KEYDOWN, WM_SYSKEYDOWN})
KEYUP_MSGS = frozenset({WM_KEYUP, WM_SYSKEYUP})

LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10
LLKHF_ALTDOWN = 0x20
LLKHF_UP = 0x80

#: 我们自己注入按键时打在 ``dwExtraInfo`` 上的标记（ASCII "TIME"）。
#:
#: 为什么要这个标记：``LLKHF_INJECTED`` 只能说明「这个事件不是键盘硬件直接产生的」，
#: 分不出是**我们自己回灌的**还是**别的程序发的**。前者必须忽略（否则上屏的文本会被
#: 自己的钩子钩回来，无限自激），后者却必须当成真实按键处理 —— 因为用户机器上只要装了
#: 任何会转发按键的软件（PowerToys Keyboard Manager、AutoHotkey、罗技/雷蛇驱动、
#: 某些笔记本的快捷键工具、软键盘），**所有**手敲的按键都会带着 INJECTED 标志进来。
#: 早先的版本把这些一律当成「注入」丢掉，症状就是「打字完全没反应」。
T_IME_INJECT_TAG = 0x54494D45

#: 钩子自检用的「无害按键」：``0xFF`` 没有映射到任何真实按键。
#:
#: 为什么需要它：Windows 会在回调超时（``LowLevelHooksTimeout``）或长时间收不到系统消息时
#: **悄悄摘掉**低级钩子 —— 不报错、不通知，句柄还非空、线程还在泵消息，``alive`` 依旧是 True，
#: 但一个键都进不来。用户看到的就是「打字没反应」，而唯一的老办法是重启程序。
#: 唯一可靠的探测方式是**自己注入一个带标记的按键，看它绕系统一圈回不回来**。
#:
#: 但这招有个硬前提：**注入方必须是前台窗口的属主**。实测（2026-10-16，本机 Low 完整性）：
#: 进程自己没有前台窗口时，``SendInput`` 照样返回成功，可按键**连系统都没进**
#: （``GetAsyncKeyState`` 读不到、任何钩子也看不到）—— 因为 UIPI 把低完整性进程的注入
#: 挡在门外，而文档明说这种情况返回值**不体现失败**。常驻实例（``pythonw`` + 不抢焦点的
#: 候选窗）正好就是「没有前台窗口」，所以自检**不能**用来判断常驻实例的钩子死活，
#: 只能用在 ``--hook-check`` 这种前台交互诊断里。常驻探活改用 :data:`SCAN_VKS`。
HOOK_PROBE_VK = 0xFF

#: 被动探活要扫的虚拟键：``0x08..0xFE`` 覆盖打字能碰到的全部键。
#:
#: 鼠标键是 ``0x01..0x06``，不在这个范围里，所以点鼠标不会让探活误判成「有人在打字」。
#: 实测一次全扫 0.21 毫秒（247 次 ``GetAsyncKeyState``，0.87 微秒/次），
#: 按 20 次/秒 采样只占 0.4% 单核。
SCAN_VKS: tuple[int, ...] = tuple(range(0x08, 0xFF))

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

MAPVK_VK_TO_VSC = 0

# 关键虚键码
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12          # Alt
VK_PAUSE = 0x13
VK_CAPITAL = 0x14       # CapsLock
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21         # PageUp
VK_NEXT = 0x22          # PageDown
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_DELETE = 0x2E
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_APPS = 0x5D          # 菜单键
VK_OEM_7 = 0xDE         # 单引号/双引号键，用来做拼音分词符
VK_OEM_MINUS = 0xBD
VK_OEM_PLUS = 0xBB
VK_OEM_COMMA = 0xBC
VK_OEM_PERIOD = 0xBE
# 左右分开的修饰键。有键盘只报 VK_SHIFT/VK_CONTROL/VK_MENU，有的报左右键码，
# 所以查真实按键状态时两边都要问。normalize_vk() 会把 0xA0-0xA5 归并成 0x10/0x11/0x12。
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
# 会打出可见字符的标点键。拼音写到一半时按到它们，应该先把候选上屏再把字符放行，
# 而不是让那个字符插进正在合成的拼音里。
VK_OEM_1 = 0xBA         # ;:
VK_OEM_2 = 0xBF         # /?
VK_OEM_3 = 0xC0         # `~
VK_OEM_4 = 0xDB         # [{
VK_OEM_5 = 0xDC         # \|
VK_OEM_6 = 0xDD         # ]}
VK_OEM_8 = 0xDF
VK_OEM_102 = 0xE2       # 欧洲键盘上的 <>

PUNCT_VKS = frozenset({
    VK_OEM_1, VK_OEM_2, VK_OEM_3, VK_OEM_4, VK_OEM_5, VK_OEM_6,
    VK_OEM_8, VK_OEM_102,
    VK_OEM_PLUS, VK_OEM_COMMA, VK_OEM_MINUS, VK_OEM_PERIOD,
})
"""按下就该「先上屏候选、再把字符放行」的标点键。VK_OEM_7 不在里面 —— 它是分词符。"""

MOD_VKS = (VK_SHIFT, VK_LSHIFT, VK_RSHIFT,
           VK_CONTROL, VK_LCONTROL, VK_RCONTROL,
           VK_MENU, VK_LMENU, VK_RMENU,
           VK_LWIN, VK_RWIN)
"""查「现在真实按着哪些修饰键」时要问的全部键码。"""


def is_modifier(vk: int) -> bool:
    """这个键码是不是修饰键本身（左/右键码也算）。

    Web 控制板「按一下你想要的组合键」需要它：钩子会把 Ctrl 的按下也送进来，
    修饰键本身不能当成「组合键的主键」，否则用户按 Ctrl+Alt+Q 会被录成
    ``Ctrl+Alt``（然后校验报「缺少主键」）。第 108 轮加的。
    """
    return int(vk) in MOD_VKS

VK_NAME: dict[str, int] = {
    "backspace": VK_BACK,
    "bksp": VK_BACK,
    "tab": VK_TAB,
    "enter": VK_RETURN,
    "return": VK_RETURN,
    "shift": VK_SHIFT,
    "ctrl": VK_CONTROL,
    "control": VK_CONTROL,
    "alt": VK_MENU,
    "menu": VK_MENU,
    "esc": VK_ESCAPE,
    "escape": VK_ESCAPE,
    "space": VK_SPACE,
    "win": VK_LWIN,
    "lwin": VK_LWIN,
    "rwin": VK_RWIN,
    "capslock": 0x14,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "apps": 0x5D,
    "oem_1": 0xBA,   # ;:
    "oem_plus": 0xBB,
    "oem_comma": 0xBC,
    "oem_minus": 0xBD,
    "oem_period": 0xBE,
    "oem_2": 0xBF,   # /?
    "oem_3": 0xC0,   # `~
    "oem_4": 0xDB,   # [{
    "oem_5": 0xDC,   # \|
    "oem_6": 0xDD,   # ]}
    "oem_7": 0xDE,   # '"
}
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK_NAME[_c] = ord(_c.upper())
for _d in "0123456789":
    VK_NAME[_d] = ord(_d)
for _i in range(1, 25):
    VK_NAME[f"f{_i}"] = 0x6F + _i

#: 反过来查「这个 vk 该写成什么名字」。同一个 vk 有多个别名时挑最短的那个
#: （``alt`` 而不是 ``menu``、``del`` 而不是 ``delete``），字母/数字本来就是最短的。
_VK_LABEL: dict[int, str] = {}
for _name, _vk in VK_NAME.items():
    _cur = _VK_LABEL.get(_vk)
    if _cur is None or (len(_name), _name) < (len(_cur), _cur):
        _VK_LABEL[_vk] = _name

#: 修饰键在给人看的写法里的顺序（``Ctrl+Alt+Shift+Win``）。
_MOD_ORDER: tuple[tuple[int, str], ...] = (
    (VK_CONTROL, "ctrl"),
    (VK_MENU, "alt"),
    (VK_SHIFT, "shift"),
    (VK_LWIN, "win"),
)


def combo_text(mods, main: int) -> str:
    """把**解析出来的**组合键写回给人看的样子：``({ctrl,alt}, Q) -> "Ctrl+Alt+Q"``。

    为什么不用原始的配置写法：``"ctrl+q+w"`` 这种写了两个主键的串，实际注册的只有
    最后一个主键，直接 ``pretty()`` 原文会报出「Ctrl+Q+W」—— 用户按 Ctrl+Q+W 没反应，
    按 Ctrl+W 才退出（第 84 轮）。给用户看的东西必须从真正生效的 (mods, main) 来。
    """
    mods = set(mods or ())
    parts = []
    for vk, label in _MOD_ORDER:
        if vk == VK_LWIN:
            if VK_LWIN in mods or VK_RWIN in mods:
                parts.append(label)
        elif vk in mods:
            parts.append(label)
    parts.append(_VK_LABEL.get(int(main), f"vk{int(main)}"))
    return "+".join(p.capitalize() for p in parts)


# 修饰键 -> 归一化后的 VK（低位/高位区分取消，统一成通用 VK）
MODIFIER_VKS = {VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN}
VK_NORMALIZE = {
    0xA0: VK_SHIFT, 0xA1: VK_SHIFT,      # LSHIFT / RSHIFT
    0xA2: VK_CONTROL, 0xA3: VK_CONTROL,  # LCTRL / RCTRL
    0xA4: VK_MENU, 0xA5: VK_MENU,        # LALT / RALT
}


def normalize_vk(vk: int) -> int:
    return VK_NORMALIZE.get(vk, vk)


def key_is_down(vk: int) -> bool:
    """这个键此刻物理上是否按着（问系统，不依赖我们收到过哪些事件）。"""
    return bool(user32.GetAsyncKeyState(int(vk)) & 0x8000)


def caps_lock_on() -> bool | None:
    """CapsLock 现在是开着的吗？读得到就给 True/False，问不到才给 None。

    用哪个 API（本机实测，README 踩坑第 20 条）：
      * ``GetKeyState(VK_CAPITAL) & 0x01`` 就是那个**开关状态**。关键是它连
        **完全没有窗口**的进程里也读得准 —— 实测：无窗口子进程在 CapsLock 关着时
        读到 0，父进程把开关真的拨到另一边之后，同一个子进程读到 1。
        （早先「没有前台窗口就一律答 0」的说法是错的：那次读 0 要么因为开关本来
        就关着，要么因为**屏幕锁着** —— 锁屏时 LockApp 占着桌面，什么读数都是 0、
        注入也全被丢掉。所以只有锁屏期间的 0 不可信。）
      * **不要**改用 ``GetAsyncKeyState(VK_CAPITAL) & 0x01``：它的最低位是
        「自上次调用以来按下过」这种一次性闩，不是开关状态。实测：CapsLock 开着时
        第一次问答 1，紧接着连问 5 次全是 0 —— 拿它当状态读会读出「关着」。
        （``GetAsyncKeyState`` 的 0x8000 位是「此刻是否物理按着」，``physical_mods``
        用的就是那一位，跟这里无关。）

    返回 None 只留给「调用本身炸了」这一种情形。而且这里只是**兜底**：权威是钩子
    自己看到的 ``VK_CAPITAL`` 按下事件（状态机据此自己记一份），系统这份只在启动 /
    重装钩子时拿来对一次表。
    """
    try:
        state = int(user32.GetKeyState(VK_CAPITAL))
    except Exception:
        return None
    return bool(state & 0x01)


def physical_mods() -> frozenset[int]:
    """**问系统**现在真实按着哪些修饰键（已归一化成 0x10/0x11/0x12/0x5B/0x5C）。

    为什么不能只信钩子事件流：我们维护的「按着的键」集合，其抬起是靠钩子回调补的。
    只要有一次抬起没被钩子看到，那个键就永远留在集合里。现实中很容易发生 ——
    Ctrl+Alt+Del、Win+L 锁屏、UAC 提权弹窗、锁屏后解锁，钩子在这些时刻完全收不到事件。
    一旦 Ctrl 卡住，``down & BLOCKING_MODS`` 恒真，**每个键都被当成组合键透传，
    整个输入法就静默变成哑巴**，用户只会说「按了没反应」。
    反过来，如果钩子是在用户已经按着 Ctrl 时装的，Ctrl 的按下我们又没看到，
    热键 ``mods <= down`` 就永远不匹配 —— 同样是「热键没反应」。
    两种失同步都靠这个函数纠偏。
    """
    out: set[int] = set()
    for vk in MOD_VKS:
        if key_is_down(vk):
            out.add(normalize_vk(vk))
    return frozenset(out)


def parse_hotkey(spec: str) -> tuple[frozenset[int], int]:
    """把 ``"ctrl+alt+space"`` 解析成 ``(修饰键集合, 主键)``。

    一个热键只能有**一个**主键：``"ctrl+q+w"`` 以前会把 q 悄悄丢掉、只注册 Ctrl+W，
    而界面上报的还是「Ctrl+Q+W」（第 84 轮）。这种写法现在直接报错，让用户改。
    """
    mods: set[int] = set()
    main = 0
    main_part = ""
    for part in str(spec).split("+"):
        part = part.strip().lower()
        if not part:
            continue
        vk = VK_NAME.get(part)
        if vk is None:
            raise ValueError(f"无法识别的按键名：{part!r}（来自热键 {spec!r}）")
        if vk in MODIFIER_VKS:
            mods.add(vk)
        elif main:
            raise ValueError(
                f"热键 {spec!r} 里有两个主键（{main_part} 和 {part}）—— "
                f"一个热键只能有一个主键（修饰键可以有多个）")
        else:
            main = vk
            main_part = part
    if not main:
        raise ValueError(f"热键 {spec!r} 缺少主键")
    return frozenset(mods), main


# --------------------------------------------------------------------------- #
# 结构体
# --------------------------------------------------------------------------- #
class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class MOUSEINPUT(ctypes.Structure):
    # 我们不发鼠标事件，但 union 必须把它算进去，
    # 否则 sizeof(INPUT) 在 x64 上会是 32 而不是真正的 40，SendInput 直接失败。
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)

# --------------------------------------------------------------------------- #
# 函数签名
# --------------------------------------------------------------------------- #
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
user32.SetWindowLongW.restype = wintypes.LONG
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.IsHungAppWindow.argtypes = [wintypes.HWND]
user32.IsHungAppWindow.restype = wintypes.BOOL
# LRESULT SendMessageTimeoutW(HWND, UINT, WPARAM, LPARAM, UINT flags, UINT timeout, PDWORD_PTR)
user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t,
                                       ctypes.c_ssize_t, wintypes.UINT, wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_size_t)]
user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL

# GetAncestor 的 flags（只用到 GA_ROOT）
GA_ROOT = 2
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
# MonitorFromWindow 的 flag：DEFAULTTONULL 在窗口**完全不在任何显示器上**时返回 0。
# 这是判断「候选窗被放到屏幕外了，用户根本看不见」最直接的一招。
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = ctypes.c_void_p


class MONITORINFO(ctypes.Structure):
    """GetMonitorInfoW 的入参结构：一块显示器的矩形与工作区。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
# GetAsyncKeyState 返回 SHORT，最高位是「当前是否按下」。这是唯一能问到
# 「此刻物理上按着哪些键」的办法 —— 事件流靠不住，见 physical_mods() 的说明。
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
# GetKeyState 返回 SHORT，最低位是 **CapsLock 的开/关状态**（不是「此刻是否按下」）。
# 它连无窗口进程里也读得准；**锁屏期间**什么读数都是假的 0 —— 见 caps_lock_on() 的说明。
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


# --------------------------------------------------------------------------- #
# 全局键盘钩子
# --------------------------------------------------------------------------- #
class KeyboardHook:
    """全局低级键盘钩子。

    回调在钩子线程上执行，必须**尽快返回**（系统默认 300ms 超时就摘钩子），
    所以回调里只做状态机，重活（HTTP 翻译、上屏注入）一律丢到别的线程。

    回调签名 ``handler(vk: int, is_down: bool, is_injected: bool) -> bool``，
    返回 ``True`` 表示吞掉这个键（上层程序收不到），``False`` 表示放行。
    """

    def __init__(self, handler: Callable[[int, bool, bool], bool], name: str = "kbhook"):
        self._handler = handler
        self._name = name
        self._thread: threading.Thread | None = None
        self._thread_id: int = 0
        self._hook: int = 0
        self._ready = threading.Event()
        self._error: str | None = None
        # 必须保持引用，否则 ctypes 回调被 GC 掉会直接崩进程
        self._proc = HOOKPROC(self._dispatch)
        # ---- 统计：用来回答「你敲的键到底有没有进到钩子里」----
        # 用户报「打字没反应」时，唯一能把责任分清的事实就是这个：
        #   real == 0  → 按键根本没进钩子（钩子被系统悄悄摘掉 / 被安全软件拦 / 焦点在提权窗口）
        #   real > 0   → 键进来了，问题在下游（状态机、候选窗、上屏）
        self.total = 0            # 收到的全部按键事件
        self.real = 0             # 其中真实按键（键盘硬件来的）
        self.injected_count = 0   # 其中带 INJECTED 标志的（= self_injected + foreign_injected）
        self.self_injected = 0    # 其中我们自己的回灌（必须忽略，否则自激）
        self.foreign_injected = 0 # 其中**别的程序**注入的（改键器/宏/软键盘 —— 要当成真实按键）
        self.swallowed = 0        # 被我们吞掉的个数（吞掉 = 上层程序收不到）
        self.last_real_at = 0.0   # 最后一次真实按键的 monotonic 时刻（0 = 从没有过）
        self.last_effective_at = 0.0  # 最后一次「有效按键」（真实 + 别人注入）的时刻
        # ---- 自检：用来回答「钩子现在还挂着吗」----
        # 句柄非空 + 线程活着都**不能**证明钩子还在生效（见 alive 的说明），
        # 唯一可靠的办法是自己注入一个带标记的无害按键，看它绕一圈回不回来。
        self.probe_sent = 0       # 自检按键发出次数
        self.probe_seen = 0       # 自检按键真的回到钩子里的次数
        self.probe_failures = 0   # 发出去却没人回应的次数（= 钩子被摘掉了）
        self.probe_send_failures = 0   # 连发都没发出去（SendInput 被拒）
        self.last_probe_error: str | None = None   # 最后一次自检为什么失败
        # ---- 被动探活：问系统「有没有键刚被按下」，再看钩子收到没有 ----
        # 两次采样「此刻按着的键」的差集 = 刚刚按下的键。为什么不直接信
        # GetAsyncKeyState 的最低有效位：文档明说那个位不可靠（别的进程读一次就清掉了），
        # 所以它以「补充信号」的身份参与，主判据是明确的最高位变化。
        self._down_before: set[int] = set()
        # 第一次采样只用来「记住现在按着哪些键」，不报任何东西 —— 否则刚重装完钩子时
        # 正好被按住的那个键（比如打大写时的 Shift）会被当成一次「刚按下」，
        # 而已经按下的键不会再有新事件，对账必然失败 → 白重装一次。
        self._samples = 0

    @property
    def effective(self) -> int:
        """真正被当成输入处理的按键数（真实 + 别的程序注入，不含我们自己的回灌）。"""
        return self.real + self.foreign_injected

    # -- 内部 ------------------------------------------------------------- #
    def _dispatch(self, n_code, w_param, l_param):
        if n_code < 0:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        msg = int(w_param)
        # 只有「我们自己打的标记」才算自激回灌；别的程序注入的按键要当成真实输入，
        # 否则装了改键器/宏软件的机器上会表现为「打字完全没反应」。
        raw_injected = bool(kb.flags & LLKHF_INJECTED)
        own_tag = int(kb.dwExtraInfo or 0) == T_IME_INJECT_TAG
        self_injected = raw_injected and own_tag

        if msg in KEYDOWN_MSGS or msg in KEYUP_MSGS:
            is_down = msg in KEYDOWN_MSGS
            self.total += 1
            if self_injected:
                self.injected_count += 1
                self.self_injected += 1
                if int(kb.vkCode) == HOOK_PROBE_VK and is_down:
                    # 自检按键的**按下**回来了 —— 这是「钩子此刻确实还挂着」的唯一证据。
                    # 只数按下：一次自检 = 一个来回，数两个事件会让 probes 和 probe_seen
                    # 永远差一倍（另一半是抬起），对不起来就没人敢信这两个数。
                    self.probe_seen += 1
            else:
                if raw_injected:
                    self.injected_count += 1
                    self.foreign_injected += 1
                else:
                    self.real += 1
                    self.last_real_at = time.monotonic()
                self.last_effective_at = time.monotonic()
            try:
                swallow = bool(self._handler(int(kb.vkCode), is_down, self_injected))
            except Exception as exc:  # 回调炸了不能影响系统键盘
                print(f"[hook] handler 异常：{exc!r}")
                swallow = False
            if swallow:
                self.swallowed += 1
                return 1

        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _run(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._proc, kernel32.GetModuleHandleW(None), 0
        )
        if not self._hook:
            self._error = f"SetWindowsHookExW 失败，GetLastError={ctypes.get_last_error()}"
            self._ready.set()
            return

        self._ready.set()
        msg = wintypes.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        user32.UnhookWindowsHookEx(self._hook)
        self._hook = 0

    # -- 对外 ------------------------------------------------------------- #
    def start(self, timeout: float = 5.0) -> bool:
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self._error = "安装键盘钩子超时"
            return False
        return self._error is None

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def alive(self) -> bool:
        """钩子线程还活着、句柄还在。

        注意这**不等于**「钩子还在生效」：Windows 会在一段时间内收不到系统消息、
        或回调超时（默认可由 ``LowLevelHooksTimeout`` 调）时**悄悄摘掉**低级钩子，
        既不报错也不会把我们叫醒。所以只有 ``real`` 计数能证明它真的在工作 ——
        要主动查证，前台诊断用 :meth:`probe`（真的注入一个自检按键），
        常驻进程用 :meth:`sample_keydowns`（不注入，拿系统按键状态跟钩子对账）。
        """
        return bool(self._hook) and self._thread is not None and self._thread.is_alive()

    def probe(self, timeout: float = 0.3) -> bool:
        """注入一个自检按键，看钩子自己收不收得到 —— 返回 ``False`` 就是「钩子已经死了」。

        这是唯一能区分「线程活着」和「钩子还在生效」的办法：被系统摘掉之后
        ``alive`` 仍然是 ``True``，而自检按键会**再也回不来**。探测用的 ``0xFF``
        没有映射到任何真实按键，钩子正常时它会被自己吞掉（上层程序看不见）；
        万一钩子真死了，它顶多给前台程序送去一个无意义的按键。
        """
        if not self.alive:
            self.probe_failures += 1
            self.last_probe_error = "钩子的线程或句柄已经没了"
            return False
        before = self.probe_seen
        self.probe_sent += 1
        if not send_vk(HOOK_PROBE_VK):
            # 两种失败必须分开记：SendInput 发不出去的时候重装钩子毫无用处
            # （钩子好好的，是这个进程注不进按键），要是混在一起报警，
            # 用户会看到一个永远在「自动重装」的程序。
            self.probe_failures += 1
            self.probe_send_failures += 1
            self.last_probe_error = "SendInput 没能把自检按键发出去（返回 0）"
            return False
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self.probe_seen > before:
                return True
            time.sleep(0.005)
        self.probe_failures += 1
        self.last_probe_error = f"自检按键发出去 {timeout:.1f} 秒也没回来"
        return False

    def sample_keydowns(self) -> tuple[int, ...]:
        """问系统「有没有键**刚刚**被按下」，返回那些键 —— 完全不注入任何东西。

        为什么需要它：``alive`` 证明不了钩子还在生效（会被静默摘掉），而注入式自检
        在常驻进程里根本发不进系统（见 :data:`HOOK_PROBE_VK` 的说明）。剩下的办法只有
        「拿系统自己的按键状态跟钩子收到的对账」：系统说有人按了键、钩子一个事件都没
        收到，那就是钩子漏了。

        只报**新按下**：按着不放的键（比如打大写时按住的 Shift）在第二次采样里已经在
        ``_down_before`` 里了，不会被当成新按键，所以不会误报。比采样间隔还短的
        轻敲则由 ``GetAsyncKeyState`` 的「自上次调用以来按下过」位补上。

        **已知盲区（实测，要如实告诉用户，别假装一定能抓到）**：2026-10-16 在本机
        （Low 完整性）测到的是**两种结果都有**。单独测的时候：另一个进程在自己的前台
        窗口里 ``SendInput`` 注入 6 次按键，目标进程的低级钩子**收到了全部 12 个事件**，
        而同一个进程里的 ``GetAsyncKeyState`` 一个都没看到（另开一个进程轮询也是 0）——
        注入进的是注入方那条队列。可同一天回归测试期间，有个常驻实例的采样**确实**看见了
        按键（对账连续报漏键并重装了一次），说明它也不是永远看不见。硬件按键按文档是
        **全局**可见的（MSDN：别的程序 ``GetAsyncKeyState`` 时会抢到「刚按下过」这一位），
        所以这一招在正常桌面上该能抓到「钩子被摘掉」；但既然「看得见」不是必然，
        诊断那边的诚实说法就只能是「还没对上过账」，**不是**「钩子正常」。
        """
        now_down: set[int] = set()
        tapped: list[int] = []
        for vk in SCAN_VKS:
            state = int(user32.GetAsyncKeyState(vk))
            if state & 0x8000:
                now_down.add(vk)
                if vk not in self._down_before:
                    tapped.append(vk)
            elif state & 0x0001:
                # 采样的这一瞬间键已经抬起来了，但两次采样之间确实被按过。
                tapped.append(vk)
        first = self._samples == 0
        self._samples += 1
        self._down_before = now_down
        if first:
            return ()  # 第一次只建基线，不报「刚按下」
        return tuple(tapped)

    def stats(self) -> dict:
        """把「键到底进没进来」摊成一份可以直接给用户看的事实。"""
        return {
            "installed": bool(self._hook) or bool(self._thread_id),
            "alive": self.alive,
            "error": self._error,
            "total": self.total,
            "real": self.real,
            "injected": self.injected_count,
            "self_injected": self.self_injected,
            "foreign_injected": self.foreign_injected,
            "effective": self.effective,
            "swallowed": self.swallowed,
            "probes": self.probe_sent,
            "probe_seen": self.probe_seen,
            "probe_failures": self.probe_failures,
            "probe_send_failures": self.probe_send_failures,
            "probe_error": self.last_probe_error,
            "last_real_age": (time.monotonic() - self.last_real_at) if self.last_real_at else None,
            "last_effective_age": ((time.monotonic() - self.last_effective_at)
                                   if self.last_effective_at else None),
        }

    def stop(self):
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None


# --------------------------------------------------------------------------- #
# 文本注入
# --------------------------------------------------------------------------- #
def _kb_input(vk: int, scan: int, flags: int,
              extra_info: int = T_IME_INJECT_TAG) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    # dwExtraInfo 打上我们自己的标记：钩子靠它区分「自己的回灌」和「别人的注入」。
    inp.u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0,
                          dwExtraInfo=extra_info)
    return inp


def _send(inputs: list[INPUT]) -> bool:
    if not inputs:
        return True
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    return sent == n


def send_text(text: str) -> bool:
    """把文本注入当前前台窗口（Unicode 直发，绕过输入法）。"""
    if not text:
        return True
    inputs: list[INPUT] = []
    for ch in text:
        if ch == "\n":
            inputs.append(_kb_input(VK_RETURN, 0, 0))
            inputs.append(_kb_input(VK_RETURN, 0, KEYEVENTF_KEYUP))
            continue
        code = ord(ch)
        if code > 0xFFFF:  # BMP 之外拆成 UTF-16 代理对
            code -= 0x10000
            units = [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
        else:
            units = [code]
        for unit in units:
            inputs.append(_kb_input(0, unit, KEYEVENTF_UNICODE))
            inputs.append(_kb_input(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    ok = True
    # 分批发，避免一次堆太多事件被系统丢弃
    for i in range(0, len(inputs), 128):
        ok = _send(inputs[i : i + 128]) and ok
    return ok


def send_vk(vk: int, extended: bool = False,
            extra_info: int = T_IME_INJECT_TAG) -> bool:
    """注入一个普通虚拟键（含扩展键标注），例如 VK_BACK。

    ``extra_info`` 默认是我们自己的标记，也就是「自家人注入的，钩子会忽略」。
    把它改成别的值，就能**仿真「别的程序（改键器/宏/软键盘）转发的按键」** ——
    这类事件同样带 ``LLKHF_INJECTED``，但输入法必须把它当成真实按键处理，
    否则装了这类软件的机器上会表现为「打字完全没反应」。
    """
    flags = KEYEVENTF_EXTENDEDKEY if extended else 0
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    return _send(
        [
            _kb_input(vk, scan, flags, extra_info),
            _kb_input(vk, scan, flags | KEYEVENTF_KEYUP, extra_info),
        ]
    )


def menu_owner_hwnd() -> int:
    """前台窗口所在线程的 ``hwndMenuOwner``：非 0 就是「有菜单被弹开、正等着吃输入」。

    返回 0 表示没有菜单；返回 -1 表示这一次问不出来（线程已经没了之类）。
    """
    hwnd = int(user32.GetForegroundWindow() or 0)
    if not hwnd:
        return -1
    tid = int(user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None) or 0)
    if not tid:
        return -1
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
        return -1
    return int(gti.hwndMenuOwner or 0)


def cancel_system_menu(owner: int | None = None) -> bool:
    """把已经弹开的系统菜单收掉（``WM_CANCELMODE``），免得它吃掉用户的下一个键。"""
    hwnd = int(owner or 0) or menu_owner_hwnd()
    if hwnd <= 0:
        return False
    return bool(user32.PostMessageW(wintypes.HWND(hwnd), WM_CANCELMODE, 0, 0))


def defuse_system_menu(watch_ms: int = 500) -> bool:
    """挡掉「Alt 单独松开 → 前台窗口的系统菜单被弹开」。

    为什么需要它（真机实测，第 26 轮）：热键的主键是被我们吞掉的，于是系统看到的
    就是「只按了 Ctrl+Alt」。而 Windows 认为「Alt 按住期间没有按过别的键」时，
    会在 Alt 抬起那一刻**激活前台窗口的系统菜单** —— 实测此时
    ``GetGUIThreadInfo().hwndMenuOwner`` 就是前台窗口，紧接着注入的那一个字母
    **没有落进文本框**（被菜单吃掉了）。用户看到的症状是：按完热键接着打字，
    第一个字凭空消失。

    修法分两步：
      1. 在 Alt 松开之前补一个「无害键」：Shift 的按下+抬起不产生字符、不动光标、
         不影响选区，但系统看到「Alt 按住期间还有别的键按下」，通常就不再激活菜单。
         实测这一招能挡住（不补时 6 次里错 3 次，补了之后 6 次全对）。
      2. 但它是**通常**而不是**一定**：菜单是 Alt 抬起之后才可能弹出来的，万一真弹了，
         就盯着一小会儿（``watch_ms`` 毫秒）发现 ``hwndMenuOwner`` 非 0 就用
         ``WM_CANCELMODE`` 把它收掉。这一步是真机 flaky 逼出来的 —— 「多数时候对」
         在用户那里就是「有时候第一个字不见了」。

    带 :data:`T_IME_INJECT_TAG`：自家钩子认得这是回灌，不会把它当成用户敲的 Shift
    （也就不会误触「单击 Shift 切直通」那套判定）。
    """
    ok = bool(send_vk(VK_SHIFT))
    if watch_ms <= 0:
        return ok
    # 菜单是 Alt **抬起之后**才可能弹出来的（那一刻 Shift 早就补完了），所以不能
    # 补完就走：得盯着这一小段时间，真弹出来就收掉。
    cancelled = False
    deadline = time.monotonic() + watch_ms / 1000.0
    while time.monotonic() < deadline:
        owner = menu_owner_hwnd()
        if owner > 0:
            if cancel_system_menu(owner):
                cancelled = True
            break
        time.sleep(0.02)
    return ok or cancelled


# --------------------------------------------------------------------------- #
# 窗口 / 光标
# --------------------------------------------------------------------------- #
def make_noactivate(hwnd: int) -> None:
    """给窗口加 WS_EX_NOACTIVATE|WS_EX_TOOLWINDOW：永不抢焦点、不进 Alt+Tab。"""
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)


def make_noactivate_deep(hwnd: int, levels: int = 3) -> list[int]:
    """沿着父链把 NOACTIVATE 刷到真正的顶层窗口上。

    Tk 的 Toplevel 外面还包了一层 wrapper 窗口，``winfo_id()`` 拿到的是里层，
    真正决定「会不会抢焦点」的是最外层，所以两层都得刷。
    """
    touched: list[int] = []
    cur = int(hwnd or 0)
    for _ in range(levels):
        if not cur:
            break
        make_noactivate(cur)
        touched.append(cur)
        parent = user32.GetParent(wintypes.HWND(cur))
        parent = int(parent or 0)
        if not parent or parent == cur:
            break
        cur = parent
    return touched


def top_level_hwnd(hwnd: int, levels: int = 4) -> int:
    """沿父链走到最外层窗口。Tk 的 ``winfo_id()`` 只是里层 TkChild，
    真正会被 Windows 当成「窗口」的是外面的 TkTopLevel。"""
    cur = int(hwnd or 0)
    for _ in range(levels):
        parent = int(user32.GetParent(wintypes.HWND(cur)) or 0)
        if not parent or parent == cur:
            break
        cur = parent
    return cur


# ShowWindow 的 nCmdShow
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOWNA = 8


def show_no_activate(hwnd: int) -> bool:
    """显示窗口但**绝不**让它变成前台；返回「显示之后它真的可见了吗」。

    不能用 Tk 的 ``deiconify()``：它内部走 ``ShowWindow(SW_SHOW)``，
    会把悬浮窗顶成前台窗口 —— 于是接下来的 SendInput 全注进悬浮窗自己，
    用户的目标程序一个字都收不到（已实测复现）。

    **踩坑**：``ShowWindow`` 的返回值是「调用之前窗口可见吗」，**不是成功与否**
    （隐藏着的窗口第一次显示必然返回 0）。早先这里写成 ``bool(ShowWindow(...))``
    并把假值当失败上报，于是每一次正常的首次显示都被记成「显示失败」，
    状态文件 / 控制面板 / ``--doctor`` 一起报假警。现在改成显示后回查
    ``IsWindowVisible``，语义才是「现在看得见吗」。
    """
    if not hwnd:
        return False
    user32.ShowWindow(wintypes.HWND(int(hwnd)), SW_SHOWNOACTIVATE)
    return is_window_visible(hwnd)


def hide_window(hwnd: int) -> bool:
    """隐藏窗口；返回「隐藏之后它真的不可见了吗」（同样不能看 ShowWindow 的返回值）。"""
    if not hwnd:
        return False
    user32.ShowWindow(wintypes.HWND(int(hwnd)), SW_HIDE)
    return not is_window_visible(hwnd)


def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def window_pid(hwnd: int) -> int:
    """拿到某个窗口属于哪个进程（拿不到返回 0）。"""
    if not hwnd:
        return 0
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid))
    return int(pid.value)


def is_hung(hwnd: int) -> bool:
    """这个窗口的线程是不是「没响应」（Windows 自己在标题栏写「无响应」的那个判断）。

    用途只有一个：**别去碰卡住的窗口**。实测（第 19 轮）：一个进程把前台窗口的
    消息循环停掉（不再泵消息）之后，我们去 ``AttachThreadInput`` 到它、再
    ``SetForegroundWindow``，会**同步阻塞**在系统调用里 —— 常驻实例就卡在启动
    路上，日志停在半句、状态文件永远不出现，用户看到的是「双击了没反应」。

    **但它有个要命的滞后**：系统的判定要等对方 ~5 秒没泵消息才成立，而那 5 秒里
    我们的调用已经卡死了。真机对照（`.tmp/r19_hung_read.py`，A 进程占住前台后彻底
    不泵消息，B 进程隔几秒问一次）：

    ===========  ==================  ============
    时间          A.hwnd              当前前台
    ===========  ==================  ============
    +0s / +4s    False               False
    +8s 起        True                True
    ===========  ==================  ============

    所以它只能当**便宜的预筛**，真正拿主意的是 :func:`responds_within`（自己给
    对方发一条无害消息、带超时，立刻就能知道对方理不理我们）。
    """
    if not hwnd:
        return False
    try:
        return bool(user32.IsHungAppWindow(wintypes.HWND(int(hwnd))))
    except Exception:
        # 老系统 / 调用本身失败：当作「没卡住」，不要因为这个判断把功能挡掉
        return False


def responds_within(hwnd: int, timeout_ms: int = 300) -> bool:
    """给对方窗口线程发一条无害消息（``WM_NULL``），看它在 ``timeout_ms`` 内回不回。

    这是「这个窗口现在还理人吗」的**唯一可靠问法**：``SendMessageTimeout`` 带超时，
    对方不理我们就在超时后返回，绝不把我们自己也搭进去。相比之下
    :func:`is_hung` 要等系统那 ~5 秒才肯改口，等它改口时我们早就卡住了。

    实测依据同 :func:`is_hung` 那段（第 19 轮：常驻实例就是在对方「还没来得及
    被系统标记成没响应」的那几秒里，卡死在 ``set_foreground`` 里）。

    正常窗口处理一条 ``WM_NULL`` 是微秒级的事；只有真卡住的窗口才会耗满
    ``timeout_ms``，所以放在热路径（每次还原前台）也不算贵。
    """
    if not hwnd:
        return False
    try:
        result = ctypes.c_size_t(0)
        rc = user32.SendMessageTimeoutW(wintypes.HWND(int(hwnd)), WM_NULL, 0, 0,
                                        SMTO_ABORTIFHUNG | SMTO_BLOCK,
                                        wintypes.UINT(int(timeout_ms)),
                                        ctypes.byref(result))
        return bool(rc)
    except Exception:
        return False


def is_responsive(hwnd: int, timeout_ms: int = 300) -> bool:
    """这个窗口现在理人吗 —— **判断「能不能碰它」只该调这一个**。

    两个问题合起来问：先看系统标记（:func:`is_hung`，便宜但有 ~5 秒滞后），
    再自己带超时地敲一下门（:func:`responds_within`，立刻见效）。

    单用前者会漏（真机实测：常驻实例就卡在那 5 秒里）；单用后者在窗口已被系统
    标成没响应时也能挡住，但多花一次超时。两个都要。
    """
    if not hwnd:
        return False
    if is_hung(hwnd):
        return False
    return responds_within(hwnd, timeout_ms)


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """窗口在屏幕坐标里的 ``(x, y, 宽, 高)``；拿不到返回 None。"""
    if not hwnd:
        return None
    r = wintypes.RECT()
    if not user32.GetWindowRect(wintypes.HWND(int(hwnd)), ctypes.byref(r)):
        return None
    return int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top)


def is_window_visible(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32.IsWindowVisible(wintypes.HWND(int(hwnd))))


MONITOR_DEFAULTTONULL = 0


def monitor_of_window(hwnd: int) -> int:
    """窗口落在哪块显示器上。

    ``MONITOR_DEFAULTTONULL`` 的语义正好是我们要问的那句：**窗口完全不在任何
    显示器上时返回 0**。悬浮候选窗如果算错坐标（多显示器、光标在负坐标、
    ``GetGUIThreadInfo`` 给了垃圾值），就会落到这种状态 —— 而此时
    ``IsWindowVisible`` 依然是 True，用户那边却是「候选窗根本不出现」。
    """
    if not hwnd:
        return 0
    return int(user32.MonitorFromWindow(wintypes.HWND(int(hwnd)), MONITOR_DEFAULTTONULL) or 0)


def monitor_dpi(x: int | None = None, y: int | None = None,
                hwnd: int = 0) -> int:
    """某块显示器当前的 DPI（96 = 100%，144 = 150%）；问不到返回 0。

    为什么不能只用 :func:`dpi_scale`：那个读的是 ``GetDpiForSystem()``，也就是
    **主屏** 的 DPI。多显示器且两块屏缩放比不同时（比如笔记本 150% + 外接 100%），
    候选窗按主屏的缩放比画出来，搬到另一块屏上字号就会偏大或偏小 ——
    第 47 轮把这件事从「已知限制」变成了可查的事实。

    用 ``GetDpiForMonitor(MDT_EFFECTIVE_DPI)``（Win8.1+）：它给的是当前生效的
    DPI，用户中途改缩放比也能立刻反映，而不是启动时的快照。
    """
    try:
        if x is not None and y is not None:
            hmon = user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)),
                                           MONITOR_DEFAULTTONEAREST)
        elif hwnd:
            hmon = user32.MonitorFromWindow(wintypes.HWND(int(hwnd)),
                                            MONITOR_DEFAULTTONEAREST)
        else:
            hmon = user32.MonitorFromWindow(None, MONITOR_DEFAULTTONEAREST)
        if hmon:
            shcore = ctypes.WinDLL("shcore", use_last_error=True)
            dx = ctypes.c_uint(0)
            dy = ctypes.c_uint(0)
            shcore.GetDpiForMonitor.argtypes = [
                ctypes.c_void_p, ctypes.c_int,
                ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
            ]
            shcore.GetDpiForMonitor.restype = ctypes.c_long
            # MDT_EFFECTIVE_DPI = 0
            if shcore.GetDpiForMonitor(ctypes.c_void_p(int(hmon)), 0,
                                       ctypes.byref(dx), ctypes.byref(dy)) == 0:
                if dx.value > 0:
                    return int(dx.value)
    except Exception:
        pass
    try:
        dpi = int(user32.GetDpiForSystem())
        if dpi > 0:
            return dpi
    except Exception:
        pass
    return 0


def screen_size() -> tuple[int, int]:
    """主显示器分辨率（SM_CXSCREEN / SM_CYSCREEN）。"""
    return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))


def monitor_count() -> int:
    """接了显示器数量（SM_CMONITORS）。多显示器时悬浮窗定位要按光标所在那块算。"""
    return int(user32.GetSystemMetrics(80))


MONITOR_DEFAULTTONEAREST = 2


def _monitor_info(hmon: int) -> MONITORINFO | None:
    if not hmon:
        return None
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(ctypes.c_void_p(int(hmon)), ctypes.byref(mi)):
        return None
    return mi


def monitor_rect(hwnd: int = 0, *, x: int | None = None, y: int | None = None,
                 work: bool = False) -> tuple[int, int, int, int] | None:
    """某块显示器的 ``(x, y, 宽, 高)``；问不到返回 None。

    三种问法，按参数挑一个：

    - ``hwnd``：这块窗口落在哪块显示器上（``MonitorFromWindow``）。
    - ``x, y``：**屏幕坐标这个点**在哪块显示器上（``MonitorFromPoint``）。
      悬浮候选窗定位要用的是这个 —— 光标在副屏右侧时，能翻转到副屏上，
      而不是被主屏宽度顶回主屏。
    - ``work=True``：返回工作区（扣掉任务栏），否则返回整块显示器。

    为什么不用 ``screen_size()``：那个只给主显示器的分辨率，多显示器下
    「右边够了没有」是拿主屏去量副屏，会把窗口算到屏幕外。
    """
    if x is not None and y is not None:
        hmon = user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)),
                                       MONITOR_DEFAULTTONEAREST)
    else:
        if not hwnd:
            return None
        hmon = user32.MonitorFromWindow(wintypes.HWND(int(hwnd)),
                                        MONITOR_DEFAULTTONEAREST)
    mi = _monitor_info(int(hmon or 0))
    if mi is None:
        return None
    r = mi.rcWork if work else mi.rcMonitor
    return int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top)


_DPI_AWARENESS_NAMES = {
    0: "UNAWARE（不感知，系统会拉伸整个窗口）",
    1: "SYSTEM_AWARE（按主屏 DPI 绘制）",
    2: "PER_MONITOR_AWARE（每块屏各自缩放）",
}


def dpi_awareness() -> str:
    """本进程当前生效的 DPI 感知级别（人话描述）。读不到就返回"未知"。"""
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        value = ctypes.c_int(-1)
        shcore.GetProcessDpiAwareness.argtypes = [ctypes.c_void_p,
                                                  ctypes.POINTER(ctypes.c_int)]
        shcore.GetProcessDpiAwareness.restype = ctypes.c_long
        if shcore.GetProcessDpiAwareness(None, ctypes.byref(value)) == 0:
            return _DPI_AWARENESS_NAMES.get(value.value, f"未知({value.value})")
    except Exception:
        pass
    return "未知"


# 具名互斥体的句柄必须一直活着，所以存在模块级；一被 GC 回收锁就没了。
_MUTEX_HANDLES: dict[str, int] = {}
_ERROR_ALREADY_EXISTS = 183


def single_instance(name: str = "translate-ime") -> bool:
    """抢一个具名互斥体，用来保证同一台机器上只跑一个实例。

    返回 True = 你是第一个（拿到锁了）；False = 已经有一个在跑。

    为什么需要：常驻输入法装两次钩子会互相打架 —— 两个进程都想吞同一个键，
    表现是「有时候打字出候选、有时候不出」，极难排查。
    """
    if name in _MUTEX_HANDLES:
        return True  # 本进程内重复调用
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, True, f"Local\\{name}")
    if not handle:
        return True  # 建不出来就不拦着用户用
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _MUTEX_HANDLES[name] = int(handle)
    return True


def instance_exists(name: str = "translate-ime") -> bool:
    """只**探测**有没有别的实例在跑，自己不占锁。

    和 :func:`single_instance` 的区别很关键：那个是「我要用，锁归我了」，
    锁会一直挂到进程退出；这个是「帮我看一眼有没有人在用」，拿到句柄立刻
    关掉。所以它**只能用在启动器身上** —— 如果用它在父进程里拦重复启动，
    父进程会把锁带走，子进程反而永远起不来。
    """
    if name in _MUTEX_HANDLES:
        return True  # 本进程已经占着锁了，那当然算「有实例」
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, True, f"Local\\{name}")
    if not handle:
        return False
    exists = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
    kernel32.CloseHandle(ctypes.c_void_p(handle))
    return exists


# 完整性级别 RID -> 人能看懂的名字。UIPI 就是按这个值决定放不放行的。
INTEGRITY_NAMES = {
    0x0000: "Untrusted（不可信）",
    0x1000: "Low（低）",
    0x2000: "Medium（中，普通用户程序的正常值）",
    0x2100: "Medium Plus（中+）",
    0x3000: "High（高，管理员提权后）",
    0x4000: "System（系统）",
}


def integrity_level() -> tuple[int, str]:
    """当前进程的完整性级别，返回 ``(RID, 可读名)``；读不到返回 ``(-1, "未知")``。

    输入法为什么要在意这个：Windows 的 UIPI 会拦下**完整性级别低于目标窗口所属
    进程**的跨进程消息。托盘图标（``Shell_NotifyIcon``）和写 ``HKCU`` 注册表都吃
    这条规矩，而且在被降权的沙箱里跑时两者都只报 ``ERROR_ACCESS_DENIED`` ——
    光看错误码完全看不出是权限问题，所以得主动把级别报出来。
    """
    try:
        advapi32 = _advapi32()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008,
                                         ctypes.byref(token)):
            return -1, "未知"
        try:
            return _level_from_token(advapi32, token)
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(token.value or 0))
    except Exception:
        return -1, "未知"


def _advapi32():
    """装好 advapi32 里令牌相关函数的签名（不装的话 64 位下指针会被截断）。"""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    return advapi32


def _level_from_token(advapi32, token) -> tuple[int, str]:
    """从一个（已打开的）令牌里读出完整性级别 RID 与可读名。"""
    buf = ctypes.create_string_buffer(1024)
    need = wintypes.DWORD()
    if not advapi32.GetTokenInformation(token, 25, buf, 1024,  # TokenIntegrityLevel
                                        ctypes.byref(need)):
        return -1, "未知"
    # TOKEN_MANDATORY_LABEL = { SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes; } }
    sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
    count = advapi32.GetSidSubAuthorityCount(sid)[0]
    rid = advapi32.GetSidSubAuthority(sid, count - 1)[0]
    return int(rid), INTEGRITY_NAMES.get(int(rid), f"未知(0x{int(rid):04X})")


def integrity_level_of(pid: int) -> tuple[int, str] | None:
    """按 pid 读**那个进程**的完整性级别；读不到返回 ``None``（意思只是「不知道」）。

    只有一个用途：上屏之前判断 UIPI 会不会拦下我们送过去的模拟按键
    （见 :func:`can_inject_into`）。读不到时千万别当成「级别高」——
    那会把正常窗口的输入也掐掉。
    """
    try:
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        # 用 QUERY_LIMITED_INFORMATION：它对更高完整性级别的进程也打得开
        h = kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return None
        try:
            advapi32 = _advapi32()
            token = wintypes.HANDLE()
            if not advapi32.OpenProcessToken(h, 0x0008, ctypes.byref(token)):
                return None
            try:
                rid, name = _level_from_token(advapi32, token)
            finally:
                kernel32.CloseHandle(ctypes.c_void_p(token.value or 0))
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(h))
        return None if rid < 0 else (rid, name)
    except Exception:
        return None


def inject_blocked_reason(pid: int) -> str:
    """UIPI 会不会拦下我们送给 ``pid`` 那个进程的模拟按键；能送就返回空串。

    为什么需要它：``SendInput`` 被 UIPI 拦下时**返回值照样是成功的**
    （第 13 轮实测：按键根本没进系统，``SendInput`` 仍返回完整条数），所以调用方
    从返回值上完全看不出「送不进去」。唯一能提前知道的线索，就是目标进程的权限。

    判据来自本机实测，和直觉相反的一条是**「读不到」要当成「拦」**：

    * 读得出来令牌的进程，实测全都是**级别不高于我们**的（Edge 的渲染进程是
      Low / Untrusted，我们自己起的子进程也是 Low）—— 这些能送进去。
    * 读不出来的，正是**级别比我们高**的（Low 进程去读 Edge 浏览器进程的令牌就
      是失败）以及受保护进程 —— 这两类本来就送不进去。

    唯一的例外：**连我们自己的级别都读不到**时没有任何判断依据，这时放行
    （试一次没有副作用：送不进去就是什么都没发生）。
    """
    mine, mine_name = integrity_level()
    if mine < 0:
        return ""
    theirs = integrity_level_of(pid)
    if theirs is None:
        return f"拿不到那个窗口的权限信息（本程序是 {mine_name}，多半它比本程序权限高）"
    if theirs[0] > mine:
        return f"那个窗口的权限比本程序高（{theirs[1]} > {mine_name}）"
    return ""


def can_inject_into(pid: int) -> bool:
    """``inject_blocked_reason(pid) == ""`` 的布尔写法（给调用方写好读的代码）。"""
    return not inject_blocked_reason(pid)


def process_name(pid: int) -> str:
    """按 pid 取可执行文件名。拿不到就返回空串（诊断用，失败不该抛）。"""
    try:
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        # PROCESS_QUERY_LIMITED_INFORMATION 就够读路径，且对高权限进程也能拿到
        h = kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return ""


def _root_hwnd(hwnd: int) -> int:
    """取一个窗口的最外层窗口（顶层框架）。取不到就原样返回。"""
    if not hwnd:
        return 0
    try:
        root = int(user32.GetAncestor(wintypes.HWND(int(hwnd)), GA_ROOT) or 0)
    except Exception:
        root = 0
    return root or int(hwnd)


def same_window(a: int, b: int) -> bool:
    """两个句柄说的是不是同一个窗口。

    不能只比数字：Tk 会额外造包装窗口，Windows 也可能把**同一个**
    顶层窗口的另一个句柄（框架而不是客户区）设成前台 —— 这时裸比句柄
    会得出「失败」的假结论（实测踩过：前台明明已经是我们了，
    ``set_foreground`` 却返回 False，于是 overlay 打出一句
    「前台窗口还原失败」的假警报）。
    """
    a, b = int(a or 0), int(b or 0)
    if not a or not b:
        return False
    if a == b:
        return True
    return _root_hwnd(a) == _root_hwnd(b)


def set_foreground(hwnd: int) -> bool:
    """把前台窗口设成 hwnd，并且**真的确认它生效了**。

    裸调 ``SetForegroundWindow`` 很容易被 Windows 的前台锁定拒绝：它不报错、
    只是不生效（顶多任务栏闪一下）。更麻烦的是**它是间歇性的** —— 同一个调用
    在这里可能这次成功、下次失败，所以「调用没抛异常」完全不能当作成功。

    标准绕法是先把本线程的输入队列 attach 到当前前台线程，这个时候我们的线程
    在系统看来「就是」前台线程，``SetForegroundWindow`` 才会被放行；设完再 detach。

    确认生效用的是 :func:`same_window`（比顶层窗口，不比裸句柄）。

    动手之前会先问一句「这窗口还理我们吗」：**卡住的窗口一个都别碰** —— 实测把一个
    进程的消息循环停掉之后，attach 到它、或把前台设到它身上都会**同步阻塞**，常驻
    实例就卡死在启动路上（日志停在半句、状态文件永远不出现，用户看到的是「双击了
    没反应」）。这种情况直接返回 False，让调用方照「没还成」处理。

    问法有两道，缺一不可：
    :func:`is_hung` 便宜但**要等系统那 ~5 秒才改口**，而我们会卡死在那 5 秒里面；
    :func:`responds_within` 自己带超时地敲一下门，立刻就知道对方理不理我们。
    把两者合起来用的就是 :func:`is_responsive` —— 调用方判「这窗口能不能碰」时
    直接用它，别自己挑一个。
    """
    target = int(hwnd)
    if not target:
        return False
    if same_window(foreground_hwnd(), target):
        return True

    fg = user32.GetForegroundWindow()
    # fg 为 0 = 当前压根没有前台窗口，那就没有「要 attach 的邻居」、也没有要避开的
    # 卡住窗口，照常往下走；不为 0 时两边都得先过「现在理人吗」这一关。
    if fg and not is_responsive(fg):
        return False
    if not is_responsive(target):
        return False
    tid_me = kernel32.GetCurrentThreadId()
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0

    for _ in range(3):
        attached = False
        if tid_fg and tid_fg != tid_me:
            attached = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
        try:
            user32.BringWindowToTop(wintypes.HWND(target))
            user32.SetForegroundWindow(wintypes.HWND(target))
        finally:
            if attached:
                user32.AttachThreadInput(tid_me, tid_fg, False)
        if same_window(foreground_hwnd(), target):
            return True
    return False


def caret_screen_pos() -> tuple[int, int] | None:
    """取前台窗口输入光标的屏幕坐标（很多程序不提供 caret，返回 None）。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    if not tid:
        return None
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
        return None
    if not gti.hwndCaret:
        return None
    pt = POINT(gti.rcCaret.left, gti.rcCaret.bottom)
    if not user32.ClientToScreen(gti.hwndCaret, ctypes.byref(pt)):
        return None
    return int(pt.x), int(pt.y)


def cursor_pos() -> tuple[int, int]:
    pt = POINT()
    if user32.GetCursorPos(ctypes.byref(pt)):
        return int(pt.x), int(pt.y)
    return 0, 0


# --------------------------------------------------------------------------- #
# DPI
# --------------------------------------------------------------------------- #
_DPI_AWARE_SET = False


def dpi_scale() -> float:
    """当前系统缩放比（1.0 = 96 DPI = 100%）。"""
    try:
        dpi = int(user32.GetDpiForSystem())
        if dpi > 0:
            return max(1.0, dpi / 96.0)
    except Exception:
        pass
    try:
        hdc = user32.GetDC(0)
        dpi = int(gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSX
        user32.ReleaseDC(0, hdc)
        if dpi > 0:
            return max(1.0, dpi / 96.0)
    except Exception:
        pass
    return 1.0


def enable_dpi_awareness() -> float:
    """让本进程自己处理 DPI 缩放，返回生效后的缩放比。

    **必须在创建任何窗口之前调用**（Tk 的 ``tk.Tk()`` 也算）。

    不调用的话 Windows 会把整个进程当成 DPI 不感知，把窗口位图整体拉伸：
    在本机 2560x1440 @150% 上实测，``winfo_screenwidth()`` 会谎报 1707、
    候选窗的字是拉伸出来的（发虚），而且屏幕坐标与实际像素差 1.5 倍。
    开启之后窗口按物理像素绘制，字是清晰的，代价是配置里的字号 / 宽度
    需要自己乘上缩放比（``Overlay`` 会做这件事）。

    **一旦有谁先问过 ``GetSystemMetrics`` / ``GetDC`` 之类，Windows 就把
    本进程的 DPI 感知锁死成 UNAWARE，这里再调也会失败**（返回
    ``E_ACCESSDENIED``）。所以这个函数会去核对真实生效的级别，而不是
    调完就当成功了 —— 否则 ``Overlay`` 会拿着 scale=1.0 去画，字是糊的，
    而日志里一个字都不会提。
    """
    global _DPI_AWARE_SET
    current = dpi_awareness()
    if not _DPI_AWARE_SET and current != "未知" and "UNAWARE" not in current:
        _DPI_AWARE_SET = True      # 别的地方（比如 Tk）已经设过了
    if not _DPI_AWARE_SET:
        for attempt in (
            # PROCESS_PER_MONITOR_DPI_AWARE = 2（Win8.1+）
            lambda: ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2) == 0,
            # PER_MONITOR_AWARE_V2 = -4（Win10 1703+）
            lambda: user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)) != 0,
            lambda: bool(user32.SetProcessDPIAware()),
        ):
            try:
                if attempt():
                    _DPI_AWARE_SET = True
                    break
            except Exception:
                continue
    return dpi_scale()


def installed_input_layouts() -> list[tuple[str, str]]:
    """读出本机装了哪些键盘布局 / 输入法，返回 ``[(布局 id, 说给人听的名字)]``。

    为什么需要：本工具**不是** Windows 输入法 —— 没有 TSF 文本服务、也没注册
    键盘布局，所以语言栏和 Win+Space 的列表里永远不会有它。可用户的第一反应
    恰恰是「我怎么选它？」，然后发现语言栏里只有微软拼音，就以为没装好。

    读 ``HKCU\\Keyboard Layout\\Preload`` 只是为了让诊断能把这句假话补全：
    「系统里装着 微软拼音，它仍然在，但不影响 —— 本工具靠钩子先接管按键」。

    读不到（权限、注册表被删、非 Windows）就返回空列表；调用方必须按「读不出来」
    处理，而不是拿这个结果当「一个输入法都没装」。
    """
    import winreg

    names = {
        "00000804": "中文(简体) · 微软拼音",
        "00000409": "英语(美国)",
        "00000411": "日语",
        "00000412": "韩语",
        "00000404": "中文(繁体) · 微软注音",
    }
    out: list[tuple[str, str]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Keyboard Layout\Preload") as key:
            i = 0
            while True:
                try:
                    _, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                ident = str(value or "").strip()
                if not ident:
                    continue
                out.append((ident, names.get(ident, f"键盘布局 {ident}")))
    except OSError:
        return []
    return out
