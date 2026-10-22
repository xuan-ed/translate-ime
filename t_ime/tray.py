# -*- coding: utf-8 -*-
"""系统托盘图标（纯 ctypes，不依赖 pywin32 / pystray）。

设计要点（都是被 Win32 逼出来的，别改）：

1. **自己开一条线程**跑「隐藏窗口 + 消息泵」。托盘回调（``WM_APP+1``）、
   ``TrackPopupMenu`` 全都只在**这条线程**上发生。
   ``TrackPopupMenu`` 会阻塞到菜单关掉 —— 所以它绝不能出现在 Tk 主线程上。
2. **主线程不碰任何窗口**。``set_tooltip`` / ``set_icon`` / ``set_item``
   只写自己那把锁保护的状态，然后 ``PostMessageW`` 一个自定义消息让托盘线程
   去真正干活（`Shell_NotifyIconW` 必须在拥有该 hwnd 的线程上调）。
3. **不调用回调**。菜单项被点中就把 ``item_id`` 丢进 ``action_queue``，
   Tk 侧照自己的 ``after`` 轮询去取 —— 跨线程直接调 Tk 会炸。
4. ``NOTIFYICONDATAW`` 的 ``cbSize`` 是这套 API 最经典的坑：结构体里
   ``uTimeout`` / ``uVersion`` 是 union，很多手写定义会把 union 后面的
   ``szInfo`` 放错偏移。这里的 ``GUID`` / ``HICON`` 字段和 union 都按
   Windows SDK 原样声明，``sizeof`` 实测 976（0x3D0）。见 ``NotifyIconDataW``。

线程模型::

    主线程 (Tk mainloop)          托盘线程
    ----------------------        ----------------------------
    start()  --> 建线程 -------->  RegisterClassExW / CreateWindowExW
                                   Shell_NotifyIconW(NIM_ADD)
    set_*()  --> PostMessageW ---> 改图标 / 气泡 / 菜单状态
    stop()   --> PostMessageW ---> 消息泵收到退出消息 -> NIM_DELETE
                    (WM_QUIT/退出消息)   DestroyWindow -> 线程结束
"""
from __future__ import annotations

import ctypes
import queue
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Union

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

PathLike = Union[str, Path]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

WM_APP = 0x8000
WM_TRAY_CALLBACK = WM_APP + 1          # Shell_NotifyIconW 的回调消息（约定死的）

# 托盘线程自己的私有消息（只有本模块用）
_WM_SET_TOOLTIP = WM_APP + 2
_WM_SET_ICON = WM_APP + 3
_WM_NOTIFY = WM_APP + 4
_WM_QUERY_STOP = WM_APP + 5
_WM_SET_ITEM = WM_APP + 6
_WM_SYNC_MENU = WM_APP + 7

# 托盘「被点了一下」的通知码（wParam 高位，uVersion>=4 才发，所以我们要设 v4）
NIN_SELECT = 0x0400
NIN_KEYSELECT = 0x0401
NIN_BALLOONSHOW = 0x0402
NIN_BALLOONHIDE = 0x0403
NIN_BALLOONTIMEOUT = 0x0404
NIN_BALLOONUSERCLICK = 0x0405

NOTIFYICON_VERSION_4 = 4

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETFOCUS = 0x00000003
NIM_SETVERSION = 0x00000004

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_STATE = 0x00000008
NIF_INFO = 0x00000010
NIF_GUID = 0x00000020
NIF_REALTIME = 0x00000040
NIF_SHOWTIP = 0x00000080

NIS_HIDDEN = 0x00000001
NIS_SHAREDICON = 0x00000002

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
LR_SHARED = 0x00008000
IDI_APPLICATION = 32512

MF_STRING = 0x00000000
MF_POPUP = 0x00000010
MF_SEPARATOR = 0x00000800
MF_CHECKED = 0x00000008
MF_UNCHECKED = 0x00000000
MF_GRAYED = 0x00000001
MF_DISABLED = 0x00000002
MF_DEFAULT = 0x00001000

TPM_LEFTALIGN = 0x0000
TPM_CENTERALIGN = 0x0004
TPM_RIGHTALIGN = 0x0008
TPM_BOTTOMALIGN = 0x0020
TPM_LEFTBUTTON = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

SW_HIDE = 0

ERROR_CLASS_ALREADY_EXISTS = 1410


