# -*- coding: utf-8 -*-
"""输入状态机：把全局钩子送来的击键流变成「候选 + 上屏」。

这一层刻意**不碰 tkinter**，只做状态迁移和后台翻译调度，
所有 UI 更新都通过 ``emit`` 回调丢出去，这样可以脱离界面单独测试。

线程模型
--------
* :meth:`IMECore.handle_key` 在**钩子线程**上被调用，必须立刻返回
  （系统给低级钩子回调的预算是 300ms），所以这里只做纯状态运算。
* 翻译走 :class:`TranslateWorker` 后台线程，永远只算最新一次请求。
* 上屏用 SendInput 同步注入 —— 必须同步，否则后面的按键会跑到上屏内容前面去。
  注入事件带 LLKHF_INJECTED 标志，:meth:`handle_key` 第一行就放行，不会自激。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

from . import winapi as W
from .hotkey import HOTKEY_ACTIONS as _HOTKEY_ACTIONS
from .hotkey import spec_problem
from .pinyin import PinyinEngine, can_segment
from .translator import Translator, ZH2EN

ROW_ZH = 0
ROW_EN = 1

EV_STATE = "state"
EV_COMMIT = "commit"
EV_QUIT = "quit"

# 拼音缓冲的上限，防止用户按住键不放把内存打满
MAX_BUFFER = 48

# 按住这些修饰键时完全透传，免得把 Ctrl+C / Alt+Tab 这类系统快捷键毁掉
BLOCKING_MODS = frozenset({W.VK_CONTROL, W.VK_MENU, W.VK_LWIN, W.VK_RWIN})

# 修饰键还按在手里的时候，往目标窗口注入的文本会被**系统**吃掉：既不进窗口，
# 也不会在松手之后补上来（实测：按着 Ctrl+Alt 注入 'Held'，靶子窗口一个字都没收到）。
# 所以「上屏」这件事必须推迟到修饰键松开之后一点点 —— 这一小段是实测出来的余震，
# 而且要垫过那个无害键（见 W.defuse_system_menu）才干净。完整实测见 README 踩坑第 26 条。
COMMIT_AFTER_MODS = 0.12
# 万一松开事件丢了（锁屏 / Ctrl+Alt+Del），最多等这么久就照样注入：宁可丢格式，
# 不能把用户刚打的那句话吞掉。
COMMIT_OUT_MAX_WAIT = 3.0
# 归一化之后的修饰键集合。_down 里存的一律是归一化过的键码。
MOD_VK_SET = frozenset({W.VK_SHIFT, W.VK_CONTROL, W.VK_MENU, W.VK_LWIN, W.VK_RWIN})

# config.json 的 hotkeys 段里**合法**的动作名 —— 就是 _fire() 认识的那几个。
# 写错的键名以前会照常注册成热键（组合键被吞、什么也不发生），所以这里要有个白名单。
# 第 63 轮起这份白名单挪到 t_ime/hotkey.py：--check / --doctor 也要用它分诊，
# 而它们不该为了这一个 frozenset 去 import 整个 core（core 会拉起 winapi）。
# 这里保留同名转发，老代码与测试（from t_ime.core import HOTKEY_ACTIONS）不用动。
HOTKEY_ACTIONS = _HOTKEY_ACTIONS


# 只按一下 Shift（按下到抬起不超过这么久，且期间没按别的键）就切中/英直通。
# 这是主流输入法的做法，用户不用去记 Ctrl+Alt+Z。
#
# 时间窗为什么是 600ms（第 20 轮真机实测后从 300ms 放宽）：300ms 只有「飞快
# 弹一下」才够得着，而用户心里的动作是「按一下 Shift 去切模式」—— 真机探针里
# 按住 500ms 再松开，结果是**什么都没发生、也没有任何提示**（`_shift_used` 为假、
# 只是超时），用户只能得出「Shift 切英文不存在」的结论。放宽到 600ms 后这种
# 从容的一按就能切。
#
# 为什么不干脆取消时间限制：Shift 还兼职当修饰键，而 Shift+鼠标点击（在资源
# 管理器/编辑器里选一段）期间**没有任何按键事件**，只看「期间没按别的键」就
# 会把这类操作误判成单击、把模式悄悄翻过去。留一个几百毫秒的窗，让「一按」
# 能过、让「按着不放去点鼠标」过不去，是这两个要求之间唯一说得通的折中。
SHIFT_TAP_MS = 600
# 候选窗上的短提示显示多久
TOAST_MS = 1600
# 直通/挂起状态下连打几个字母都没进缓冲，就在候选窗上提醒一次。
# 「打字没反应」是用户最容易误解成「程序坏了」的状态，必须给个回声。
PASSTHROUGH_NUDGE_AFTER = 3

# 会话内自学习权重的落盘节流
BOOST_SAVE_INTERVAL = 5.0


class TranslateWorker(threading.Thread):
    """后台翻译线程，带防抖 + 「只算最新一次」的合并语义。

    打字很快时会产生一串请求，这里保证：等待 ``debounce`` 期间若有新请求进来，
    就直接换掉旧请求，永远不去翻译中间那些已经被删掉的半成品。
    """

    def __init__(self, translator: Translator, debounce: float = 0.25, verbose: bool = True):
        super().__init__(name="translate", daemon=True)
        self.translator = translator
        self.debounce = max(0.0, float(debounce))
        self.verbose = verbose
        self.results: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._latest: tuple[int, str] | None = None
        self._event = threading.Event()
        # 别叫 ``_stop``：``threading.Thread`` 自己有一个 private 方法 ``_stop()``，
        # 同名属性会把它盖掉 —— 于是 ``worker.join()``（顺便 ``is_alive()`` 在某些
        # 时序下）会抛 ``TypeError: 'bool' object is not callable``。
        self._stopping = False

    def submit(self, seq: int, text: str) -> None:
        with self._lock:
            self._latest = (seq, text)
        self._event.set()

    def stop(self) -> None:
        self._stopping = True
        self._event.set()

    def run(self) -> None:
        while not self._stopping:
            self._event.wait(0.25)
            self._event.clear()
            if self._stopping:
                break
            # 防抖：这段时间内又来了新请求，就重新计时
            deadline = time.monotonic() + self.debounce
            while not self._stopping and time.monotonic() < deadline:
                if self._event.wait(0.02):
                    self._event.clear()
                    deadline = time.monotonic() + self.debounce
            with self._lock:
                item = self._latest
                self._latest = None
            if item is None:
                continue
            seq, text = item
            try:
                out = self.translator.translate_many(text, ZH2EN)
            except Exception as exc:  # 翻译炸了绝不能影响打字
                out = []
                if self.verbose:
                    print(f"[translate] 异常：{exc!r}")
            self.results.put((seq, text, out))


class IMECore:
    """拼音 → 中文候选 → 译文候选，以及上屏。"""

    def __init__(
        self,
        cfg: dict,
        engine: PinyinEngine,
        translator: Translator,
        *,
        emit=None,
        committer=None,
        verbose: bool = True,
        mods_source=None,
        caps_source=None,
    ):
        self.cfg = cfg
        self.engine = engine
        self.translator = translator
        self.verbose = verbose
        self._emit = emit or (lambda ev: None)
        self._committer = committer or (lambda text: True)
        # 「已经决定上屏、但还没真的注入」的排队处：修饰键还按在手里的时候注入会被系统
        # 吃掉，于是这些文本先在这儿等修饰键松开（见 _deliver）。
        self._out_lock = threading.Lock()
        self._out_queue: list[tuple[str, str]] = []
        self._out_running = False
        # 「此刻物理上按着哪些修饰键」。默认问系统；测试可以注入一个假的。
        # 事件流会失同步（Ctrl+Alt+Del / 锁屏 / UAC 之后 keyup 永远收不到），
        # 所以凡是靠修饰键做的判断都要拿这个纠偏，见 W.physical_mods 的说明。
        self._mods_source = mods_source or W.physical_mods
        # 「CapsLock 现在开着吗」。默认问系统，但那一次读数**不是随时都准**
        # （锁屏时必然是假 0，见 README 踩坑第 20 条），所以真正可靠的那一份是
        # 我们自己从事件流里记的 ``_caps``（钩子看得见 VK_CAPITAL 的按下），
        # 系统那份只在启动 / 重装钩子时拿来对一次表。
        self._caps_source = caps_source if caps_source is not None else W.caps_lock_on

        out_cfg = cfg.get("output", {})
        p_cfg = cfg.get("pinyin", {})
        t_cfg = cfg.get("translate", {})

        self.boost_on_correct = float(out_cfg.get("boost_on_correct", 1.0))
        self.boost_on_pick = float(out_cfg.get("boost_on_pick", 0.25))
        boost_file = str(out_cfg.get("boost_file", "user_boosts.json"))
        root = Path(__file__).resolve().parent.parent
        self.boost_path = Path(boost_file)
        if not self.boost_path.is_absolute():
            self.boost_path = root / boost_file

        self.max_candidates = int(p_cfg.get("max_candidates", 7))
        self.variant_count = max(1, int(t_cfg.get("variants", 4)))
        self.default_row = ROW_EN if str(out_cfg.get("default_language", "zh")).lower() == "en" else ROW_ZH

        # ---- 对外的可见状态（UI 直接照着画） ----
        self.enabled = bool(out_cfg.get("start_enabled", True))
        self.buffer = ""
        self.zh: list[str] = []
        self.en: list[str] = []
        # active 是「上屏语言」模式，**跨词粘住**：Tab 切到译文行以后就一直上屏译文，
        # 直到用户再按一次 Tab / toggle_default。这正是「用快捷键选输入中文还是译文」
        # 这个需求本身，所以 _reset() 刻意不清它。
        self.active = self.default_row
        # 第 91 轮：默认上屏设成「译文」而翻译关着，是一对自相矛盾的配置 ——
        # 每次空格都会从 _commit 的兜底退回中文，用户却只看到状态里写着「译文」。
        # 启动时就说清楚，别让人以为「译文模式」真的生效了。
        if self.default_row == ROW_EN and not self.translator.enabled:
            print("[core] 默认上屏设成了「译文」，但翻译是关着的（translate.enabled=false）"
                  "—— 每次上屏都会退回中文；想用译文就先打开它再重启")
        self.sel = [0, 0]
        # 这一轮候选是按缓冲的哪个前缀算出来的（`_candidates_for` 里写）。
        # 上屏时用它算「还剩哪些字母没被用掉」—— 用户敲的字母一个都不许丢。
        self._cand_prefix = ""
        self.tr_state = "idle"          # idle | pending | done | failed | off
        self.status = ""
        self.quit_requested = False
        # 直通英文模式：字母原样交给系统（所以大小写、各种符号天然正确），
        # 完全不进拼音缓冲。单击 Shift 切换 —— 这就是「想打英文怎么办」的答案。
        self.direct = False

        # ---- Shift 单击判定 ----
        in_cfg = cfg.get("input", {})
        self.shift_tap_ms = max(0, int(in_cfg.get("shift_tap_ms", SHIFT_TAP_MS)))
        self.shift_tap_toggles = bool(in_cfg.get("shift_tap_toggles", True))
        self._shift_at: float | None = None   # Shift 按下的时刻
        self._shift_used = False              # 这轮 Shift 按住期间有没有按别的键

        # ---- CapsLock（用户反馈「开着 IME 打不出大写英文」的那一半）----
        # ``_caps`` 是我们从事件流里记的：钩子每看到一次 VK_CAPITAL 按下就翻一次。
        # 这么记的理由很硬：钩子看到的是**用户真的按了那个键**，不依赖任何一次读数
        # 的时机；而启动那一刻对表用的 ``GetKeyState`` 有两种问不到的情形（锁屏、
        # 以及别人刚把状态改掉而我们还没处理过键盘消息）—— 见 winapi.caps_lock_on()。
        self._caps = False
        self._caps_inited = False      # 有没有跟系统对过表
        self.caps_from_events = 0     # 事件流里看到过几次 CapsLock 切换
        self.caps_fallbacks = 0       # 系统那份读不到、退回自己记的次数
        self.caps_from_system = 0     # 系统那份读到并采纳的次数

        # ---- 候选窗上的短提示（模式切换、挂起/恢复都要让用户看得见）----
        self.toast_ms = max(0, int(in_cfg.get("toast_ms", TOAST_MS)))
        self.toast = ""
        self._toast_until = 0.0
        # 「直通/挂起」状态下连打了几个字母都没进缓冲时提醒一次。
        # 没有这个的话，用户看到的就是纯粹的「打字没反应」，根本猜不到原因。
        self._pass_streak = 0

        # ---- 钩子侧内部状态 ----
        self._down: set[int] = set()
        self._consumed: set[int] = set()
        self._swallow_up: set[int] = set()
        self._seq = 0
        self._tr_seq = 0
        self._tr_text = ""
        self._boosts_dirty = False
        self._last_boost_save = time.monotonic()
        # 上一次落盘失败的原因（空 = 最近一次落盘是好的）。面板和 --check 靠它
        # 说清楚「自学习为什么没生效」—— 否则只能看见「已记住 N 个词」，永远等不到
        # 那个「已写入」（第 81 轮：boost_file 的父目录不存在时就是这样）。
        self._boost_error = ""
        # 权重文件读进来时出的错（空 = 读懂了或根本没有文件）。和落盘错误分开：
        # 读不懂时那份文件会被改名留证，而「这次自学习从零开始」是另一件事。
        self._boost_load_error = ""

        self.hotkeys: dict[tuple[frozenset, int], str] = {}
        # 真正注册上的热键（动作名 -> 给人看的写法）。播报（banner / 引导 / 状态）
        # 必须读这张表：配置里写了「q」但被拒绝时，还说「按 Q 退出」就是另一句假话。
        self.registered_hotkeys: dict[str, str] = {}
        self._parse_hotkeys()

        self.worker = TranslateWorker(
            translator,
            debounce=float(t_cfg.get("debounce_ms", 250)) / 1000.0,
            verbose=verbose,
        )
        self.worker.start()

    # ------------------------------------------------------------------ #
    # 热键
    # ------------------------------------------------------------------ #
    def _parse_hotkeys(self) -> None:
        for name, spec in (self.cfg.get("hotkeys") or {}).items():
            if not spec:
                continue  # 留空 = 这个动作不绑热键（比如 toggle_direct 只靠单击 Shift）
            if name not in HOTKEY_ACTIONS:
                # 名字写错（比如 quit -> quitt）以前会被当成一个合法热键注册：
                # 那个组合键从此被我们吞掉，按下去却什么也不发生，日志里也没有一句话
                # （只有 --verbose 才会说「未知热键动作」）。别注册，直接说清楚。
                print(
                    f"[core] 热键动作名 {name!r} 不认识，已忽略"
                    f"（支持的是 {', '.join(sorted(HOTKEY_ACTIONS))}）"
                )
                continue
            try:
                mods, main = W.parse_hotkey(spec)
            except Exception as exc:
                print(f"[core] 热键 {name}={spec!r} 无效，已忽略：{exc}")
                continue
            problem = spec_problem(spec, W.parse_hotkey)
            if problem:
                # 没有修饰键的打字键（``quit: "q"``）：注册它等于把那个键从打字里
                # 抢走 —— 打「q」直接把输入法退了，而用户根本不知道发生过什么。
                print(f"[core] 热键 {spec!r} 已忽略：{problem}")
                continue
            prev = self.hotkeys.get((mods, main))
            if prev is not None:
                # 两个动作绑同一个组合键：后一个会静默盖掉前一个，用户只会觉得
                # 「我配的那个热键没反应」。保留先出现的那个，并把冲突说出来。
                print(
                    f"[core] 热键 {spec!r} 同时配给了 {prev} 和 {name}，"
                    f"{prev} 生效、{name} 已忽略"
                )
                continue
            self.hotkeys[(mods, main)] = name
            # 播报（banner / 托盘提示 / 状态文件 / 面板）读的必须是**真正注册上的**
            # 组合键，不是配置原文：``"ctrl+q+w"`` 这种写法真正生效的只有 Ctrl+W
            # （第 84 轮以前的 pretty() 会照抄成「Ctrl+Q+W」）。
            self.registered_hotkeys[name] = W.combo_text(mods, main)

    def apply_hotkeys(self, cfg: dict) -> None:
        """重新读 ``hotkeys.*``（Web 控制板保存后立刻生效，不用重启）。

        必须先清空两张表再重解析：只往里加的话，用户把 ``quit`` 从 Ctrl+Alt+Q
        改成 F9，旧组合键仍然挂在表里 —— 按 Ctrl+Alt+Q 依然退出，而面板上写着
        「退出：F9」，两边对不上。解析规则和启动时完全同一条路
        （``_parse_hotkeys``），所以「没修饰键的打字键」「两个主键」这些拒绝
        在热更新里同样生效。
        """
        self.cfg = cfg
        self.hotkeys.clear()
        self.registered_hotkeys.clear()
        self._parse_hotkeys()

    def apply_config(self, cfg: dict) -> None:
        """热更新「改了立刻能生效」的那部分配置。

        App 层在收到 ``reload`` 命令后调用。这里刻意只碰不依赖重建对象的东西：
        热键、单击 Shift、提示时长、自学习强度、候选个数、默认上屏行。
        拼音索引（min_freq / max_word_len / dict_cache）、窗口外观、托盘开关
        不在这里 —— 那些要么得重建索引/窗口，要么得启停线程，由 App 层决定。
        """
        self.cfg = cfg
        out_cfg = cfg.get("output", {})
        in_cfg = cfg.get("input", {})
        p_cfg = cfg.get("pinyin", {})
        t_cfg = cfg.get("translate", {})

        self.boost_on_correct = float(out_cfg.get("boost_on_correct", 1.0))
        self.boost_on_pick = float(out_cfg.get("boost_on_pick", 0.25))
        boost_file = str(out_cfg.get("boost_file", "user_boosts.json"))
        root = Path(__file__).resolve().parent.parent
        boost_path = Path(boost_file)
        if not boost_path.is_absolute():
            boost_path = root / boost_file
        if boost_path != self.boost_path:
            # 换了自学习文件：把新文件读进来（旧文件的权重不搬过去 —— 用户换
            # 文件就是想换一份，静默合并反而会把他想甩掉的词带回来）。
            self.boost_path = boost_path
            self.boosts = {}
            self._boost_load_error = ""
            self.load_boosts()

        self.max_candidates = int(p_cfg.get("max_candidates", 7))
        self.variant_count = max(1, int(t_cfg.get("variants", 4)))
        self.shift_tap_ms = max(0, int(in_cfg.get("shift_tap_ms", SHIFT_TAP_MS)))
        self.shift_tap_toggles = bool(in_cfg.get("shift_tap_toggles", True))
        self.toast_ms = max(0, int(in_cfg.get("toast_ms", TOAST_MS)))

        new_default = ROW_EN if str(out_cfg.get("default_language", "zh")).lower() == "en" else ROW_ZH
        self.default_row = new_default
        if not self.buffer and self.tr_state != "pending":
            # 缓冲空着才动「上屏语言」：正在打一半拼音时把模式翻过去，用户按下
            # 空格会发现上屏的语言跟他打的时候看到的不一样。
            self.active = new_default
        self.apply_hotkeys(cfg)

    def _switch_active_row(self, *, toast: bool = True) -> None:
        """切「中文 / 译文」上屏行（Tab 和 hotkeys.toggle_default 共用一条路）。

        第 91 轮：翻译关着时切到译文行拿不到译文（_commit 见 en 为空会退回中文），
        与其切进一个永远不可达的模式，不如留在中文并说清原因。
        第 97 轮：Tab 以前绕过这段判断，直接翻 active —— 同一个动作，热键拒绝、
        Tab 放行，于是用户按 Tab 会看到「译文」模式，每次空格却又变回中文。
        第 97 轮（同一轮内修）：Tab 是打字时随手按的键，**不弹 toast**
        （一弹就把候选窗遮住，`tests/test_hook_chain.py` 的「上屏后悬浮窗隐藏」
        立刻变红）；只有热键那条路才弹。
        """
        if self.active == ROW_ZH and not self.translator.enabled:
            self.status = "翻译关着（translate.enabled=false）—— 切不到译文"
            self._toast("翻译关着（translate.enabled=false）—— 切不到译文，想用先打开它再重启")
            self._push_state()
            return
        self.active = ROW_EN if self.active == ROW_ZH else ROW_ZH
        if toast:
            self._toast("空格上屏：译文" if self.active == ROW_EN else "空格上屏：中文")
        self._push_state()

    def _settle_buffer_before_switch(self) -> str:
        """切模式 / 挂起 / 上屏大写之前，先把手上的拼音落下去，返回真正上屏的文本。

        第 80 轮：挂起热键以前直接 ``_reset()``，被钩子吞掉的半截拼音凭空消失。
        第 98 轮：译文行上译文还在路上时 ``_commit(ROW_EN)`` 拒绝交付，接着
        ``_reset()`` 又把缓冲清掉 —— 字母既没上屏也没留下。有中文候选就先按中文
        首候选上屏，别让用户的五下按键变成空气。
        第 101 轮：同一件事在「切英文直通」和「Shift+字母」两条入口上各写了一份
        判断，那两条都没有这层兜底 —— 半截拼音留在缓冲里不走，直通模式下再按一次
        切回来，它才在某次别的动作里冒出来。统一成这一个入口。
        第 102 轮：这层兜底原来还要求「有中文候选」（``and self.zh``），可缓冲里
        整串不是拼音时（``qqq``）zh 就是空的 —— 于是又原样漏出去，缓冲留着
        ``qqq``。现在只要 pending 就先按中文行走一趟：有候选按首候选，没候选
        ``_commit`` 自己会兜底把整串原样上屏。
        """
        if not self.buffer:
            return ""
        if self.active == ROW_EN and self.tr_state == "pending":
            delivered = self._commit(ROW_ZH, None)
            if delivered:
                self.status = f"译文还没回来，先把「{delivered}」上屏了"
            return delivered
        return self._commit(self.active, None)

    def _fire(self, name: str) -> None:
        if name == "quit":
            self.quit_requested = True
            self._emit({"type": EV_QUIT})
        elif name == "toggle_enabled":
            if self.buffer:
                # 半截拼音先落下去再挂起。这些键已经被钩子吞了，直接 _reset
                # 等于把它们从用户眼前抹掉。修饰键还按着时投递会自动推迟。
                self._settle_buffer_before_switch()
            self.enabled = not self.enabled
            self._reset(keep_status=True)
            self._pass_streak = 0
            self._toast("输入法已开启：直接打字" if self.enabled else "输入法已挂起：按键全部透传")
            self._push_state()
        elif name == "toggle_default":
            self._switch_active_row()
        elif name == "toggle_direct":
            self.toggle_direct()
        elif name == "compose":
            if not self.buffer:
                self.status = "先打几个拼音，再按直译上屏"
                self._push_state()
                return
            self.status = ""
            self._commit(ROW_EN, None)
        else:
            if self.verbose:
                print(f"[core] 未知热键动作 {name!r}")

    # ------------------------------------------------------------------ #
    # 钩子入口（钩子线程）
    # ------------------------------------------------------------------ #
    def handle_key(self, vk: int, is_down: bool, own_injection: bool) -> bool:
        """返回 True 表示吞掉这个键。必须极快返回。

        ``own_injection`` 只标记**我们自己回灌的**事件（钩子按 ``dwExtraInfo``
        上的 :data:`winapi.T_IME_INJECT_TAG` 认出来的），必须放行，否则上屏的文本
        会被自己的钩子钩回来，自激成死循环。

        注意它**不是** ``LLKHF_INJECTED``：别的程序（改键器 / 宏 / 软键盘）转发的
        按键也带那个标志，但那是用户实实在在敲下去的，必须当成真实按键处理 ——
        否则那些机器上会表现为「打字完全没反应」。
        """
        if own_injection:
            return False

        vk = W.normalize_vk(vk)
        # 「按下」可能是键盘**连发**（按住不放时系统反复补按下事件）—— 记录一下，
        # 热键不能因为它重复触发（连发间隔约 30ms，按住半秒就能把开关抖成一团）。
        repeated = is_down and vk in self._down
        if is_down:
            self._down.add(vk)
        else:
            self._down.discard(vk)

        down = set(self._down)
        if is_down:
            down.add(vk)

        # ---- Shift 单击判定：只按一下 Shift 切中/英直通 ----
        # 判定必须在别的键之前做，因为「按着 Shift 又按了别的键」就不算单击。
        if is_down and vk != W.VK_SHIFT and self._shift_at is not None:
            self._shift_used = True
        if vk == W.VK_SHIFT:
            if is_down:
                if self._shift_at is None:
                    self._shift_at = time.monotonic()
                    self._shift_used = False
            else:
                held_ms = (time.monotonic() - self._shift_at) * 1000.0 if self._shift_at else 1e9
                tapped = (self._shift_at is not None and not self._shift_used
                          and held_ms <= self.shift_tap_ms)
                self._shift_at = None
                self._shift_used = False
                if tapped and self.shift_tap_toggles and self.enabled:
                    self.toggle_direct()
            return False        # Shift 本身永远透传，单独按它不该有副作用

        # ---- CapsLock：事件流里的那一次按下就是权威 ----
        # 为什么以事件流为准：这里看到的是用户真按下去的 CapsLock，不依赖任何一次
        # 读数的时机（启动对表那次读数在锁屏时必然是假 0，见 winapi.caps_lock_on()）。
        # 键照常透传，让系统自己去切真实状态，我们只跟着记一份。
        if vk == W.VK_CAPITAL and is_down:
            self._caps = not self._caps
            # 用户刚亲口告诉我们 CapsLock 是什么状态了 —— 之后系统那句
            # 「关着」不许再把它抹掉（那正是探针里抓到的现场）。
            self._caps_inited = True
            self.caps_from_events += 1
            return False

        # ---- 热键 ----
        # 事件流可能没看到修饰键的按下（钩子是在用户已经按着 Ctrl 时才装上的），
        # 所以匹配不上时拿真实按键状态兜一次底 —— 否则热键会「按了没反应」。
        for (mods, main), name in self.hotkeys.items():
            if vk != main:
                continue
            if is_down:
                if repeated:
                    # 键盘连发：键已经吞过了，别再触发一次。按住 Ctrl+Alt+Z 半秒
                    # 会来十几个连发，不挡住的话挂起/恢复就抖成一团，用户松手时
                    # 停在哪一边全看运气。
                    self._consumed.add(vk)
                    return True
                if mods <= down or mods <= self._real_mods():
                    self._consumed.add(vk)
                    if W.VK_MENU in mods:
                        # 主键被吞掉之后，系统只看到「Alt 自己松开了」-> 它会激活
                        # 前台窗口的系统菜单，把用户紧接着敲的下一个键吃掉。补一个
                        # 无害键挡掉（见 winapi.defuse_system_menu 的说明）。
                        # 丢给线程做：钩子回调的预算只有 300ms，不在这里等 SendInput。
                        threading.Thread(
                            target=W.defuse_system_menu, daemon=True,
                            name="t-ime-defuse-menu",
                        ).start()
                    self._fire(name)
                    return True
            elif vk in self._consumed:
                self._consumed.discard(vk)
                return True

        # ---- 非打字键的抬起：把之前吞下的配套抬起也吞掉 ----
        if not is_down:
            if vk in self._swallow_up:
                self._swallow_up.discard(vk)
                return True
            return False

        if not self.enabled:
            if 0x41 <= vk <= 0x5A:
                self._nudge_passthrough()
            return False

        # 直通英文模式：字母原样交给系统。不吞、不进缓冲，
        # 所以大小写、CapsLock、各种键盘布局天然就是对的。
        if self.direct:
            if 0x41 <= vk <= 0x5A:
                self._nudge_passthrough()
            return False

        # Ctrl/Alt/Win 组合一律透传，保住系统级快捷键。
        # 但「事件流说 Ctrl 还按着」可能是假的 —— Ctrl+Alt+Del / 锁屏 / UAC 之后
        # 那个 keyup 永远收不到。卡住的 Ctrl 会让这里恒真，每个键都透传，
        # 输入法就静默变成哑巴。所以真要透传之前，先跟物理按键状态核对一次。
        if down & BLOCKING_MODS:
            # 这个事件本身就是某个修饰键的按下/抬起 —— 事件流在这件事上就是权威，
            # 拿物理状态去覆盖它会把用户正按着的 Ctrl 抹掉，热键就永远匹配不上了。
            if vk in MOD_VK_SET:
                return False
            self.resync_modifiers()
            down = set(self._down)
            if is_down:
                down.add(vk)
            if down & BLOCKING_MODS:
                return False

        shift_now = W.VK_SHIFT in down or W.VK_SHIFT in self._real_mods()

        # ---- 字母与分词符 ----
        if 0x41 <= vk <= 0x5A:
            self._swallow_up.add(vk)
            caps = self.caps_lock()
            # Windows 的大小写就是 Shift 与 CapsLock 的**异或**：
            #   开 CapsLock 不按 Shift → 大写；开 CapsLock 又按 Shift → 小写。
            upper = shift_now != caps
            if shift_now or caps:
                # Shift+字母 = 大写英文、CapsLock 开着 = 大写英文，都直接上屏，
                # 不进拼音缓冲（拼音全是小写，这两种情况本来也没有拼音含义）。
                # 少了 CapsLock 这一半，用户看到的就是「开着输入法打不出大写英文」。
                self._commit_raw(chr(vk) if upper else chr(vk + 0x20))
            else:
                self._input(chr(vk + 0x20))
            return True

        if vk == W.VK_OEM_7:  # 单引号键，当拼音分词符用
            if not shift_now and self.buffer:
                self._swallow_up.add(vk)
                self._input("'")
                return True
            # 没在打拼音的时候按引号，那是用户真想打一个引号（英文缩写、引用）；
            # Shift+' 打的是双引号，同理。这里绝不能吞：早先无条件吞掉之后，
            # 空缓冲里会冒出一个只有一个 "'" 的拼音行，再按空格才把那个 "'" 打出去，
            # 用户看到的是「按了引号什么都没发生，还弹出一个空候选窗」。
            if self.buffer:
                self.status = ""
                self._commit(self.active, None)
            return False

        # ---- 标点：先把挂起的候选上屏，再把字符放过去 ----
        # 不这么做的话，那些字符会插进正在合成的拼音中间，变成一串垃圾。
        if vk in W.PUNCT_VKS or (0x30 <= vk <= 0x39 and shift_now):
            if self.buffer:
                self.status = ""
                self._commit(self.active, None)
            return False

        # ---- 编辑键 ----
        if vk == W.VK_BACK:
            if self.buffer:
                self._swallow_up.add(vk)
                self._input(None)
                return True
            return False

        if vk == W.VK_ESCAPE:
            if self.buffer:
                self._swallow_up.add(vk)
                self.status = ""
                self._reset()
                return True
            return False

        if vk == W.VK_TAB:
            if self.buffer:
                self._swallow_up.add(vk)
                self._switch_active_row(toast=False)
                return True
            return False

        if vk in (W.VK_SPACE, W.VK_RETURN):
            if self.buffer:
                self._swallow_up.add(vk)
                self._commit(self.active, None)
                return True
            return False

        if vk in (W.VK_LEFT, W.VK_UP, W.VK_RIGHT, W.VK_DOWN):
            if self.buffer:
                self._swallow_up.add(vk)
                self._move(-1 if vk in (W.VK_LEFT, W.VK_UP) else 1)
                return True
            return False

        # ---- 1..9 直选 ----
        if 0x31 <= vk <= 0x39:
            if self.buffer:
                self._swallow_up.add(vk)
                self._pick(vk - 0x31)
                return True
            return False

        return False

    # ------------------------------------------------------------------ #
    # 状态迁移
    # ------------------------------------------------------------------ #
    def _input(self, ch: str | None) -> None:
        self._pass_streak = 0  # 又进缓冲了，说明用户不在「打字没反应」的状态里
        if ch is None:
            self.buffer = self.buffer[:-1]
            self._recompute()
            return
        if len(self.buffer) >= MAX_BUFFER:
            # 缓冲到顶了，但用户还在敲：把当前这一段当作按了一次空格先上屏，
            # 再让这一下按键开启新的拼音。以前这里是 `return` —— 键被钩子吞掉、
            # 屏幕上什么也不发生，用户看到的是「打到一半键盘失灵」。
            # `_commit` 的兜底分支保证非空缓冲一定会被清掉（非法拼音整串上屏）。
            self._commit(ROW_ZH, None)
            self.buffer += ch
            self._recompute()
            self.status = f"拼音太长（超过 {MAX_BUFFER} 个字母），已先把上一段上屏"
            self._push_state()
            return
        self.buffer += ch
        self._recompute()

    def _recompute(self) -> None:
        """缓冲变了：重算中文候选，并触发一次（防抖的）翻译。"""
        self._seq += 1
        self._tr_seq += 1  # 作废在途翻译
        self.sel = [0, 0]
        self.en = []
        self.status = ""

        if not self.buffer:
            self.zh = []
            self.tr_state = "idle"
            self._push_state()
            return

        self.zh = self._candidates_for(self.buffer)
        self.sel[ROW_ZH] = 0

        if not self.zh:
            self.tr_state = "idle"
        elif not self.translator.enabled:
            self.tr_state = "off"
        else:
            self.tr_state = "pending"
            self._tr_text = self.zh[0]
            self.worker.submit(self._tr_seq, self.zh[0])

        self._push_state()

    def _candidates_for(self, buf: str) -> list[str]:
        """对**最长可切分前缀**求候选。

        用户边打边看，输入到一半（比如 ``nih``）时整串还不是合法拼音，
        这时应该退回 ``ni`` 的候选，而不是清空候选行 —— 真实输入法就是这么做的。

        用到的前缀会记进 ``self._cand_prefix``：上屏时要把没用掉的那截字母
        留在缓冲里（``nihaoz`` + 空格 = 上屏「你好」、缓冲剩 ``z``），
        不然用户敲的字母会凭空消失。
        """
        s = buf.replace("'", "").lower()
        self._cand_prefix = ""
        if not s:
            return []
        for cut in range(len(s), 0, -1):
            prefix = s[:cut]
            if can_segment(prefix):
                cands = self.engine.candidates(prefix)
                if cands:
                    self._cand_prefix = prefix
                    return cands
        return []

    def _leftover_after_prefix(self) -> str:
        """缓冲里没被这次候选前缀用掉的那一截（按字母数对齐，跳过分词符）。"""
        prefix = self._cand_prefix
        buf = self.buffer
        if not prefix or not buf:
            return ""
        need = len(prefix)
        i = 0
        while i < len(buf) and need > 0:
            if buf[i] != "'":
                need -= 1
            i += 1
        return buf[i:]

    def _retranslate(self) -> None:
        """中文行换了高亮项 —— 译文行跟着重算。"""
        self._tr_seq += 1
        self.en = []
        self.sel[ROW_EN] = 0
        if not self.zh:
            self.tr_state = "idle"
            return
        text = self.zh[min(self.sel[ROW_ZH], len(self.zh) - 1)]
        if not self.translator.enabled:
            self.tr_state = "off"
            return
        self.tr_state = "pending"
        self._tr_text = text
        self.worker.submit(self._tr_seq, text)

    def _move(self, delta: int) -> None:
        lst = self.zh if self.active == ROW_ZH else self.en
        if not lst:
            return
        cur = self.sel[self.active]
        self.sel[self.active] = max(0, min(len(lst) - 1, cur + delta))
        if self.active == ROW_ZH:
            self._retranslate()
        self._push_state()

    def _pick(self, index: int) -> None:
        lst = self.zh if self.active == ROW_ZH else self.en
        if not lst:
            # 活动行是空的（多半是译文还没回来），交给 _commit 给出准确的原因，
            # 而不是含糊地说「没有第 N 个候选」。
            self._commit(self.active, None)
            return
        if index >= len(lst):
            self.status = f"没有第 {index + 1} 个候选"
            self._push_state()
            return
        self.status = ""
        self._commit(self.active, index)

    def _mods_held(self) -> bool:
        """现在有没有「会挡住注入」的修饰键真按着（事件流的 ``_down`` + 物理状态）。"""
        held = set(self._down) | set(self._real_mods() or ())
        return bool(held & BLOCKING_MODS)

    def _deliver(self, text: str, note: str = "") -> bool:
        """把一段文本交给 committer（``_commit`` 的唯一出口）。

        修饰键还按在手里的时候**必须推迟**：那一刻窗口处于系统的「菜单模式」，
        注入进去的文本会被整个吃掉、连松手之后都不会补上来（真机实测，README 踩坑 26）。
        推迟发生在后台线程里，钩子回调一个字都不多等。
        """
        if not self._mods_held():
            ok = bool(self._committer(text))
            self._emit({"type": EV_COMMIT, "text": text, "ok": ok, "note": note})
            return ok
        with self._out_lock:
            self._out_queue.append((text, note))
            if not self._out_running:
                self._out_running = True
                threading.Thread(target=self._flush_out, daemon=True,
                                 name="t-ime-commit-out").start()
        return True

    def _flush_out(self) -> None:
        """等修饰键松开（最多 ``COMMIT_OUT_MAX_WAIT`` 秒）再把排队的上屏投出去。"""
        try:
            while True:
                with self._out_lock:
                    if not self._out_queue:
                        return
                deadline = time.monotonic() + COMMIT_OUT_MAX_WAIT
                while self._mods_held() and time.monotonic() < deadline:
                    time.sleep(0.01)
                time.sleep(COMMIT_AFTER_MODS)
                with self._out_lock:
                    batch, self._out_queue = self._out_queue, []
                for text, note in batch:
                    ok = bool(self._committer(text))
                    self._emit({"type": EV_COMMIT, "text": text, "ok": ok, "note": note})
        finally:
            with self._out_lock:
                self._out_running = False

    def _translation_fallback_note(self) -> str:
        """译文行没东西可上屏时，说清**具体**是哪一种原因。

        第 64 轮：以前不管哪种原因都写「翻译不可用（检查 API key / 网络）」。
        可实际原因可能是「你自己把 translate.enabled 关了」「用的是离线词表、
        这个词不在词表里」或者「在线请求真的失败了」—— 前两种情况下让人去查
        API key / 网络是假话，用户会去反复检查一个根本用不上的 key。
        """
        tr = self.translator
        if not getattr(tr, "enabled", True):
            return "翻译开关是关的（translate.enabled=false）—— 已改为上屏中文"
        if getattr(tr, "is_mock", False):
            why = str(getattr(tr, "mock_reason", "") or "")
            if "api.provider" in why:
                tail = "你把 api.provider 设成了 mock，走的是离线词表"
            else:
                tail = "走的是离线词表（没配 api.api_key）"
            return f"翻译{tail}，这个词不在词表里 —— 已改为上屏中文"
        err = ""
        try:
            err = str((getattr(tr, "stats", {}) or {}).get("last_error") or "")
        except Exception:  # noqa: BLE001 —— 读不到原因也要能上屏
            err = ""
        if err:
            return f"翻译没回来（{err[:80]}）—— 已改为上屏中文"
        return "翻译没回来（检查网络 / API key）—— 已改为上屏中文"

    def _commit(self, row: int, index: int | None) -> str:
        """把当前候选/缓冲上屏；返回真正上屏的文本（没上屏就是空串）。

        返回值的用处：调用方要据此说「我把什么替你先上屏了」（第 98 轮挂起热键）。
        """
        note = ""
        if row == ROW_EN and not self.en:
            if self.tr_state == "pending":
                self.status = "译文还在路上… 稍后再按一次"
                self._push_state()
                return ""
            if not self.zh:
                self.status = "没有可上屏的内容"
                self._push_state()
                return ""
            # 翻译挂了或被关掉：明确告诉用户换成了中文，而不是默默上屏一个别的东西
            note = self._translation_fallback_note()
            row = ROW_ZH
            index = self.sel[ROW_ZH]

        if row == ROW_EN:
            idx = self.sel[ROW_EN] if index is None else index
            idx = max(0, min(idx, len(self.en) - 1))
            text = self.en[idx]
        elif self.zh:
            idx = self.sel[ROW_ZH] if index is None else index
            idx = max(0, min(idx, len(self.zh) - 1))
            text = self.zh[idx]
            # 自学习：用户主动改选说明首选项不对，重罚；直接选中则轻微强化
            amount = self.boost_on_correct if idx > 0 else self.boost_on_pick
            if amount:
                self.engine.boost(text, amount)
                self._boosts_dirty = True
        elif self.buffer:
            text = self.buffer  # 兜底：整串都不是合法拼音，原样上屏
        else:
            return ""

        if not text:
            self._reset()
            return ""
        # 候选是按「最长可切分前缀」算的，缓冲尾巴上可能还有没用掉的字母
        # （`nihaoz` 的 `z`、`nihaozh` 的 `zh`）。这些字母用户敲过，不能跟着
        # `_reset()` 一起消失 —— 留在缓冲里当下一段组合的开头，等他继续打。
        rest = self._leftover_after_prefix()
        self._deliver(text, note)
        # 降级原因留在 status 上，直到用户敲下一个键（_recompute 会清掉它）
        if rest:
            self.buffer = rest
            self._recompute()
            if note:
                self.status = note
                self._push_state()
            return text
        self.status = note
        self._reset(keep_status=bool(note))
        return text

    def _reset(self, keep_status: bool = False) -> None:
        self.buffer = ""
        self.zh = []
        self.en = []
        self.sel = [0, 0]
        self._cand_prefix = ""
        self.tr_state = "idle"
        self._pass_streak = 0
        self._tr_seq += 1
        self._tr_text = ""
        if not keep_status:
            self.status = ""
        self._push_state()

    # ------------------------------------------------------------------ #
    # 修饰键纠偏 / 模式切换
    # ------------------------------------------------------------------ #
    def sync_caps(self) -> bool | None:
        """跟系统对一次 CapsLock。**只在启动和重装钩子时调**。

        为什么不能每次问：这个读数**不总是准的**（锁屏时必然是假 0；别人刚改过状态
        而我们还没处理过键盘消息时可能读到旧值）。要是每次判断都拿它覆盖，就成了
        探针里抓到的现场：用户按了一下 CapsLock（我们明明看见了那次按下），下一秒
        系统那句「关着」又把记录抹掉，字母照样进拼音缓冲 —— 白修。所以：
        **事件流是权威**，系统这份只在「我们可能漏掉过按下」的时刻（刚启动、刚重装
        钩子）用来对表。
        """
        try:
            value = self._caps_source()
        except Exception:
            value = None
        self._caps_inited = True
        if value is None:
            self.caps_fallbacks += 1
            return None
        self._caps = bool(value)
        self.caps_from_system += 1
        return self._caps

    def caps_lock(self) -> bool:
        """CapsLock 现在开着吗？（第一次问的时候顺便跟系统对一次表）"""
        if not self._caps_inited:
            self.sync_caps()
        return self._caps

    def _real_mods(self) -> frozenset[int]:
        """问系统：此刻物理上真正按着哪些修饰键。

        拿不到就返回空集合（宁可当没按，也不要凭空以为按着 —— 后者会让输入法变哑巴）。
        """
        try:
            return frozenset(self._mods_source() or ())
        except Exception:
            return frozenset()

    def resync_modifiers(self) -> bool:
        """把 ``_down`` 里的修饰键跟真实按键状态对齐，返回是否发生了变化。

        钩子只在有键盘事件时才回调，所以 **keyup 会丢**：Ctrl+Alt+Del、Win+L 锁屏、
        UAC 提权弹窗期间，钩子完全收不到事件。丢掉一次 Ctrl 抬起，Ctrl 就永远留在
        ``_down`` 里 —— 之后 ``down & BLOCKING_MODS`` 恒真，每个键都被当成组合键
        透传出去，字母再也进不了拼音缓冲，**整个输入法静默变成哑巴**，
        而用户只会说「按了没反应」。热键匹配不上也是同一个根因。

        只对修饰键纠偏，普通键不管 —— 普通键的 keyup 丢了顶多是重复触发。
        """
        real = self._real_mods()
        believed = frozenset(v for v in self._down if v in MOD_VK_SET)
        if believed == real:
            return False
        self._down -= MOD_VK_SET
        self._down |= set(real)
        if W.VK_SHIFT not in real:
            # Shift 的抬起丢了：单击判定作废，免得下次按 Shift 时把上一轮算进去
            self._shift_at = None
            self._shift_used = False
        return True

    def on_foreground_changed(self) -> bool:
        """前台窗口换了：丢掉挂起的合成，返回是否有东西被丢掉。

        否则：在编辑器里打了半截拼音没上屏，Alt+Tab 到浏览器后按空格，
        那半截就会凭空落进浏览器。丢掉的只是**还没上屏的拼音**，用户没损失什么。

        刻意不动 ``active``：那是用户显式选的上屏语言，按设计要一直粘着。
        """
        self._swallow_up.clear()
        self._shift_at = None
        self._shift_used = False
        self.resync_modifiers()  # 切窗口往往伴随 Alt+Tab，修饰键状态最容易在这时错
        if not self.buffer and not self.zh and not self.en:
            return False
        self._reset()
        return True

    def _switch_ways(self) -> list[str]:
        """「拼音 ⇄ 英文直通」现在真能用的键（第 92 轮）。

        ``toggle_direct()`` 的提示以前写死「再单击 Shift 回拼音」：``input.
        shift_tap_toggles=false`` 的人按 Shift 一点反应都没有，提示还在教他按；
        ``hotkeys.toggle_direct`` 明明配了也不提。这里和 ``tap_switch_hint`` 一个
        口径 —— 只有真能触发的键才写进句子。
        """
        from .hotkey import pretty

        ways: list[str] = []
        if self.shift_tap_toggles and self.shift_tap_ms > 0:
            window = (f"{self.shift_tap_ms / 1000:.1f} 秒内"
                      if self.shift_tap_ms >= 100 else f"{self.shift_tap_ms} 毫秒内")
            ways.append(f"点一下 Shift（{window}）")
        direct = pretty((self.cfg.get("hotkeys") or {}).get("toggle_direct"))
        if direct:
            ways.append(direct)
        return ways

    def toggle_direct(self) -> None:
        """在「拼音输入」和「英文直通」之间切（单击 Shift、托盘菜单都会调）。"""
        self.direct = not self.direct
        self._pass_streak = 0
        if self.buffer:
            # 已经打了半截拼音：按当前上屏语言落下去，别把用户打的东西扔了。
            # 译文还在路上时退回中文候选（第 101 轮，见 _settle_buffer_before_switch）。
            self._settle_buffer_before_switch()
        else:
            self.status = ""
            self._reset()
        ways = self._switch_ways()
        if self.direct:
            tail = (f" · {' 或 '.join(ways)} 回拼音" if ways else
                    " · 现在没有切回拼音的键（input.shift_tap_toggles 关着，hotkeys.toggle_direct 也没设）")
            self._toast("英文直通：字母原样输入" + tail)
        else:
            tail = (f" · {' 或 '.join(ways)} 切英文" if ways else
                    " · 现在没有切到英文直通的键（input.shift_tap_toggles 关着，hotkeys.toggle_direct 也没设）")
            self._toast("拼音输入：打字母出候选" + tail)
        self._push_state()

    def _nudge_passthrough(self) -> None:
        """直通/挂起时连打几个字母都没进缓冲 —— 提醒一次，别让它静默。

        起因是真实的用户反馈：「打字没反应，候选窗根本不出现」。
        直通模式和挂起状态都会让每个键原样透传、不弹候选窗，而用户唯一的
        线索只有切换那一刻闪过的 1.6 秒提示。所以就出现了「明明开着程序却
        像没开」的错觉。连打 ``PASSTHROUGH_NUDGE_AFTER`` 个字母提醒一次。
        """
        self._pass_streak += 1
        if self._pass_streak != PASSTHROUGH_NUDGE_AFTER:
            return
        # 提示里的组合键一律从配置里取 —— 写死 Ctrl+Alt+Z 的话，用户改过
        # hotkeys.toggle_enabled 之后，这条「怎么恢复」的提示就成了假话。
        from .hotkey import pretty
        hk = self.cfg.get("hotkeys") or {}
        if not self.enabled:
            resume = pretty(hk.get("toggle_enabled"))
            self._toast("输入法处于挂起状态，按键全部透传"
                        + (f" · {resume} 恢复" if resume else ""))
        else:
            # 第 90 轮：shift_tap_ms=0 时 held_ms <= 0 永不成立，Shift 单击根本
            # 不触发 —— 这时候再说「点一下 Shift（0.0 秒内）」就是教用户按一个死键。
            # 第 92 轮：这段和 toggle_direct() 的提示共用 _switch_ways()，别再各写一份。
            ways = self._switch_ways()
            tail = (" · " + " 或 ".join(ways) + " 切回拼音") if ways else ""
            self._toast(f"英文直通中，字母原样输入{tail}")

    def _commit_raw(self, text: str) -> None:
        """把一个字符原样上屏（Shift+字母 走这里）。

        挂起的合成必须先落下去，否则大写字母会插到拼音前面，顺序错乱。
        """
        if self.buffer:
            # 先把半截拼音落下（译文 pending 时退回中文候选），再上屏大写字母，
            # 否则拼音会插到大写字母后面（第 101 轮统一走同一个入口）。
            self._settle_buffer_before_switch()
        ok = bool(self._committer(text))
        self._pass_streak = 0
        self._emit({"type": EV_COMMIT, "text": text, "ok": ok, "note": ""})

    # ------------------------------------------------------------------ #
    # UI 通道
    # ------------------------------------------------------------------ #
    def _toast(self, text: str) -> None:
        """在候选窗上闪一条短提示。缓冲为空时也显示 —— 模式切换得让用户看得见。"""
        self.toast = text
        self._toast_until = time.monotonic() + self.toast_ms / 1000.0

    def show_toast(self, text: str, seconds: float | None = None) -> None:
        """给 UI 层（托盘菜单、启动引导）用的公开入口，和内部提示走同一条路。

        传 ``seconds`` 可以覆盖默认的显示时长（引导文案要看得久一点）。
        注意名字不能叫 ``toast``：``self.toast`` 已经是「当前提示文本」这个字符串属性了。
        """
        self._toast(text)
        if seconds is not None:
            self._toast_until = time.monotonic() + float(seconds)

    def _live_toast(self) -> str:
        return self.toast if time.monotonic() < self._toast_until else ""

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "direct": self.direct,
            "caps": self._caps,
            "toast": self._live_toast(),
            "buffer": self.buffer,

            "zh": list(self.zh),
            "en": list(self.en),
            "active": self.active,
            "sel": list(self.sel),
            "tr_state": self.tr_state,
            "tr_text": self._tr_text,
            "status": self.status,
            "mock": self.translator.is_mock,
            "hotkeys": {
                name: spec for name, spec in (self.cfg.get("hotkeys") or {}).items()
            },
        }

    def _push_state(self) -> None:
        self._emit({"type": EV_STATE, "state": self.snapshot()})

    def apply_translation_results(self) -> bool:
        """由 UI 线程调用：把后台翻译结果并进状态，返回是否有变化。"""
        changed = False
        while True:
            try:
                seq, text, out = self.worker.results.get_nowait()
            except queue.Empty:
                break
            if seq != self._tr_seq:
                continue  # 过期结果，丢掉
            self.en = [x for x in out if x][: self.variant_count]
            self.sel[ROW_EN] = 0
            self.tr_state = "done" if self.en else "failed"
            if not self.en:
                self.status = "翻译失败（检查 API key / 网络）"
            changed = True
        if changed:
            self._push_state()
        return changed

    # ------------------------------------------------------------------ #
    # 自学习权重落盘
    # ------------------------------------------------------------------ #
    def load_boosts(self) -> int:
        p = self.boost_path
        if not p.exists():
            return 0
        try:
            # utf-8-sig：手工编辑过的 user_boosts.json 可能带 BOM，
            # 用 utf-8 读会直接抛、整份自学习权重被丢掉。
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            self._boost_load_error = f"{type(exc).__name__}: {exc}"
            keep = self._keep_broken_boosts(p)
            print(
                f"[core] 读取自学习权重 {p} 失败：{exc}"
                f"（已把这份文件留成 {keep.name}，下次保存不会覆盖它；"
                f"这次自学习从零开始）"
            )
            return 0
        if isinstance(data, dict):
            # 坏值在引擎里逐项跳过，这里只负责把「丢了几项」说清楚 ——
            # 静默丢和崩掉一样糟：用户改过文件，就该知道哪一项没被认。
            self._boost_load_error = ""
            skipped = self.engine.load_boosts(data)
            if skipped:
                print(
                    f"[core] 自学习权重 {p} 里有 {skipped} 项不是数字，已忽略"
                    f"（其余 {len(data) - skipped} 项照常生效）"
                )
            return max(0, len(data) - skipped)
        self._boost_load_error = f"内容不是字典（拿到 {type(data).__name__}）"
        keep = self._keep_broken_boosts(p)
        print(
            f"[core] 自学习权重 {p} 的内容不是字典（拿到 {type(data).__name__}），已忽略"
            f"（已把这份文件留成 {keep.name}，下次保存不会覆盖它）"
        )
        return 0

    def _keep_broken_boosts(self, p: Path) -> Path:
        """把读不懂的权重文件改名留证，绝不就地覆盖。

        以前读失败只打一行日志就继续，下一次 ``save_boosts()`` 会直接把这份文件
        写成新内容 —— 用户那份坏文件（也许只差一个逗号）就永久没了，而他唯一的
        线索是一行可能根本没看到的日志。改名之后原文件还在 ``.broken`` 后缀上。
        """
        target = p.with_name(f"{p.name}.broken")
        n = 2
        while target.exists():
            target = p.with_name(f"{p.name}.broken{n}")
            n += 1
        try:
            p.replace(target)
        except Exception as exc:  # noqa: BLE001
            print(f"[core] 备份自学习权重 {p} 失败（不影响使用）：{exc}")
            return p
        return target

    def save_boosts(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not self._boosts_dirty:
            return False
        if not force and now - self._last_boost_save < BOOST_SAVE_INTERVAL:
            return False
        data = self.engine.user_boosts()
        if not data:
            return False
        # 父目录不存在就建出来：``output.boost_file`` 指到 ``data/boosts.json``
        # 这种子目录是很自然的配置，而这里以前直接 write_text —— 目录不在就永远
        # 写不进去，自学习等于没开（日志、拼音索引缓存都会自己建目录，这里没理由不建）。
        # 失败时也不许每个心跳刷一行：错误文本没变就只报一次，并把重试压到
        # BOOST_SAVE_INTERVAL 一拍，免得日志被灌满（第 81 轮真机 ~2 行/秒）。
        try:
            self.boost_path.parent.mkdir(parents=True, exist_ok=True)
            self.boost_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            # 「父路径被一个文件占住了」比原始的 OS 错误码好懂：报错里直接说清楚。
            parent = self.boost_path.parent
            if parent.exists() and not parent.is_dir():
                why = f"{parent} 是个文件，不是目录"
            else:
                why = f"{type(exc).__name__}: {exc}"
            if why != self._boost_error:
                print(
                    f"[core] 写入自学习权重 {self.boost_path} 失败：{why}"
                    f"（这次记下的 {len(data)} 个词没能落盘；改好 output.boost_file 后会自动补写）"
                )
            self._boost_error = why
            self._last_boost_save = now
            return False
        self._boost_error = ""
        self._boosts_dirty = False
        self._last_boost_save = now
        return True

    @property
    def boost_error(self) -> str:
        """上一次自学习落盘失败的原因（空 = 最近一次是好的）。"""
        return self._boost_error

    @property
    def boost_load_error(self) -> str:
        """读权重文件时出的错（空 = 读懂了，或者根本没有这份文件）。"""
        return self._boost_load_error

    def shutdown(self) -> None:
        self.save_boosts(force=True)
        self.worker.stop()