# --------------------------------------------------------------------------- #
# 结构体（照 Windows SDK 原样搬）
# --------------------------------------------------------------------------- #
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _TIMEOUTVERSIONUNION(ctypes.Union):
    """``NOTIFYICONDATAW`` 里的 ``uTimeout`` / ``uVersion``。

    这两个字段是 union，**不是**两个独立成员。手写结构体时如果写成两行，
    后面 ``szInfo`` 的偏移就会错 4 字节，``NIF_INFO``（气泡通知）会静默失效。
    """
    _fields_ = [("uTimeout", wintypes.UINT), ("uVersion", wintypes.UINT)]


class NotifyIconDataW(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("u", _TIMEOUTVERSIONUNION),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

# --------------------------------------------------------------------------- #
# 函数签名
# --------------------------------------------------------------------------- #
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.WORD
user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
user32.UnregisterClassW.restype = wintypes.BOOL
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadIconW.restype = wintypes.HICON
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.DestroyIcon.restype = wintypes.BOOL
user32.CreatePopupMenu.argtypes = []
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyMenu.restype = wintypes.BOOL
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.AppendMenuW.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
]
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p

shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NotifyIconDataW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def load_icon_handle(path: PathLike | None, *, verbose: bool = False) -> tuple[int, str]:
    """加载图标句柄。

    返回 ``(hIcon, 说明)``。优先 ``LoadImageW(..., IMAGE_ICON, 16, 16, LR_LOADFROMFILE)``，
    失败回退 ``LoadIconW(None, IDI_APPLICATION)``。两个都失败会返回 ``(0, 中文错误)``。
    """
    if path:
        p = Path(path)
        if not p.is_file():
            msg = f"图标文件不存在：{p}"
            print(f"[tray] {msg}，回退默认图标", file=sys.stderr)
        else:
            h = user32.LoadImageW(None, str(p), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
            if h:
                if verbose:
                    print(f"[tray] 已加载图标 {p}")
                return int(h), f"LoadImageW({p.name})"
            err = ctypes.get_last_error()
            print(
                f"[tray] LoadImageW 失败（GetLastError={err}）：{p}，回退默认图标",
                file=sys.stderr,
            )
    h = user32.LoadIconW(None, wintypes.LPCWSTR(IDI_APPLICATION))
    if h:
        if verbose:
            print("[tray] 使用系统默认图标 IDI_APPLICATION")
        return int(h), "LoadIconW(IDI_APPLICATION)"
    err = ctypes.get_last_error()
    return 0, f"LoadIconW(IDI_APPLICATION) 也失败，GetLastError={err}"


class _Item:
    """一条右键菜单项的状态。``checked=None`` 表示普通项（不可勾选）。"""

    __slots__ = ("item_id", "label", "checked", "enabled", "separator_before", "default")

    def __init__(self, item_id: str, label: str, checked, enabled, separator_before, default):
        self.item_id = item_id
        self.label = label
        self.checked = checked
        self.enabled = enabled
        self.separator_before = separator_before
        self.default = default


# --------------------------------------------------------------------------- #
# TrayIcon
# --------------------------------------------------------------------------- #
class TrayIcon:
    """一个系统托盘图标 + 右键菜单，跑在自己的线程上。

    典型用法（Tk 主线程）::

        q: queue.Queue[str] = queue.Queue()
        tray = TrayIcon("translate-ime", Path(base) / "t_ime/assets/tray.ico", q)
        tray.add_item("toggle_enabled", "启用输入法", checked=True)
        tray.add_item("quit", "退出", separator_before=True)
        tray.start()                 # 非阻塞，返回时图标已出现
        # Tk 的 after 循环里： while not q.empty(): item_id = q.get_nowait()
        tray.stop()                  # 可重复调用

    ``action_queue`` 里可能出现两种东西：

    * ``"__activate__"`` —— 左键单击 / 双击（拿来做「弹出候选窗」或「闪一下状态」）
    * 菜单项的 ``item_id`` —— 某条菜单被点中
    """

    def __init__(
        self,
        tooltip: str = "translate-ime",
        icon_path: PathLike | None = None,
        action_queue: "queue.Queue[str] | None" = None,
        *,
        verbose: bool = False,
    ) -> None:
        self._tooltip = str(tooltip)
        self._icon_path: Path | None = Path(icon_path) if icon_path else None
        self._queue = action_queue
        self._verbose = bool(verbose)

        # 菜单状态表：主线程写、托盘线程读，用一把锁保护
        self._lock = threading.Lock()
        self._items: list[_Item] = []
        self._seq = 0

        # 托盘线程会把「期望状态」翻成「已生效状态」，失败不会静默：错误写这里
        self._last_error: str | None = None
        self._tray_added = False
        # NIM_SETVERSION(4) 是否真的成功 —— 决定 WM_APP+1 回调是 v4 还是老式报文
        self._notify_version_ok = False

        self._thread: threading.Thread | None = None
        self._hwnd = 0
        self._hicon = 0
        self._icon_owned = False          # 自己 LoadImage 出来的要自己 DestroyIcon
        self._nid: NotifyIconDataW | None = None
        self._wndproc = None              # 必须一直持有，否则 ctypes 回调被 GC 直接崩进程
        self._ready = threading.Event()
        self._done = threading.Event()
        self._started = False
        # 类名唯一：同进程里多个 TrayIcon 实例不能撞窗口类
        self._class_name = f"T_imeTrayWnd_{id(self):x}"

        if verbose:
            print(f"[tray] NOTIFYICONDATAW 大小 = {ctypes.sizeof(NotifyIconDataW)}")

    # -- 只读属性 --------------------------------------------------------- #
    @property
    def hwnd(self) -> int:
        """隐藏窗口句柄（还没 start 时为 0）。测试直接往它 PostMessageW 模拟托盘消息。"""
        return int(self._hwnd)

    @property
    def running(self) -> bool:
        return bool(self._started and self._thread is not None and self._thread.is_alive())

    @property
    def tray_added(self) -> bool:
        """``Shell_NotifyIconW(NIM_ADD)`` 是否真的成功了（explorer 没起来时会 False）。"""
        return bool(self._tray_added)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # -- 菜单状态（主线程安全） ------------------------------------------- #
    def add_item(
        self,
        item_id: str,
        label: str,
        *,
        checked: bool | None = None,
        enabled: bool = True,
        separator_before: bool = False,
        default: bool = False,
    ) -> None:
        """加一个右键菜单项。

        ``checked=None`` 普通项；``True`` / ``False`` 可勾选项。
        点击时不调回调，只把 ``item_id`` 字符串放进 ``action_queue``。
        """
        item_id = str(item_id)
        with self._lock:
            for it in self._items:
                if it.item_id == item_id:
                    raise ValueError(f"菜单项 id 重复：{item_id!r}")
            self._items.append(
                _Item(item_id, str(label), checked, bool(enabled), bool(separator_before), bool(default))
            )
        self._sync_menu()

    def set_item(
        self,
        item_id: str,
        *,
        label: str | None = None,
        checked: bool | None = None,
        enabled: bool | None = None,
    ) -> None:
        """改菜单项状态；托盘在跑的话，下次弹出菜单就反映新状态。

        三个参数都是「传 ``None`` = 这一项不改」：
        ``checked`` 只有真的是 ``bool`` 才会写进状态表，所以
        ``set_item("x", checked=False)`` 能把勾去掉（``False is not None``）。
        """
        item_id = str(item_id)
        with self._lock:
            for it in self._items:
                if it.item_id != item_id:
                    continue
                if label is not None:
                    it.label = str(label)
                if isinstance(checked, bool):
                    it.checked = checked
                if isinstance(enabled, bool):
                    it.enabled = enabled
                break
            else:
                raise KeyError(f"没有这个菜单项：{item_id!r}")
        self._sync_menu()

    def clear_items(self) -> None:
        with self._lock:
            self._items.clear()
        self._sync_menu()

    # -- 图标 / 提示 / 气泡 ----------------------------------------------- #
    def set_tooltip(self, text: str) -> None:
        """改悬停提示。**不跨线程直接调 Shell_NotifyIconW**，投递到托盘线程做。"""
        self._tooltip = str(text)
        if self.running:
            user32.PostMessageW(wintypes.HWND(self._hwnd), _WM_SET_TOOLTIP, 0, 0)

    def set_icon(self, icon_path: PathLike) -> None:
        self._icon_path = Path(icon_path)
        if self.running:
            user32.PostMessageW(wintypes.HWND(self._hwnd), _WM_SET_ICON, 0, 0)

    def notify(self, title: str, text: str) -> None:
        """气泡通知（``NIF_INFO``）。失败不抛异常。"""
        self._notify_title = str(title)
        self._notify_text = str(text)
        if self.running:
            user32.PostMessageW(wintypes.HWND(self._hwnd), _WM_NOTIFY, 0, 0)

    # -- 生命周期 --------------------------------------------------------- #
    def start(self) -> bool:
        """起专用线程：注册窗口类 -> 建隐藏窗口 -> 装托盘 -> 跑消息泵。

        **非阻塞**：返回时图标已经出现（或明确知道失败）。

        返回值只回答一个问题：``Shell_NotifyIconW(NIM_ADD)`` 到底成没成 ——
        ``True`` 表示图标真的出现了，``False`` 表示没装上，原因在 :attr:`last_error`
        里（explorer 没起来、进程完整性级别低于 explorer 时 UIPI 会拒掉，等等）。

        注意 ``False`` **不代表托盘线程死了**：线程照样在跑（:attr:`running` 为
        True），explorer 重启时它会自己把图标重装回去。所以别因为返回值是 False
        就去 stop 它。
        """
        if self.running:
            return bool(self._tray_added)
        self._join_dead_thread()
        with self._lock:
            self._ready.clear()
            self._done.clear()
            self._last_error = None
            self._tray_added = False
            self._hwnd = 0
            self._hicon = 0
            self._nid = None
            self._started = True
        self._thread = threading.Thread(target=self._run, name="t-ime-tray", daemon=True)
        self._thread.start()
        if not self._ready.wait(5.0):
            self._last_error = "启动托盘线程超时（5s 内没完成初始化）"
            self._started = False
            return False
        return bool(self._tray_added)

    def stop(self, timeout: float = 3.0) -> None:
        """移除图标、销毁窗口、结束线程。可重复调用。"""
        with self._lock:
            self._started = False
            hwnd = self._hwnd
        if self._thread is None:
            return
        if hwnd:
            # 让托盘线程自己 NIM_DELETE + 退消息泵（hwnd 没建出来时它会自己到点退出）
            user32.PostMessageW(wintypes.HWND(hwnd), _WM_QUERY_STOP, 0, 0)
        if not self._done.wait(timeout):
            self._last_error = f"托盘线程 {timeout}s 内没结束"
            if self._verbose:
                print(f"[tray] {self._last_error}", file=sys.stderr)
        self._join_dead_thread()
        if self._thread is not None and self._thread.is_alive():
            # 兜底：直接砸窗口，逼消息泵退出
            if hwnd:
                user32.PostMessageW(wintypes.HWND(hwnd), WM_CLOSE, 0, 0)
            self._thread.join(timeout=1.0)
        if self._verbose and self._thread is None:
            print("[tray] 已停止")

    def _join_dead_thread(self) -> None:
        t = self._thread
        if t is not None and not t.is_alive():
            t.join(timeout=0.5)
            self._thread = None
            with self._lock:
                self._hwnd = 0

    # -- 内部：投递菜单同步 ----------------------------------------------- #
    def _sync_menu(self) -> None:
        """菜单表变了就敲一下托盘线程（它只在弹菜单前读表，所以这步只是保险）。"""
        if self.running:
            hwnd = self._hwnd
            if hwnd:
                user32.PostMessageW(wintypes.HWND(hwnd), _WM_SYNC_MENU, 0, 0)

    # -- 内部：托盘线程 --------------------------------------------------- #
    def _run(self) -> None:
        """托盘线程主体。任何一步失败都会把中文错误写进 self._last_error。"""
        hinst = kernel32.GetModuleHandleW(None)
        hwnd = 0
        registered = False
        try:
            # 1) 注册窗口类。同名类已存在（上一轮没反注册干净）不算错，直接复用。
            self._wndproc = WNDPROC(self._wnd_proc)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.style = 0
            wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = hinst
            wc.hIcon = None
            wc.hCursor = None
            wc.hbrBackground = None
            wc.lpszMenuName = None
            wc.lpszClassName = self._class_name
            wc.hIconSm = None

            ctypes.set_last_error(0)
            atom = user32.RegisterClassExW(ctypes.byref(wc))
            if not atom:
                err = ctypes.get_last_error()
                if err != ERROR_CLASS_ALREADY_EXISTS:
                    self._last_error = (
                        f"RegisterClassExW 失败，GetLastError={err}"
                        f"（窗口类 {self._class_name!r}）"
                    )
                    return
                if self._verbose:
                    print("[tray] 窗口类已存在，复用")
            registered = True

            # 2) 建隐藏窗口。WS_EX_TOOLWINDOW：不进 Alt+Tab。
            hwnd = user32.CreateWindowExW(
                0x00000080,            # WS_EX_TOOLWINDOW
                self._class_name,
                "translate-ime tray",
                0,                     # WS_OVERLAPPED，从不 ShowWindow 所以永远不可见
                0, 0, 0, 0,
                None, None, hinst, None,
            )
            if not hwnd:
                err = ctypes.get_last_error()
                self._last_error = f"CreateWindowExW 失败，GetLastError={err}（建不出托盘隐藏窗口）"
                return
            with self._lock:
                self._hwnd = int(hwnd)

            # 3) 载图标 + 装托盘
            self._install_tray()

            # TaskbarCreated：explorer 重启后要重新 NIM_ADD，否则图标永远消失
            self._taskbar_created_msg = user32.RegisterWindowMessageW("TaskbarCreated")
            if not self._taskbar_created_msg:
                self._taskbar_created_msg = 0

            # 4) 消息泵（只有它退出，线程才结束）
            self._ready.set()
            msg = wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:  # 托盘线程绝不能把异常丢到 stderr 之外
            self._last_error = f"托盘线程异常：{exc!r}"
            print(f"[tray] {self._last_error}", file=sys.stderr)
        finally:
            # 线程自己退出（或上面任何一步 return）时的收尾，全部在本线程上做
            if hwnd:
                self._remove_tray()
                self._destroy_icon()
                user32.DestroyWindow(hwnd)
            if registered:
                for _ in range(200):
                    if user32.UnregisterClassW(self._class_name, hinst):
                        break
                    # 本线程的消息队列要排空一次，否则 WM_DESTROY 还没处理完
                    m = wintypes.MSG()
                    while user32.PeekMessageW(ctypes.byref(m), None, 0, 0, 1):  # PM_REMOVE
                        user32.DispatchMessageW(ctypes.byref(m))
                    threading.Event().wait(0.005)
            with self._lock:
                self._hwnd = 0
                self._tray_added = False
            self._ready.set()   # 初始化失败时也别让 start() 干等
            self._done.set()
            if self._verbose:
                print("[tray] 托盘线程已退出")

    # -- 内部：托盘本体 --------------------------------------------------- #
    def _fill_nid(self, flags: int, *, tip: str | None = None,
                  info: tuple[str, str] | None = None) -> NotifyIconDataW:
        nid = NotifyIconDataW()
        nid.cbSize = ctypes.sizeof(NotifyIconDataW)
        nid.hWnd = wintypes.HWND(self._hwnd)
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAY_CALLBACK
        if flags & NIF_ICON:
            nid.hIcon = wintypes.HICON(self._hicon)
        if flags & NIF_TIP:
            nid.szTip = (tip if tip is not None else self._tooltip)[:127]
        if flags & NIF_INFO and info is not None:
            title, text = info
            nid.szInfo = text[:255]
            nid.szInfoTitle = title[:63]
            nid.dwInfoFlags = 0x00000001   # NIIF_INFO
            nid.uTimeout = 10000           # 10s（Vista+ 由系统决定，这里只是兼容旧版）
        return nid

    def _install_tray(self) -> None:
        """载图标 + NIM_ADD + NIM_SETVERSION。失败只记错误，不阻断消息泵。"""
        hicon, how = load_icon_handle(self._icon_path, verbose=self._verbose)
        if not hicon:
            self._last_error = f"图标加载失败：{how}"
            print(f"[tray] {self._last_error}", file=sys.stderr)
        self._hicon = hicon
        self._icon_owned = how.startswith("LoadImageW")

        nid = self._fill_nid(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        self._nid = nid
        ctypes.set_last_error(0)
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            err = ctypes.get_last_error()
            self._last_error = (
                f"Shell_NotifyIconW(NIM_ADD) 失败，GetLastError={err}"
                + ("（ACCESS_DENIED：shell 拒绝了这次调用。本机实测最常见的原因是"
                   "**本进程的完整性级别低于 explorer.exe**（UIPI 会拦下低完整性进程"
                   "发给托盘窗口的消息），其次是 explorer 没在跑") if err == 5 else ""
                + "）"
            )
            print(f"[tray] {self._last_error}", file=sys.stderr)
            if err == 5 and self._verbose:
                print("[tray] 自检：进程完整性级别可用 "
                      "whoami /groups 里「强制标签\\中强制级别」是否显示为「低」来确认。",
                      file=sys.stderr)
            return
        self._tray_added = True
        # v4 才会发 NIN_SELECT（单击/双击），不设的话左键单击收不到任何通知。
        # 注意：必须把 uVersion 真的设成 4；不赋值的话这个 union 是 0，
        # 等于要求 shell 用「版本 0」的报文，NIN_SELECT 永远等不来。
        ver = self._fill_nid(NIF_MESSAGE)
        ver.uVersion = NOTIFYICON_VERSION_4
        ctypes.set_last_error(0)
        if shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(ver)):
            self._notify_version_ok = True
        else:
            err = ctypes.get_last_error()
            self._notify_version_ok = False
            if self._verbose:
                print(f"[tray] NIM_SETVERSION(4) 失败，GetLastError={err}"
                      "（仍可用，只是左键单击走老式回调）", file=sys.stderr)
        if self._verbose:
            print(f"[tray] 托盘已就绪 hwnd={self._hwnd} 图标来源={how}")

    def _remove_tray(self) -> None:
        if not self._tray_added:
            return
        nid = self._fill_nid(0)
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        self._tray_added = False

    def _destroy_icon(self) -> None:
        if self._hicon and self._icon_owned:
            user32.DestroyIcon(wintypes.HICON(self._hicon))
        self._hicon = 0

    def _apply_tooltip(self) -> None:
        if not self._tray_added:
            return
        nid = self._fill_nid(NIF_TIP | NIF_SHOWTIP)
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _apply_icon(self) -> None:
        hicon, how = load_icon_handle(self._icon_path, verbose=self._verbose)
        if not hicon:
            self._last_error = f"set_icon 失败：{how}"
            print(f"[tray] {self._last_error}", file=sys.stderr)
            return
        old, old_owned = self._hicon, self._icon_owned
        self._hicon, self._icon_owned = hicon, how.startswith("LoadImageW")
        if self._tray_added:
            nid = self._fill_nid(NIF_ICON)
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        if old and old_owned:
            user32.DestroyIcon(wintypes.HICON(old))

    def _apply_notify(self) -> None:
        if not self._tray_added:
            return
        title = getattr(self, "_notify_title", "")
        text = getattr(self, "_notify_text", "")
        nid = self._fill_nid(NIF_INFO, info=(title, text))
        if not shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)):
            err = ctypes.get_last_error()
            self._last_error = f"气泡通知失败（Shell_NotifyIconW NIM_MODIFY），GetLastError={err}"

    # -- 内部：消息处理 --------------------------------------------------- #
    def _emit(self, action: str) -> None:
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(action)
        except Exception as exc:
            # 队列塞满 / 已关闭：不能把托盘线程带崩
            print(f"[tray] 事件入队失败（{action!r}）：{exc!r}", file=sys.stderr)

    def _snapshot_items(self) -> list[_Item]:
        with self._lock:
            return [
                _Item(i.item_id, i.label, i.checked, i.enabled, i.separator_before, i.default)
                for i in self._items
            ]

    def _show_menu(self) -> None:
        """右键菜单。**只在托盘线程调用** —— TrackPopupMenu 会阻塞到菜单关闭。"""
        hwnd = self._hwnd
        items = self._snapshot_items()
        menu = user32.CreatePopupMenu()
        if not menu:
            self._last_error = f"CreatePopupMenu 失败，GetLastError={ctypes.get_last_error()}"
            print(f"[tray] {self._last_error}", file=sys.stderr)
            return
        cmd_to_id: dict[int, str] = {}
        try:
            cmd = 1
            for it in items:
                if it.separator_before:
                    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                flags = MF_STRING
                if it.checked is True:
                    flags |= MF_CHECKED
                if it.checked is False:
                    flags |= MF_UNCHECKED
                if not it.enabled:
                    flags |= MF_GRAYED | MF_DISABLED
                if it.default:
                    flags |= MF_DEFAULT
                user32.AppendMenuW(menu, flags, cmd, it.label)
                cmd_to_id[cmd] = it.item_id
                cmd += 1

            pt = wintypes.POINT()
            if not user32.GetCursorPos(ctypes.byref(pt)):
                pt.x, pt.y = 0, 0

            # Win32 铁律：弹菜单前必须把窗口设成前台，否则菜单关不掉
            # （点别处菜单不消失，得再点一次）。
            user32.SetForegroundWindow(wintypes.HWND(hwnd))
            ret = user32.TrackPopupMenu(
                menu,
                TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RIGHTBUTTON,
                int(pt.x), int(pt.y), 0, wintypes.HWND(hwnd), None,
            )
            # 第二条铁律：菜单关掉后必须给窗口发个空消息，否则菜单还挂在屏幕上。
            user32.PostMessageW(wintypes.HWND(hwnd), WM_NULL, 0, 0)
            _ = ret  # 没开 TPM_RETURNCMD，点击结果走 WM_COMMAND
        finally:
            user32.DestroyMenu(menu)
            cmd_to_id.clear()

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            msg = int(msg)
            if msg == WM_TRAY_CALLBACK:
                return self._on_tray_callback(hwnd, wparam, lparam)
            if msg == _WM_SET_TOOLTIP:
                self._apply_tooltip()
                return 0
            if msg == _WM_SET_ICON:
                self._apply_icon()
                return 0
            if msg == _WM_NOTIFY:
                self._apply_notify()
                return 0
            if msg == _WM_SYNC_MENU:
                return 0          # 菜单表只有弹菜单时才读，这里不用干活
            if msg == _WM_QUERY_STOP:
                self._remove_tray()
                user32.PostQuitMessage(0)
                return 0
            if msg == WM_COMMAND:
                cmd = int(wparam) & 0xFFFF
                with self._lock:
                    if 0 < cmd <= len(self._items):
                        self._emit(self._items[cmd - 1].item_id)
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            tbc = getattr(self, "_taskbar_created_msg", 0)
            if tbc and msg == tbc:
                if self._verbose:
                    print("[tray] 收到 TaskbarCreated，重新加载托盘图标")
                self._tray_added = False
                self._install_tray()
                return 0
        except Exception as exc:
            self._last_error = f"托盘窗口过程异常（msg={int(msg)}）：{exc!r}"
            print(f"[tray] {self._last_error}", file=sys.stderr)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_tray_callback(self, hwnd, wparam, lparam):
        """``WM_APP+1`` 的回调。**两种报文格式都要认**：

        * ``uVersion >= 4``（``NIM_SETVERSION`` 成功）：``lparam`` 低字是通知码
          （``NIN_SELECT`` / ``NIN_KEYSELECT`` / ...），高字是图标 id；
          ``wparam`` 低字 x、高字 y。
        * 老格式：``lparam`` 低字直接是鼠标消息（``WM_LBUTTONUP`` 等）。

        辨别方式**不能**靠「lparam 看起来像不像鼠标消息」——那种启发式在 v4 下
        会把 ``wparam`` 里的坐标当事件码，坐标凑巧等于某个常量时就会误触发。
        这里以 ``NIM_SETVERSION`` 的实际结果为准，再对 ``NIN_SELECT`` 做一次
        显式兜底（有些 shell 不认 SETVERSION 但照样发 v4 通知码）。
        """
        try:
            lparam = int(lparam)
            wparam = int(wparam)
        except Exception:
            return 0
        low = lparam & 0xFFFF

        if low in (NIN_SELECT, NIN_KEYSELECT) or (
            not self._notify_version_ok and low in (WM_LBUTTONUP, WM_LBUTTONDBLCLK)
        ):
            self._emit("__activate__")
        elif low in (WM_RBUTTONUP, WM_CONTEXTMENU):
            self._show_menu()
        elif self._notify_version_ok and low in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
            # 设了 v4 却收到老式鼠标消息（少见，但有人碰到过）：也当激活
            self._emit("__activate__")
        _ = (hwnd, wparam)
        return 0


# PeekMessageW：清理窗口类前要排空队列，这里补签名（放模块底部，不影响上面的可读性）
PM_REMOVE = 0x0001
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT
]
user32.PeekMessageW.restype = wintypes.BOOL


def _default_icon_path() -> Path:
    """``t_ime/assets/tray.ico``（打包 / 换目录时可能不存在，调用方自己判断）。"""
    return Path(__file__).resolve().parent / "assets" / "tray.ico"
