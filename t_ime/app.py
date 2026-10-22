# -*- coding: utf-8 -*-
"""编排层：把键盘钩子、候选窗、状态机、翻译工人缝成一个能跑的程序。

线程模型（只有两个线程，没有别的地方碰共享状态）::

    主线程   Tk mainloop ──每 16ms──> 抽事件队列 / 取翻译结果 / 重画候选窗
    钩子线程 Windows 低级键盘钩子回调 -> IMECore.handle_key -> emit 事件入队
    翻译线程 TranslateWorker        -> results 队列

``IMECore`` 只被钩子线程写；Tk 侧只通过 ``snapshot()`` 读，并且在 Tk 线程里
调 ``apply_translation_results()`` 把译文搬进来。所以没有锁。托盘事件队列同理，
只由托盘线程 put、Tk 线程 get。
"""
from __future__ import annotations

import os
import queue
import sys
import time
import traceback
from pathlib import Path

from . import winapi as W
from .core import EV_COMMIT, EV_QUIT, EV_STATE, IMECore, TranslateWorker
from .overlay import Overlay
from .pinyin import PinyinEngine, load_or_build_index
from .translator import Translator

TICK_MS = 16
# 空转时的心跳间隔。**这是一个真实存在过的耗电 bug 的修法。**
#
# 以前主循环永远按 16 毫秒一拍跑（约 60 次/秒），不管屏幕上有没有东西。真机实测
# （真 pythonw 常驻实例，空转 —— 不打字、候选窗关着）：烧掉 **3.8%~5.9% 的一个核**，
# 其中 bare Tk 的 `after(16)` 空循环本身就占 1.4%，剩下的才是每拍里干的活
# （收件箱轮询一次 60µs，等等）。常驻输入法 90% 的时间在空转，这笔钱要背一整天。
#
# 实测同样一段 bare Tk 空循环：16ms → 1.41%，50ms → 0.47%，250ms → 0%。
# 取 50ms 是折中：用户停手两秒以上、又敲下第一个键时，最多多等 50 毫秒才看到
# 候选窗 —— 这一次 50ms 的延迟在感知阈值附近；换来的是空转开销掉一个数量级。
IDLE_TICK_MS = 50
# 「空转」的判定：这么久没有任何按键事件、候选窗也关着。
IDLE_AFTER_S = 2.0
# 收件箱（第二个进程投的命令：弹面板 / 退出）的轮询间隔。一次 stat + open 要 60µs，
# 每 16 毫秒做一次就是 0.4% 一个核；而「第二次双击弹出面板」晚 200 毫秒没人看得出来。
CMD_POLL_MS = 200
# 修饰键状态跟系统核对一遍的间隔。钩子的 keyup 会丢（锁屏 / Ctrl+Alt+Del / UAC），
# 而卡住的 Ctrl 会让输入法整个变哑巴，所以得定期体检。
RESYNC_MS = 500
# 状态文件多久刷一次。只在内容变了才真写盘，所以这个值大一点也没关系。
STATUS_MS = 1000
# 键盘钩子探活的采样间隔。Windows 会因回调超时或长时间收不到系统消息**悄悄摘掉**
# 低级钩子（不报错、不通知，句柄和线程都还在），那之后的症状是「打字没反应」。
# 查证的办法只能是「拿系统自己的按键状态跟钩子收到的对账」，所以每 50 毫秒采一次样
# （实测一次全扫 0.21 毫秒，20 次/秒 ≈ 0.4% 单核）。
#
# 为什么不用「注入自检按键」那招：常驻实例是 pythonw、候选窗又是故意不抢焦点的，
# 进程自己没有前台窗口 —— 而 UIPI 会把这种进程的 SendInput 直接挡掉，返回值照样
# 是成功，按键却连系统都没进。那样的自检在常驻模式下**只会永远失败**，
# 于是「自动重装」会变成每隔半分钟折腾一次还怪钩子不听话。注入自检留给
# `main.py --hook-check`（那里我们会自己造一个前台窗口）。
HOOK_SAMPLE_MS = 50
# 记下「疑似漏键」之后再等多久。钩子回调偶尔会慢半拍，250 毫秒内补上就不算漏。
HOOK_MISS_CONFIRM_MS = 250
# 连着几次确认漏键才判定「钩子真死了」并重装。重装会短暂丢掉正在按的键，
# 宁可多对一次账也不要误重装。
HOOK_MISSES_TO_REINSTALL = 2
# 启动引导在屏幕上停留多久。没有控制台（后台常驻）时这是用户唯一能看到的说明。
WELCOME_SECONDS = 7.0
WELCOME_TIP_SECONDS = 3.0

# 三个「开关」动作由状态机处理（和热键走同一条 _fire 路径，保证托盘和键盘不会各说各话）。
CORE_ACTIONS: tuple[str, ...] = ("toggle_enabled", "toggle_direct", "toggle_default")
# 托盘菜单的**唯一**声明处：(item_id, 标签, 前面要不要加分隔线)。
# 控制面板（双击运行时的菜单）发的是同一批 item_id —— tests/test_tray_menu.py 会盯着两边不跑偏。
MENU_ITEMS: tuple[tuple[str, str, bool], ...] = (
    ("toggle_enabled", "启用输入法", False),
    ("toggle_direct", "英文直通（不转拼音）", False),
    ("toggle_default", "上屏译文（否则中文）", False),
    ("status", "当前状态…", True),
    ("help", "使用帮助…", False),
    ("open_log", "打开日志文件", False),
    ("open_config", "打开配置文件", False),
    ("quit", "退出", True),
)
MENU_IDS: tuple[str, ...] = tuple(item_id for item_id, _, _ in MENU_ITEMS)


class App:
    def __init__(self, cfg: dict, root_dir: Path, *, verbose: bool = True, install_hook: bool = True,
                 log_path: Path | None = None, config_path: Path | None = None):
        self.cfg = cfg
        self.root_dir = Path(root_dir)
        self.verbose = verbose
        self.install_hook = install_hook
        # 启动时用的是**哪一份** config.json（--config 可以指向别处）。状态文件里
        # 必须写清楚：体检和控制面板读的是「本目录的 config.json」，而实例完全
        # 可能在用另一份 —— 不写的话，外面会把另一份配置的热键/开关当成实例的
        # 现状报出来（第 60 轮：体检说「热键 quit ctrl+alt+q」，实例其实是 F9）。
        self._config_path = Path(config_path).resolve() if config_path else None
        # 日志到底写在哪：命令行 --log 的优先级比配置高，而 main.py 在构造 App
        # **之前**就把路径算好了（setup_file_logging 用的就是它）。以前 App 自己
        # 又按「配置里那个名字」重算一遍，于是 --log 指向别处时，状态文件里写的
        # 是另一个路径、托盘上的「打开日志文件」打开的也是另一个文件 —— 一个
        # 不报错、只是把人指到错地方的谎。这里让调用方把真实路径交进来。
        self._log_file = Path(log_path) if log_path else None
        self.events: queue.Queue = queue.Queue()
        self._running = False

        cache = self.root_dir / cfg["pinyin"]["dict_cache"]
        if verbose:
            print(f"[app] 载入拼音索引（{cache.name}）…")
        index = load_or_build_index(
            cache,
            min_freq=int(cfg["pinyin"]["min_freq"]),
            max_word_len=int(cfg["pinyin"]["max_word_len"]),
            verbose=verbose,
        )
        self.engine = PinyinEngine(
            index,
            beam_width=int(cfg["pinyin"]["beam_width"]),
            max_candidates=int(cfg["pinyin"]["max_candidates"]),
        )
        if not index:
            # 空索引 = 打什么都是「没候选」，但程序本身看起来一切正常。
            # 这种「启动了、也装上了钩子、就是不出字」最容易让人以为是键盘坏了，
            # 所以启动时就把原因和参数摆出来（第 79 轮）。
            print(
                f"[app] 拼音索引是空的（0 个键）—— 打字不会有候选。"
                f"检查 pinyin.min_freq（现在 {int(cfg['pinyin']['min_freq'])}，设太大就全被筛掉），"
                f"或删掉 {cache.name} 重建一次。"
            )
        self.translator = Translator(cfg, verbose=verbose)
        self.worker = TranslateWorker(
            self.translator, debounce=cfg["translate"]["debounce_ms"] / 1000.0, verbose=verbose
        )

        self.overlay = Overlay(cfg, verbose=verbose)
        self.core = IMECore(
            cfg, self.engine, self.translator,
            emit=self._on_event, committer=self._commit, verbose=verbose,
        )
        # 候选窗底部那格「换语言」只能写真正注册上的键（第 115 轮）。
        # Overlay 建在 core 之前，所以等 core 解析完 hotkeys 再补这一句 ——
        # 否则配了 `toggle_default: "l"`（被拒绝注册）时，窗口底部会教用户按 L。
        self.overlay.set_switch_hint(self.core.registered_hotkeys.get("toggle_default") or "")
        self.core.enabled = bool(cfg["output"].get("start_enabled", True))
        n = self.core.load_boosts()
        if verbose and n:
            print(f"[app] 载入 {n} 条自学习词权重")
        if cfg["output"].get("default_language", "zh") == "en":
            self.core.active = 1

        self.hook = None
        self._fg_hwnd = W.foreground_hwnd()
        self._last_resync = 0.0
        self._started_at = time.time()
        self._last_publish = 0.0
        self._status_sig: dict | None = None
        # 状态文件的心跳时刻（time.monotonic）。状态文件的 mtime 是外面判断
        # 「这个实例还活着吗」的依据，所以状态没变也得定期写一次 —— 否则用户
        # 十来秒不打字，体检就会说它「可能卡死或被强杀了」。
        self._last_write = 0.0
        self._status_write_warned = False          # 写不进去只吵一次，别刷屏
        self._tick_errors = 0
        self._last_tick_error: str | None = None
        # 钩子探活/自愈：Windows 会悄悄摘掉低级钩子，alive 看不出来（见 winapi.sample_keydowns）
        self._last_sample = 0.0
        self._hook_misses = 0                    # 已确认「系统有按键、钩子没收到」的次数
        self._miss_pending = False               # 刚发现疑似漏键，正等钩子补上
        self._miss_at = 0.0                      # 疑似漏键的时刻
        self._miss_total = 0                     # 疑似漏键时的对账基线（上一次采样时的事件数）
        self._last_total = -1                    # 上一次体检读到的钩子事件总数（-1 = 还没读过）
        self._hook_reinstalls = 0
        self._hook_ok_since_reinstall = False    # 上次重装之后探活确实对上过账没有
        self._hook_giveup = False                # 重装也救不回来，别再折腾
        # 「对账成功」的次数：它 >0 就说明探活这条线在常驻实例里**真的跑起来了**
        # （否则「没报漏键」可能只是因为我们压根没在采样）。诊断里要看这个数。
        self._hook_reconciles = 0

        # 空转降频（见 IDLE_TICK_MS 那段注释）：最近一次「有动静」的时刻，
        # 以及上一拍实际排了多少毫秒（诊断 / 测试要能看见它）。
        self._last_activity = time.monotonic()
        self._last_tick_ms = TICK_MS
        # 收件箱上次轮询的时刻（0 = 还没轮询过，第一拍立刻轮询一次）
        self._last_cmd_poll = 0.0

        # 托盘：常驻之后用户没有控制台可看，托盘是「我在跑 / 我想干啥」的唯一入口
        self.tray = None
        self.tray_actions: queue.Queue = queue.Queue()
        self._tray_sig = None
        self._warnings: list[str] = []
        # 引导播完了没有。播完之后才产生的坏消息（钩子被摘掉、自动重装、托盘被拒）
        # 以前只在 stderr 里喊 —— 而常驻时没有控制台，等于什么都没说。
        self._welcome_done = False

        # 上屏失败：SendInput 送不出去（UIPI 拦下比我们权限高的窗口、安全软件
        # 拦截）时，以前用户**什么都看不到** —— 候选窗一关、缓冲一清，打出来的
        # 字凭空消失，正是「打字没反应」最常见的一种。现在要看得见、还要能补救。
        self._commit_reason = ""                 # 上一次上屏为什么失败（钩子线程写，主线程读）
        self._commit_failures = 0
        self._last_commit_error = ""
        self._inject_cache: dict[int, bool] = {}  # pid -> 能不能往它里面注入
        self._inject_warned = False               # 权限墙只解释一次，别刷屏

        # 自学习权重落盘了几次。以前「学到的东西」只有干净退出那一刻才写盘 ——
        # 被强杀 / 崩 / 注销就等于白学，而常驻用户几乎从不主动退出。
        # 现在心跳里定期存（core 内部有节流），这个计数进状态文件，外面能看见。
        self._boost_saves = 0
        # Web 控制板保存配置后会往命令文件里投一个 reload：这里记「重载了几次」
        # 和「上一次为什么没成」，面板/体检据此判断「保存」是真的生效了，
        # 而不是只把文件写了一遍（第 108 轮）。
        self._config_reloads = 0
        self._last_reload_error = ""
        self._reload_notes: list[str] = []

    # ------------------------------------------------------------------ #
    # 启动引导
    # ------------------------------------------------------------------ #
    def _welcome(self) -> None:
        """开机引导。后台常驻时没有控制台，这是用户唯一能看到的「怎么用」。"""
        secs = self.show_help()
        # 引导消失后再补一条更短的，防止用户正好没看屏幕
        self.overlay.after(int(secs * 1000) + 300, self._welcome_tip)
        if self.verbose:
            print(f"[app] 已显示启动引导（{secs:.0f} 秒）")

    def _add_warning(self, text: str) -> None:
        """记下一条「用户会以为程序坏了」的坏消息，等引导播完在候选窗上说出来。

        为什么要绕这一圈：托盘装不上、钩子装不上，这两种情况都只往 stderr 喊。
        而后台常驻时压根没有控制台，用户只看得见候选窗 —— 结果就是「打字没反应」
        却完全不知道原因。

        注意这些坏消息**不一定**发生在启动那一刻：钩子被系统摘掉、自动重装、放弃重装
        都是常驻半小时以后才可能发生的事，那时引导早就播完了。所以冲刷不能只挂在
        引导的尾巴上 —— ``_tick_once`` 每一拍都会来问一次（见 ``_flush_warnings``）。
        """
        self._warnings.append(text)

    def _flush_warnings(self) -> bool:
        """把攒下的坏消息弹到候选窗上，返回有没有弹。

        三个条件缺一不可：引导已经播完（否则会把引导本身顶掉）、有坏消息、
        用户此刻没在打字（正打拼音时弹一条 8 秒的提示是在碍事，下一拍再说）。
        """
        if not self._warnings or not self._welcome_done:
            return False
        if self.core.buffer:
            return False
        text = "\n".join(self._warnings)
        self._warnings.clear()
        # 8 秒：比启动提示长，因为这条信息用户没预期会看到，要留够读的时间
        self.core.show_toast(text, seconds=8.0)
        return True

    def _welcome_tip(self) -> None:
        self._welcome_done = True
        if self._flush_warnings():
            return
        if self.core.buffer:
            return  # 用户已经在打字了，别打断
        state = "已启用" if self.core.enabled else "已挂起"
        # 「怎么切 / 怎么恢复」必须跟着配置：input.shift_tap_toggles 关掉之后
        # 「点一下 Shift」什么都不做；而挂起状态连 Shift 都不响应（core 里那条件是
        # tapped and shift_tap_toggles and enabled），只有 toggle_enabled 能恢复。
        from .hotkey import direct_switch_hint, resume_hint

        if self.core.enabled:
            switch = direct_switch_hint(self.cfg)
            switch_part = f" · {switch} 切英文" if switch else ""
        else:
            resume = resume_hint(self.cfg)
            switch_part = f" · {resume} 恢复" if resume else " · （用面板菜单恢复）"
        self.core.show_toast(
            f"translate-ime {state}{switch_part} · {self._quit_spec()} 退出",
            seconds=WELCOME_TIP_SECONDS,
        )

    def _hotkey_spec(self, name: str) -> str:
        """把**真正注册上的**那个热键说给人听。

        空字符串是**合法**的配置（``IMECore._parse_hotkeys`` 会把空 spec 跳过，
        也就是「关掉这个键」）。所以这里不能直接 ``pretty("")`` —— 那会在引导里
        留下一个空槽（``4.  一键直译上屏``），用户只能对着空白猜。

        而且必须读 ``core.registered_hotkeys``，不能读配置：配置里写着
        ``quit: "q"`` 时核心会拒绝注册（没有修饰键，打字会误触），这时还说
        「按 Q 退出」就是教用户去按一个不存在的功能；动作名写错、两个动作撞车
        输掉时同理。
        """
        spec = self.core.registered_hotkeys.get(name) or ""
        return spec or "（没设热键）"

    def _quit_spec(self) -> str:
        return self._hotkey_spec("quit")

    def _mode_step_text(self) -> str:
        """引导第 3 步：「拼音 ⇄ 英文直通」按什么。

        以前写死「点一下 Shift」。可 ``input.shift_tap_toggles`` 是合法配置，关掉它的人
        照着引导去点 Shift，什么都不会发生；而 Shift 单击本身也只在输入法没挂起时有效。
        所以这里按配置拼：Shift 单击可用就写它（带真实时间窗），否则写
        ``hotkeys.toggle_direct``；两个都没有就直说没有，而不是留个假动作。
        """
        from .hotkey import direct_switch_hint, shift_tap_off_reason, tap_switch_hint

        if tap_switch_hint(self.cfg):
            return (f"3. 点一下 Shift 切「拼音 / 英文直通」"
                    f"（别按住超过 {self._tap_window_phrase()}）")
        switch = direct_switch_hint(self.cfg)
        if switch:
            return f"3. {switch} 切「拼音 / 英文直通」"
        why = shift_tap_off_reason(self.cfg) or "input.shift_tap_toggles 关着"
        return (f"3. 「拼音 / 英文直通」现在没有切换键"
                f"（{why}，hotkeys.toggle_direct 也没设）")

    def _tap_window_phrase(self) -> str:
        """「别按住超过 X」里的 X：够长就写秒，不足 100 毫秒就写毫秒（别写 0.0 秒）。"""
        ms = int(self.core.shift_tap_ms)
        return f"{ms / 1000:.1f} 秒" if ms >= 100 else f"{ms} 毫秒"

    def show_help(self, seconds: float = WELCOME_SECONDS) -> float:
        """把使用说明显示在候选窗上，返回它停留的秒数。"""
        lines = [
            "translate-ime 已就绪",
            "· 语言栏里没有它是正常的：一启动就接管字母，直接打拼音",
            "1. 直接打拼音，候选窗里选中文（空格上屏）",
            f"2. {self._hotkey_spec('toggle_default')} 切「上屏中文 / 上屏译文」",
            self._mode_step_text(),
            f"4. {self._hotkey_spec('compose')} 一键直译上屏",
            f"5. {self._hotkey_spec('toggle_enabled')} 挂起 · {self._quit_spec()} 退出",
        ]
        self.core.show_toast("\n".join(lines), seconds=seconds)
        return seconds

    # ------------------------------------------------------------------ #
    # 托盘
    # ------------------------------------------------------------------ #
    def _start_tray(self) -> None:
        """起托盘图标。失败不致命：没有托盘也照常能打字，只是少了操作入口。"""
        if not bool(self.cfg["output"].get("tray", True)):
            return
        try:
            from .tray import TrayIcon, _default_icon_path
        except Exception as exc:  # pragma: no cover - 只在文件缺失时发生
            print(f"[app] 托盘模块不可用（{exc!r}），跳过", file=sys.stderr)
            return

        icon = _default_icon_path()
        self.tray = TrayIcon(
            self._tray_tip(), icon if icon.exists() else None,
            self.tray_actions, verbose=self.verbose,
        )
        for item_id, label, sep in MENU_ITEMS:
            self.tray.add_item(item_id, label, separator_before=sep)

        self.tray.start()
        self._sync_tray(force=True)
        if not self.tray.tray_added:
            # 说清楚：图标没出现不是我们崩了，是系统拒绝了 NIM_ADD
            print(f"[app] 托盘图标没能装上：{self.tray.last_error}", file=sys.stderr)
            # 这里也走 _quit_spec()：写死 Ctrl+Alt+Q 的话，用户把 hotkeys.quit
            # 改成别的组合键之后，这条「怎么退出」的指引就成了假话。
            print(f"[app] 不影响打字，{self._quit_spec()} 仍可退出。", file=sys.stderr)
            print("[app] 想知道确切原因，跑：run.bat --tray-check", file=sys.stderr)
            # 光打 stderr 是不够的：后台常驻时压根没有控制台，用户只看得见候选窗。
            # 所以引导播完之后，在候选窗上再弹一次。
            self._add_warning(
                "托盘图标没能装上（不影响打字）\n"
                f"{self._short_reason()}\n"
                f"诊断：run.bat --tray-check · 退出：{self._quit_spec()}"
            )
        elif self.verbose:
            print("[app] 托盘图标已就绪")

    def _short_reason(self) -> str:
        """把托盘的长篇错误压成一行，好放进候选窗的短提示里。"""
        raw = (self.tray.last_error or "") if self.tray is not None else ""
        if "GetLastError=5" in raw:
            return "原因：shell 拒绝了本次调用（ERROR_ACCESS_DENIED）——多半是本进程完整性级别低于 explorer"
        return raw.splitlines()[0] if raw else "原因未记录"

    def _tray_tip(self) -> str:
        state = "开" if self.core.enabled else "停"
        mode = "英文直通" if self.core.direct else ("中文" if self.core.active == 0 else "译文")
        return f"translate-ime（{state} · {mode}）"

    def _sync_tray(self, *, force: bool = False) -> None:
        """把状态机的状态刷到托盘菜单上。状态没变就什么都不做。"""
        if self.tray is None:
            return
        sig = (self.core.enabled, self.core.direct, self.core.active)
        if not force and sig == self._tray_sig:
            return
        self._tray_sig = sig
        try:
            self.tray.set_item("toggle_enabled", checked=self.core.enabled)
            self.tray.set_item("toggle_direct", checked=self.core.direct)
            # 标签是「这个开关管什么」，勾才是「现在开着没有」——不要两个都说状态
            self.tray.set_item("toggle_default", checked=self.core.active == 1)
            self.tray.set_tooltip(self._tray_tip())
        except Exception as exc:
            if self.verbose:
                print(f"[app] 刷新托盘菜单失败：{exc!r}", file=sys.stderr)

    def _handle_tray_action(self, action: str) -> None:
        if action == "__activate__":
            self.core.show_toast(
                f"输入法{'已启用' if self.core.enabled else '已挂起'} · "
                f"{'英文直通' if self.core.direct else ('拼音→中文' if self.core.active == 0 else '拼音→译文')}"
            )
            return
        if action == "status":
            self.core.show_toast(
                f"{'启用' if self.core.enabled else '挂起'} · "
                f"{'英文直通' if self.core.direct else '拼音'} · "
                f"上屏{'译文' if self.core.active == 1 else '中文'}"
            )
            return
        if action == "help":
            self.show_help()
            return
        if action == "open_log":
            self._open_path(self._log_path())
            return
        if action == "open_config":
            from .config import EXAMPLE_PATH
            # 打开**这个实例真正在读的**那一份：--config 可以把实例指到别处，
            # 而托盘/面板的「打开配置文件」以前写死 root/config.json —— 用户改完
            # 那份，实例读的还是另一份，改动永远不生效（第 61 轮）。
            cfg_file = self._config_path or (self.root_dir / "config.json")
            if cfg_file.exists():
                print(f"[app] 打开配置文件：{cfg_file}")
                self._open_path(cfg_file)
            else:
                # 实例是拿默认值跑起来的（或 --config 指了个还不存在的路径）：
                # 打开模板，并且说清楚「这不是你正在读的那份」。
                self.core.show_toast(
                    f"启动用的配置文件还不存在：{cfg_file.name}（正在用默认值），"
                    "已打开模板 config.example.json")
                print(f"[app] 配置文件不存在：{cfg_file}；改为打开模板 {EXAMPLE_PATH}")
                self._open_path(EXAMPLE_PATH)
            return
        if action == "quit":
            self.core.quit_requested = True
            return
        if action == "reload":
            # Web 控制板 / 手动改完 config.json 后让实例重新读一遍。走命令文件
            # 而不是让面板直接调进程内函数：面板是**另一个进程**，它只能通过
            # 收件箱跟实例说话（第 108 轮）。
            self.reload_config(source="命令")
            return
        # 三个开关交给状态机 —— 热键走的是同一条 _fire 路径，保证托盘和键盘不会各说各话。
        if action in CORE_ACTIONS:
            self.core._fire(action)
            return
        # 走到这里说明菜单项和处理器对不上了（改菜单时最容易犯的错）。宁可吵一声，
        # 也不要像以前那样默默丢给 _fire，只在日志里留一行「未知热键动作」。
        print(
            f"[app] 不认识这个菜单动作：{action!r}（菜单项与处理器不同步了）",
            file=sys.stderr,
        )

    def _log_path(self) -> Path:
        """日志文件在哪：优先用调用方算好的那个（命令行 --log 可能覆盖配置）。

        这个路径会进状态文件，也是托盘/面板「打开日志文件」要打开的东西 ——
        它要是错的，用户就会被指到一个不存在或早就没在写的文件上。
        """
        if self._log_file is not None:
            return self._log_file
        try:
            from . import daemon
            return daemon.resolve_log_path(self.root_dir, None, self.cfg)
        except Exception:
            return self.root_dir / "translate-ime.log"

    def _open_path(self, path: Path) -> None:
        if not path.exists():
            self.core.show_toast(f"文件还不存在：{path.name}")
            return
        try:
            os.startfile(str(path))  # noqa: S606 - 只是用系统默认程序打开自己的文件
        except Exception as exc:
            print(f"[app] 打不开 {path}：{exc!r}", file=sys.stderr)

    # ------------------------------------------------------------------ #
    # 事件 / 上屏
    # ------------------------------------------------------------------ #
    def _on_event(self, ev: dict) -> None:
        self.events.put(ev)

    def _inject_blocked(self, pid: int) -> str:
        """UIPI 会不会把我们的模拟按键拦在这个进程的窗口外面（带缓存）。

        返回空串 = 可以送；否则是**人话的原因**。缓存按 pid 存：上屏是热路径，
        每次提交都去开一次令牌太浪费。原因里带了目标进程名，所以缓存不会串味
        —— 同一个 pid 在进程退出前不会变成别的程序。
        """
        hit = self._inject_cache.get(pid)
        if hit is None:
            hit = W.inject_blocked_reason(pid)
            if not hit:
                # 原因里带进程名，读一次就够，之后直接用缓存
                hit = ""
            self._inject_cache[pid] = hit
        return hit

    def _commit(self, text: str) -> bool:
        """真正上屏：用 KEYEVENTF_UNICODE 直接注入到当前焦点窗口。

        两条「送不到」的路都要留下原因（``_commit_reason``），因为以前这里**只返回
        一个 False**，而调用方拿到之后什么都不说 —— 用户看到的就是打出来的字凭空
        消失，控制台（常驻时根本没有）里连一行都没有。
        """
        if not text:
            return False
        self._commit_reason = ""
        # SendInput 只送到当前前台窗口。万一前台被我们自己（的候选窗）占了，
        # 注入的字就会掉进自己窗口、用户什么都看不到。上屏前先确认这件事。
        target = 0
        mine = False
        try:
            fg = W.foreground_hwnd()
            if fg and W.window_pid(fg) == os.getpid():
                self.overlay.restore_foreground()
                fg = W.foreground_hwnd()
            target = W.window_pid(fg) if fg else 0
            mine = bool(target) and target == os.getpid()
        except Exception:
            target = 0
        if mine or not target:
            # 前台还是我们自己（还原没成功：那个窗口多半权限比我们高，抢不回来），
            # 或者压根没有前台窗口。这两种情况下 send_text 都会把字打进候选窗
            # 自己身上，用户看到的是「打字没反应」—— 宁可不送，把话说清楚。
            self._commit_reason = (
                "前台窗口还在本程序自己手里，没能还给你正在用的那个窗口"
                if mine
                else "找不到可以接收文字的前台窗口"
            )
            if self.verbose:
                print(f"[app] 上屏 {text!r} -> 拒绝：{self._commit_reason}")
            return False
        if target and target != os.getpid():
            blocked = self._inject_blocked(target)
            if blocked:
                # 目标窗口的权限比我们高。UIPI 会把按键拦在门外，而且 SendInput
                # 还会**假装成功**（第 13 轮实测），所以干脆不送，把话说清楚。
                name = Path(W.process_name(target) or "那个窗口").name
                self._commit_reason = f"{name} 不收模拟按键：{blocked}"
                if self.verbose:
                    print(f"[app] 上屏 {text!r} -> 拒绝：{self._commit_reason}")
                return False
        ok = W.send_text(text)
        if not ok:
            self._commit_reason = "SendInput 没能把文字送出去（被系统或安全软件拦下了）"
        if self.verbose:
            print(f"[app] 上屏 {text!r} -> {'ok' if ok else '失败：' + self._commit_reason}")
        return ok

    # ------------------------------------------------------------------ #
    def _recover_leftovers(self) -> None:
        """启动时清理上一个实例的残留（状态文件 + 收件箱）—— 崩溃恢复。

        正常退出时 ``shutdown()`` 会 ``daemon.clear_status()``，两个文件一起清。但上个
        实例若是被**强杀**（``taskkill /F``、崩溃、注销、断电），清理根本没机会跑：
        收件箱里可能还留着一条 ``quit``，新实例照单全收就会当场自杀 —— 用户看到的是
        「双击了 run.bat，输入法却没起来」，且日志里只有一行 quit，极难归因。
        残留的状态文件同理：它描述的是一个**已经不存在的进程**，留着会让
        ``--doctor`` 的 ``[0]`` 段把死掉的实例报成活的。

        命令属于上一个生命周期，一律丢掉（内容写进日志，让它有据可查）。
        """
        from . import daemon

        existed = daemon.status_path(self.root_dir).exists()
        try:
            stale = daemon.discard_commands(self.root_dir)
        except Exception as exc:
            print(f"[app] 清理残留命令失败（忽略）：{exc}", file=sys.stderr)
            stale = []
        if stale:
            print(f"[app] 丢掉了上一个实例残留的 {len(stale)} 条命令"
                  f"（它多半是被强杀的，没来得及自己清理）：{stale}")
        if existed:
            print("[app] 覆盖了上一个实例残留的状态文件")
        daemon.clear_status(self.root_dir)

    def start(self) -> None:
        self._recover_leftovers()
        self.worker.start()
        self._start_tray()
        if self.install_hook:
            self.hook = W.KeyboardHook(self.core.handle_key, "t-ime")
            if not self.hook.start():
                print(f"[app] 键盘钩子安装失败：{self.hook.error}", file=sys.stderr)
                print("[app] 可能被安全软件拦截，或已有更高优先级的钩子。", file=sys.stderr)
                # 钩子装不上 = 打字完全没反应。这是最容易被误判成「程序坏了」的状态，
                # 所以除了控制台，还要在候选窗上明说。
                self._add_warning(
                    "键盘钩子没能装上，打拼音不会有反应\n"
                    f"原因：{self.hook.error or '未记录'}\n"
                    "多半是安全软件拦了全局键盘钩子"
                )
            elif self.verbose:
                print("[app] 键盘钩子已就绪")
            self._last_sample = time.monotonic()
            self._last_total = -1  # 刚装好的钩子还没读过事件数，第一次体检只建基线
        # 我们刚起来，之前那些 CapsLock 按下（如果用户本来就开着）我们没看见 ——
        # 跟系统对一次表。**故意放在 if 外面**：`--no-hook` 调试模式下也要对表，
        # 否则状态文件里的 `caps` 会照着默认的 False 谎报「大写锁定 关」。
        self.core.sync_caps()
        self._print_banner()
        self._running = True
        self.overlay.after(TICK_MS, self._tick)
        # 起来 0.6 秒后再弹引导：等窗口把第一帧画完，免得位置算到一半
        self.overlay.after(600, self._welcome)
        self.overlay.run()

    def _check_hook_health(self, now: float) -> None:
        """问系统「刚才有人按键吗」，再跟钩子收到的事件对账 —— 对不上就是钩子漏了。

        为什么这么绕：``KeyboardHook.alive`` 只能说明线程还在、句柄还在，而 Windows
        摘掉低级钩子时**没有任何通知**（回调超时 / 长时间收不到系统消息），句柄和线程
        原封不动，用户在那之后看到的就是「打字没反应」，老版本只能靠「重启程序」绕过。
        「注入自检按键」那招在常驻模式下用不了（pythonw 没有自己的前台窗口，UIPI 会把
        注入整个挡掉，见 ``winapi.HOOK_PROBE_VK``），所以改成对账：系统自己的按键状态说
        「刚才有键被按下」，钩子却一个事件都没多 —— 那就是钩子不在链子上了。
        """
        hook = self.hook
        if hook is None or hook.error:
            return  # 没装钩子 / 装的时候就已经失败过，引导里已经告诉用户了
        total = hook.stats()["total"]
        # 对账基线要用**上一次**体检时的事件数，不能用这一次的：低级钩子的回调跑在输入
        # 送进系统之前，所以「钩子收到这个键」常常发生在这次采样之前 —— 拿当前 total
        # 当基线的话，一个被正常收到的键会因为「总数没再涨」被判成漏键（真机回归里
        # 就真的这么误报过一次，还白白重装了钩子）。
        prev_total = self._last_total if self._last_total >= 0 else total
        self._last_total = total
        if self._miss_pending:
            if total > self._miss_total:
                # 钩子只是慢了一拍，还是收到了 —— 不算漏键，顺便证明它还活着
                self._miss_pending = False
                self._hook_misses = 0
                self._hook_ok_since_reinstall = True
                self._hook_reconciles += 1
                return
            if now - self._miss_at < HOOK_MISS_CONFIRM_MS / 1000.0:
                return  # 再给钩子 250 毫秒，回调偶尔会慢半拍
            self._miss_pending = False
            self._hook_misses += 1
            print(f"[app] 系统里有按键按下，键盘钩子却没收到（第 {self._hook_misses} 次）",
                  file=sys.stderr, flush=True)
            if self._hook_misses < HOOK_MISSES_TO_REINSTALL:
                return
            self._hook_misses = 0
            if self._hook_giveup:
                return
            self._reinstall_hook()
            return
        if hook.sample_keydowns():
            self._miss_pending = True
            self._miss_at = now
            self._miss_total = prev_total

    def _reinstall_hook(self) -> None:
        """重装键盘钩子。钩子被系统摘掉后，这是唯一不需要用户重启程序的办法。"""
        if self._hook_reinstalls >= 1 and not self._hook_ok_since_reinstall:
            # 上一次重装之后一次账都没对上 —— 问题不在钩子身上（多半是别的全局钩子
            # 把按键吃掉了），再装下去只是白白地断键盘。
            self._hook_giveup = True
            print("[app] 重装之后仍然漏键，停止自动重装（问题不在钩子本身）",
                  file=sys.stderr, flush=True)
            self._add_warning(
                "键盘钩子重装之后仍然收不到按键，已经停止自动重装\n"
                "多半是别的程序（改键器 / 宏软件 / 安全软件）把按键吃掉了\n"
                "如果打字确实没反应，请重启一下程序"
            )
            return
        self._hook_reinstalls += 1
        print(f"[app] 正在重装键盘钩子（第 {self._hook_reinstalls} 次）", flush=True)
        old = self.hook
        if old is not None:
            try:
                old.stop()
            except Exception as exc:  # 卸不掉也要继续装新的
                print(f"[app] 卸下旧钩子失败：{exc!r}", file=sys.stderr, flush=True)
        try:
            self.hook = W.KeyboardHook(self.core.handle_key, "t-ime")
            ok = self.hook.start()
        except Exception as exc:
            self.hook = None
            self._add_warning(f"键盘钩子重装失败：{exc!r}\n打字可能没反应，重启一下程序试试")
            return
        self._last_sample = time.monotonic()
        # 新钩子的计数器从 0 开始，基线必须跟着重来，否则会拿旧钩子的事件数去比
        self._last_total = -1
        self._miss_pending = False
        self._hook_ok_since_reinstall = False
        # 刚才那段时间没有钩子（CapsLock 按下可能漏了）——跟系统对一次表
        self.core.sync_caps()
        if not ok:
            self._add_warning(
                "键盘钩子的自动重装失败了，打字可能没反应\n"
                f"原因：{self.hook.error or '未记录'}\n"
                "重启一下程序试试"
            )
        else:
            print("[app] 键盘钩子已重装", flush=True)
            self._add_warning(
                f"键盘钩子被系统摘掉过，已经自动重装（第 {self._hook_reinstalls} 次）\n"
                "现在应该能正常打字了"
            )

    def _print_banner(self) -> None:
        c = self.cfg
        print()
        print("  translate-ime 已启动")
        print(f"  翻译引擎   : {self.translator.describe()}")
        print(f"  拼音候选   : 最多 {c['pinyin']['max_candidates']} 个")
        _row = "中文" if self.core.active == 0 else "译文"
        if self.core.active != 0 and not self.translator.enabled:
            # 译文行永远拿不到译文（翻译关着），别让这一行看起来像已经生效了。
            _row += "（翻译关着 —— 实际上屏会退回中文）"
        print(f"  默认上屏   : {_row}")
        print(f"  输入状态   : {'已启用' if self.core.enabled else '已挂起'}")
        print("  输入法来源 : 全局键盘钩子 —— 语言栏 / Win+Space 里没有本工具是正常的")
        print("  快捷键     :")
        for spec, name, desc in self._hotkey_help():
            print(f"      {spec:<18} {desc}")
        from .hotkey import direct_switch_hint, shift_tap_off_reason, tap_switch_hint

        if tap_switch_hint(self.cfg):
            print(f"      点一下 Shift       切换「拼音 / 英文直通」"
                  f"（别按住超过 {self._tap_window_phrase()}）")
        elif not direct_switch_hint(self.cfg):
            why = shift_tap_off_reason(self.cfg) or "input.shift_tap_toggles 关着"
            print(f"      （没有切「拼音 / 英文直通」的键：{why}，"
                  "hotkeys.toggle_direct 也没设）")
        print(f"  退出       : {self._quit_spec()}，或在控制台按 Ctrl+C")
        if not self.install_hook:
            print("  （本次没装键盘钩子，只起了界面）")
        print()

    def _hotkey_help(self) -> list[tuple[str, str, str]]:
        """banner 里的「快捷键」表：写**真正注册上的**写法。

        配置里写了但核心拒绝注册的热键（动作名拼错、撞车输掉、没有修饰键的打字键）
        以前照样以配置原文出现在表里，用户看着表按下去却什么也不发生。现在标成
        「（没用上）」，并在 `--check` 里给出具体原因。
        """
        hk = self.cfg["hotkeys"]
        rows = [
            ("compose", "一键直译上屏（跳过选词）"),
            ("toggle_default", "切换默认上屏语言（中文 / 译文）"),
            ("toggle_enabled", "挂起 / 恢复本输入法"),
            ("toggle_direct", "切换拼音 / 英文直通"),
            ("quit", "退出"),
        ]
        from .hotkey import pretty
        out: list[tuple[str, str, str]] = []
        for name, desc in rows:
            raw = hk.get(name) or ""
            if not raw:
                continue
            spec = self.core.registered_hotkeys.get(name)
            # 没用上的那行也用给人看的写法（``shift+q（没用上）`` 读着别扭）。
            out.append((spec or f"{pretty(raw)}（没用上）", name, desc))
        return out

    # ------------------------------------------------------------------ #
    def _poll_foreground(self) -> None:
        """前台窗口换了就把挂起的合成丢掉 —— 否则半截拼音会落进新窗口。"""
        try:
            fg = W.foreground_hwnd()
        except Exception:
            return
        if not fg or fg == self._fg_hwnd:
            return
        # 候选窗自己短暂成为前台不算「用户换了窗口」，别拿它当切换
        if W.window_pid(fg) == os.getpid():
            return
        self._fg_hwnd = fg
        if self.core.on_foreground_changed():
            self.overlay.hide()

    def _translate_info(self) -> dict:
        """翻译层对外要说的那句话：用的哪家、翻了几次、最近一次为什么失败。"""
        tr = getattr(self, "translator", None)
        stats = dict(getattr(tr, "stats", {}) or {})
        err = str(stats.get("last_error") or "")
        try:
            cache_n = len(getattr(tr, "cache", None) or ())
        except Exception:
            cache_n = 0
        return {
            # translate.enabled=false 时引擎一次都不会被调用：不把这件事报出来，
            # 面板只能照着 is_mock 说「离线词表（没配 key）」，等于在解释一件
            # 根本没发生的事（第 78 轮）。
            "enabled": bool(getattr(tr, "enabled", True)),
            "mock": bool(getattr(tr, "is_mock", True)),
            # 为什么离线：没配 key（被动）还是 api.provider=mock（主动）。
            # 少了这一条，面板只能猜「没配 key」，配了 key 的人会觉得程序在说谎。
            "mock_reason": str(getattr(tr, "mock_reason", "") or ""),
            "provider": str(getattr(getattr(tr, "provider", None), "name", "?")),
            "variants": int(getattr(tr, "variants", 0) or 0),
            "requests": int(stats.get("requests") or 0),
            "cache_hits": int(stats.get("cache_hits") or 0),
            # 缓存条数不进 sig（每学一句就变），只写进 payload 供体检参考。
            "cache_size": cache_n,
            "errors": int(stats.get("errors") or 0),
            "last_error": (err[:160] or None),
            # 第 117 轮：光有错误原文不够 —— 「连不上网」和「key 写错了」在
            # 字符串里长得一样，面板照着原文念会把用户送去改配置。
            "last_error_kind": str(stats.get("last_error_kind") or ""),
            "last_error_status": stats.get("last_error_status"),
        }

    def _publish_status(self, *, force: bool = False) -> None:
        """把「我现在是什么状态」写进状态文件，供第二次双击 run.bat 的那个进程读。

        托盘图标可能装不上（低完整性进程会被 UIPI 拦掉），而它本来是唯一能
        退出/挂起/换语言的入口。状态文件 + 命令文件就是那条退路。
        """
        from . import daemon

        snap = {
            "pid": os.getpid(),
            "root": str(self.root_dir),
            "log": str(self._log_path()),
            # 实例用的配置是哪一份（--config 可以指向别的文件）以及它实际的退出键。
            # 面板/体检拿不到这两样时，只能按自己读的配置猜 —— 猜出来的热键按下去
            # 没有任何反应，用户会以为「程序卡住了」（第 60 轮）。
            "config": str(self._config_path) if self._config_path else None,
            "quit_hotkey": self._quit_spec(),
            "enabled": bool(self.core.enabled),
            "direct": bool(self.core.direct),
            # CapsLock 开着的时候字母会原样上屏（大写英文字母），面板上得看得见，
            # 否则用户会以为「输入法又坏了，打字母不出候选」。
            "caps": bool(getattr(self.core, "_caps", False)),
            "active": "en" if self.core.active else "zh",
            # 状态机最近一句「跟用户说的话」。第 105 轮：从控制面板点「换默认上屏
            # 语言」时，如果翻译关着，状态机会拒绝并把原因写进 core.status ——
            # 可那个原因只弹在候选窗的 toast 上（1.6 秒），而面板用户正盯着控制台，
            # 看到的只是「上屏语言 中文」原地不动，于是以为菜单坏了。
            # 状态文件里带上它，面板就能把「为什么没变」说出来。
            "status": (str(self.core.status or "") or None),
            "tray": bool(getattr(self.tray, "tray_added", False)),
            # 托盘图标是「你自己关的」还是「想装但没装上」必须分得清：以前只有 tray
            # 一个字段，面板/体检就只能猜，于是 output.tray=false 的机器上照样被写成
            # 「没装上（UIPI 拦的）」，用户会去查一个根本不该试的动作（第 82 轮）。
            "tray_enabled": bool((self.cfg.get("output") or {}).get("tray", True)),
            # 「钩子装上了」必须问「装成功了没有」，不能只问「对象在不在」：start() 失败时
            # KeyboardHook 对象照样存在（error 里有原因），旧写法
            # ``self.hook is not None`` 于是把「装失败」报成「装上了」—— 启动器据此
            # 打印「已就绪：钩子装上了，现在可以打字了」，而实例自己的日志写着
            # 「键盘钩子安装失败」，用户照着打字却一个字母都不出来（第 107 轮）。
            # --no-hook 也是 False（那是用户关的，靠 hook_info.disabled_by_flag 区分）。
            "hook": bool(self.install_hook and self.hook is not None
                         and not self.hook.error),
            "hook_reinstalls": self._hook_reinstalls,
            "hook_misses": self._hook_misses,
            "hook_reconciles": self._hook_reconciles,
            "hook_giveup": self._hook_giveup,
            # 上屏失败是用户最直接的「打字没反应」：commit_failures > 0 时面板和
            # --doctor 必须能说出上一次是为什么（多半是往管理员窗口里打字）。
            "commit_failures": self._commit_failures,
            "last_commit_error": self._last_commit_error or None,
            # 自学习：「改选的词被记住了吗」以前只能打开 user_boosts.json 才知道。
            # boosts = 记住多少个词，boost_saves = 落盘过几次（>0 就说明真的写进去了）。
            "boosts": len(self.core.engine.user_boosts()),
            "boost_saves": self._boost_saves,
            # 落盘失败的原因：只报「已记住 N 个词」而不报「写不进去」时，用户会以为
            # 自己改选的词都被记住了，其实一条也没落盘（第 81 轮）。
            "boost_file": str(self.core.boost_path),
            "boost_error": self.core.boost_error or None,
            # 权重文件读不懂时（坏 JSON / 不是字典）也单独报一声：那份文件会被改名
            # 留证、自学习从零开始，用户该知道「我上次记的词为什么不在了」（第 87 轮）。
            "boost_load_error": self.core.boost_load_error or None,
            # 翻译引擎现在是哪个、最近一次为什么失败：配了 key 却一直不出译文时，
            # 面板/体检里应该能直接看到「走的是 DeepSeek 还是离线词表」「失败了 3 次，
            # 最近一次 HTTP 401」—— 而不是只能去追悬浮窗上那行一闪就没的字。
            "translate": self._translate_info(),
            "started": self._started_at,
            # Web 控制板点「保存」之后，凭什么说「同步成功了」？就凭这两个数：
            # config_reloads 涨了，说明实例真的重读了配置；config_reload_error
            # 非空，说明重读失败（文件写坏了 / 权限不够）—— 只写文件不重载的话，
            # 面板上改完的快捷键要等下次重启才生效，用户会以为面板坏了（第 108 轮）。
            "config_reloads": self._config_reloads,
            "config_reload_error": self._last_reload_error or None,
            "config_reload_notes": list(self._reload_notes),
        }
        # 「候选窗到底出现过没有」必须能从外面读到：用户报的「候选窗根本不出现」
        # 在出口处就是这组字段 —— shows 恒为 0 = 一次都没显示过；shows > 0 但
        # on_monitor 为假 = 显示到屏幕外了；error 非空 = 显示本身失败。
        ov = self.overlay
        ov_info = {
            "shows": int(getattr(ov, "shows", 0)),
            # 「候选窗弹过几次」和「窗口显示过几次」不是一回事：启动引导、挂起提示
            # 都算 show，却完全不能说明候选窗能用（第 86 轮）。
            "cand_shows": int(getattr(ov, "cand_shows", 0)),
            "visible": bool(getattr(ov, "visible", False)),
            # 只有 visible 一个布尔值时，外面分不清「候选窗真开着」和「屏幕上只有
            # 一条提示」—— 切窗口后候选收起了、托盘失败的提示条却正好接着显示，
            # 只看 visible 就会被误判成「候选窗没清理」（第 57 轮实机复验踩到）。
            "mode": str(getattr(ov, "mode", "hidden")),
            "on_monitor": bool(getattr(ov, "on_monitor", False)),
            "rect": list(ov.last_rect) if getattr(ov, "last_rect", None) else None,
            "error": getattr(ov, "show_error", None),
        }
        # 「你敲的键到底有没有进到钩子里」—— 用户报「打字没反应」时唯一能分清
        # 责任的事实：real == 0 说明按键根本没进来（钩子被系统悄悄摘掉 / 被安全
        # 软件拦 / 焦点在提权窗口）；real > 0 说明键进来了，问题在下游。
        # ``probes`` 那一组只在 `--hook-check` 里会有值：常驻进程没有前台窗口，
        # 注入式自检在那里根本送不进系统，所以常驻探活看的是 ``misses`` 那一组。
        hk_info: dict = {"installed": False, "alive": False, "error": None,
                         "total": 0, "real": 0, "injected": 0, "self_injected": 0,
                         "foreign_injected": 0, "effective": 0, "swallowed": 0,
                         "probes": 0, "probe_seen": 0, "probe_failures": 0,
                         "probe_send_failures": 0, "probe_error": None,
                         "last_real_age": None, "last_effective_age": None}
        if self.hook is not None:
            try:
                hk_info = self.hook.stats()
            except Exception as exc:  # 统计拿不到不能让状态发布崩掉
                hk_info = dict(hk_info, error=f"hook.stats() 失败：{exc!r}")
        # 让外面分得清两种「hook 不是装上了」：用户自己用 --no-hook 关的（正常，
        # 别骂他），还是想装却装失败了（故障，得给出下一步）。只看 `hook` 一个
        # 布尔值只能得出「没装」，面板/体检于是只剩「敲几个拼音字母」这一句
        # 根本不可能生效的建议（第 107 轮）。
        hk_info["disabled_by_flag"] = not self.install_hook
        # 变更检测要拿**稳定字段**比：`updated` 每次都不同，混进去就等于每次都写盘。
        # 候选窗的 rect 只写进 payload 不参与比较 —— 打字时候选框宽度一直在变，
        # 拿它当变更信号等于每敲一个字写一次盘。`last_real_age` 同理（它一直在走）。
        # foreign_injected 必须进 sig：装了改键器/宏的机器上 real 永远是 0，
        # 光比 real 的话状态文件永远不刷新，面板会一直显示「还没收到过真实按键」。
        sig = dict(snap, overlay_shows=ov_info["shows"],
                   overlay_cand_shows=ov_info["cand_shows"],
                   overlay_visible=ov_info["visible"],
                   overlay_mode=ov_info["mode"], tick_errors=self._tick_errors,
                   status=str(self.core.status or ""),
                   hook_real=hk_info["real"], hook_foreign=hk_info.get("foreign_injected", 0),
                   hook_swallowed=hk_info["swallowed"],
                   hook_probe_failures=hk_info.get("probe_failures", 0),
                   hook_probe_send_failures=hk_info.get("probe_send_failures", 0),
                   hook_reinstalls=self._hook_reinstalls,
                   caps=bool(getattr(self.core, "_caps", False)),
                   # 学到新词也是一次「状态变了」：不然面板要等到下一次心跳周期
                   # 才肯刷新，用户刚改选完看不到变化。
                   boosts=len(self.core.engine.user_boosts()),
                   # 落盘失败/恢复都要让面板立刻刷新：用户改好配置就想看见它变绿。
                   boost_error=self.core.boost_error,
                   boost_load_error=self.core.boost_load_error,
                   # 翻译失败一次要能让面板立刻刷新（用户最可能就是在那一刻去看它）。
                   translate_errors=int((snap.get("translate") or {}).get("errors") or 0),
                   translate_requests=int((snap.get("translate") or {}).get("requests") or 0),
                   commit_failures=self._commit_failures)
        if not force and sig == self._status_sig:
            # 状态没变也至少每 STATUS_HEARTBEAT_SECONDS 秒写一次盘：状态文件的
            # mtime 就是外面判断「这个实例还活着吗」的依据（--doctor、第二次双击
            # 的面板）。只在状态变化时写，用户十来秒不打字就会被报成卡死。
            if time.monotonic() - self._last_write < daemon.STATUS_HEARTBEAT_SECONDS:
                return
        snap = dict(snap, overlay=ov_info, hook_info=hk_info, tick_errors=self._tick_errors,
                    tick_error=self._last_tick_error, updated=time.time())
        now = time.monotonic()
        # 写成功才算「发布过了」：写失败还把 sig 记成已发布的话，状态文件会一直
        # 停在一份过期的内容上，而外面（面板/体检）只能看到旧数据却不知道。
        if daemon.write_status(self.root_dir, snap):
            self._status_sig = sig
            self._status_write_warned = False
        else:
            # 失败别每拍都重试（目录没权限时那是每秒一次的无用 IO），下一个
            # 心跳再试；但这件事必须说出来 —— 状态文件写不进去等于控制面板和
            # --doctor 都看不到这个实例，用户会以为它没在跑。
            if not self._status_write_warned:
                self._status_write_warned = True
                print(f"[app] 状态文件写不进去：{daemon.status_path(self.root_dir)}"
                      "（目录没写权限？）", file=sys.stderr)
                self._add_warning(
                    "状态文件写不进去，控制面板和 --doctor 会看不到这个实例\n"
                    "（多半是目录没有写权限，或者被安全软件挡住了）\n"
                    "打字本身不受影响；想彻底解决请把程序换个目录再试")
        self._last_write = now

    def _drain_commands(self) -> None:
        """看第二个进程有没有往收件箱里投命令。"""
        from . import daemon

        for cmd in daemon.take_commands(self.root_dir):
            print(f"[app] 收到命令：{cmd}")
            self._handle_tray_action(cmd)

    def reload_config(self, *, source: str = "") -> bool:
        """重新读一遍实例正在用的那份配置，并把能热更新的部分应用上。

        触发者是 Web 控制板（保存后投 ``reload`` 命令）或托盘菜单。**不重启**：
        用户多半正在打字，重启会丢缓冲、闪一下候选窗，还可能被安全软件再拦一次。
        改完立刻生效的是：hotkeys.*（重新注册）、api.*（换引擎 / key / 模型）、
        translate.enabled / variants / debounce、input.*（单击 Shift、提示时长）、
        output.boost_* / default_language、pinyin.max_candidates / beam_width、
        托盘开关。**要重启**的是：拼音索引相关（min_freq / max_word_len /
        dict_cache）、窗口外观、日志文件位置 —— 这些要重建索引 / 窗口 / 文件句柄。
        """
        from . import config as _config

        path = self._config_path or (self.root_dir / "config.json")
        try:
            _config.LAST_LOAD_ERROR = ""
            cfg, _loaded = _config.load(path)
        except Exception as exc:
            self._last_reload_error = f"读 {path.name} 失败：{exc}"
            print(f"[app] 配置重载失败：{self._last_reload_error}", file=sys.stderr)
            self._publish_status(force=True)
            return False
        if _config.LAST_LOAD_ERROR:
            # config.load() 对读不懂的文件是「警告 + 改用内置默认值」，直接照它
            # 的结果换上去，等于用户手一抖少个逗号，实例的热键/开关就全部被重置
            # 成默认值，而面板还显示「已同步」（第 108 轮实测踩到）。这里必须
            # 把「读不懂」当成失败：老配置继续用，只把原因写进状态。
            self._last_reload_error = f"读 {path.name} 失败：{_config.LAST_LOAD_ERROR}"
            print(f"[app] 配置重载失败（旧配置继续生效）：{self._last_reload_error}", file=sys.stderr)
            self._publish_status(force=True)
            return False

        notes: list[str] = []
        old_tray = bool((self.cfg.get("output") or {}).get("tray", True))
        self.cfg = cfg
        self.core.apply_config(cfg)
        # 热键热更新之后，候选窗底部那格「换语言」也得跟着换 —— 否则面板里
        # 明明写着 F9，窗口底部还教用户按旧组合键（第 115 轮）。
        self.overlay.set_switch_hint(self.core.registered_hotkeys.get("toggle_default") or "")
        notes += self.translator.reconfigure(cfg)
        self.worker.debounce = float(cfg["translate"]["debounce_ms"]) / 1000.0
        self.engine.beam_width = int(cfg["pinyin"]["beam_width"])
        self.engine.max_candidates = int(cfg["pinyin"]["max_candidates"])
        self.engine._cache.clear()

        # 配置刚换过一版，上一版留下的那句「提示」可能早就不成立了 —— 最典型的是
        # 「翻译关着（translate.enabled=false）—— 切不到译文」，而用户这一版刚把
        # 翻译打开。状态文件是面板和网页控制板唯一的眼睛，留着它就是在撒谎（第 111 轮）。
        self.core.status = ""

        new_tray = bool((cfg.get("output") or {}).get("tray", True))
        if new_tray and not old_tray and self.tray is None:
            self._start_tray()
            notes.append("托盘已打开")
        elif old_tray and not new_tray and self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
            notes.append("托盘已关闭")

        self._last_reload_error = ""
        self._reload_notes = notes
        self._config_reloads += 1
        print(f"[app] 配置已重载（来源：{source or '命令'}）："
              + ("；".join(notes) if notes else "没有需要说明的变化"))
        self._publish_status(force=True)
        return True

    def _tick(self) -> None:
        """Tk 定时回调。**任何异常都必须在这里被吃掉。**

        这是本文件最重要的一行防线：Tk 的 ``after`` 回调一旦抛异常，Tk 只往
        stderr 打一行 traceback，**不会**再排下一次 —— 而重新排程的
        ``self.overlay.after(TICK_MS, self._tick)`` 正好在函数末尾。于是主循环
        静默死亡：钩子还装着、键还被吞掉、候选窗永远不再出现、上屏永远不会发生，
        用户看到的就是「打字没反应」，日志里只有一行不起眼的 traceback。
        （候选窗定位、翻译结果合并、Tk 重绘都可能抛，尤其在别的机器上。）
        """
        try:
            self._tick_once()
        except Exception:
            self._tick_errors += 1
            self._last_tick_error = traceback.format_exc()
            print(f"[app] 主循环异常（第 {self._tick_errors} 次，已继续运行）：\n"
                  f"{self._last_tick_error}", file=sys.stderr, flush=True)
            if self._tick_errors == 1:
                self._add_warning("主循环报错了，正在带伤继续跑\n详情见日志")
        finally:
            # 重排程放在 finally 里：无论上面发生什么，下一次心跳一定到位。
            # 间隔是自适应的：忙的时候 16ms（打字要跟手），空转的时候 50ms
            # （见 IDLE_TICK_MS 那段注释 —— 这一行是「常驻时白烧 3.8% 一个核」
            # 的修法，别改回固定值）。
            if self._running and not self.core.quit_requested:
                self._last_tick_ms = TICK_MS if self._busy() else IDLE_TICK_MS
                self.overlay.after(self._last_tick_ms, self._tick)

    def _busy(self) -> bool:
        """现在这一拍该不该用 16ms？判断要便宜（它每拍都会被调一次）。

        忙 = 缓冲里有字、正在等翻译、队列里已经有待处理的按键、或者两秒内
        还收到过按键（用户还在打字，别在这时候降频）。

        **别把「候选窗开着」算进来**：启动引导的短提示会一直挂在屏幕上，把
        ``overlay.visible`` 当忙，等于永远忙 —— 第一版就这么写的，进程内实测
        12 秒里 601 拍全是 16ms，等于没改。候选窗的**内容**变化是被事件驱动的，
        空转档 50ms 足够把新状态画出来（真要跟手的是「按键 → 有反应」这一段）。
        """
        if self.core.buffer:
            return True
        if getattr(self.core, "tr_state", "") == "pending":
            return True
        if not self.events.empty():          # 这一拍就有键要处理，别拖
            return True
        return (time.monotonic() - self._last_activity) < IDLE_AFTER_S

    def _tick_once(self) -> None:
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass

        self._poll_foreground()
        now = time.monotonic()
        if now - self._last_resync >= RESYNC_MS / 1000.0:
            self._last_resync = now
            self.core.resync_modifiers()

        # 钩子体检：被系统悄悄摘掉的话，这里会把它装回去（用户不用重启程序）
        if self.hook is not None and now - self._last_sample >= HOOK_SAMPLE_MS / 1000.0:
            self._last_sample = now
            self._check_hook_health(now)

        try:
            while True:
                self._handle_tray_action(self.tray_actions.get_nowait())
        except queue.Empty:
            pass
        self._sync_tray()

        if now - self._last_publish >= STATUS_MS / 1000.0:
            self._last_publish = now
            self._publish_status()
        # 收件箱降频轮询：一次 stat + open 要 60µs，每拍都做就是 0.4% 一个核。
        # 直接调 _drain_commands() 的调用方（测试、命令入口）不受影响。
        if now - self._last_cmd_poll >= CMD_POLL_MS / 1000.0:
            self._last_cmd_poll = now
            self._drain_commands()

        # 自学习权重定期落盘。以前**只有** shutdown() 写一次，于是「学到的东西」全靠
        # 一次干净退出：被 taskkill /F、崩了、注销、断电，这一场学到的全丢 —— 而常驻
        # 用户根本不会主动退出（这正是「能后台常驻」的代价）。core 内部有
        # BOOST_SAVE_INTERVAL 节流、没脏就立刻返回，所以放在心跳里是免费的。
        if self.core.save_boosts():
            self._boost_saves += 1
            if self.verbose:
                print(f"[app] 自学习权重已写入 {self.core.boost_path.name}"
                      f"（{len(self.core.engine.user_boosts())} 条）")

        # 引导播完之后才出现的坏消息（钩子被系统摘掉、自动重装、放弃重装），
        # 在这里补说 —— 否则它们只进得了日志，用户屏幕上永远看不到。
        # 放在 render **之前**：show_toast 只是把文本交给 core，真正画出来是下面
        # 那次 render 干的活，放在后面就要白等一拍（16 毫秒，用户看不出来，
        # 但「提示要等下一帧」是没必要的）。
        if self._warnings:
            self._flush_warnings()

        changed = self.core.apply_translation_results()
        if changed or self._running:
            self.overlay.render(self.core.snapshot())

        if self.core.quit_requested:
            self.shutdown()
            return
        # 注意：重排程不在这里做，统一由 _tick 的 finally 负责（那里才挡得住异常）

    def _report_commit_failure(self, text: str) -> None:
        """上屏失败要看得见：弹一条提示，并把这次打的字放进剪贴板兜底。

        以前这里是**彻底的静默** —— 候选窗一关、缓冲一清，用户打出来的字凭空消失，
        屏幕上没有任何解释（而且常驻时连控制台都没有，stderr 也没人看得见）。
        这属于「打字没反应」里最冤的一种：程序早就知道了，只是没说。
        """
        self._commit_failures += 1
        reason = self._commit_reason or "SendInput 没能把文字送出去"
        self._last_commit_error = reason
        print(f"[app] 上屏失败：{reason}", file=sys.stderr)
        shown = text if len(text) <= 16 else text[:16] + "…"
        copied = self.overlay.set_clipboard(text) if text else False
        if copied:
            tip = (f"文字没能打进那个窗口：{reason}\n"
                   f"已经把「{shown}」放进剪贴板 —— 直接按 Ctrl+V 就行")
        else:
            tip = (f"文字没能打进那个窗口：{reason}\n"
                   f"（剪贴板也没写进去，你刚才打的是「{shown}」）")
        if not self._inject_warned:
            self._inject_warned = True
            tip += "\n想让它直接打进去：用「以管理员身份运行」启动本程序，或者换个窗口打字"
        # 说出口的渠道只有一个：引导还没播完就先排队（由引导的尾巴带出来），
        # 播完了就直接弹 —— 两条都走会重复，而这里每一句都是要紧话。
        if self._welcome_done:
            self.core.show_toast(tip, seconds=8.0)
        else:
            self._add_warning(tip)

    def _handle_event(self, ev: dict) -> None:
        # 任何事件（按键、上屏、状态变化）都算「有动静」，空转降频要看见它 ——
        # 否则用户停手 2 秒后开始打字，第一拍还在 50ms 的慢速档上，候选窗晚 50ms。
        self._last_activity = time.monotonic()
        kind = ev.get("type")
        if kind == EV_COMMIT:
            self.overlay.hide()
            if ev.get("note"):
                print(f"[app] 注意：{ev['note']}")
            if ev.get("ok"):
                if self.verbose:
                    print(f"[app] 已上屏：{ev.get('text')}")
            else:
                self._report_commit_failure(str(ev.get("text") or ""))
        elif kind == EV_STATE:
            pass  # 状态变化由 render 自己按快照 diff
        elif kind == EV_QUIT:
            print("[app] 收到退出热键")

    # ------------------------------------------------------------------ #
    def shutdown(self) -> None:
        if not self._running:
            return
        self._running = False
        print("\n[app] 正在退出…")
        if self.hook is not None:
            self.hook.stop()
        if self.tray is not None:
            self.tray.stop()
        self.core.shutdown()
        self.overlay.destroy()
        # 把状态文件撤掉：留着它，下一次双击 run.bat 会以为还有个实例在跑
        try:
            from . import daemon

            daemon.clear_status(self.root_dir)
        except Exception:
            pass


def demo_snapshot() -> dict:
    """给 ``--demo`` 用的假状态，方便在没装钩子的机器上检查界面长什么样。"""
    return {
        "enabled": True,
        "direct": False,
        "toast": "",
        "buffer": "jintiantianqizenmeyang",
        "zh": ["今天天气怎么样", "今天田七怎么样", "今天天其怎么样", "今天田契怎么样"],
        "en": ["How is the weather today", "What's the weather like today",
               "How's the weather today"],
        "active": 0,
        "sel": [0, 0],
        "tr_state": "done",
        "tr_text": "今天天气怎么样",
        "status": "",
        "mock": True,
        "hotkeys": {},
    }


def run_demo(cfg: dict, root_dir: Path, seconds: float = 30.0) -> int:
    """只显示候选窗，不装钩子 —— 用来肉眼验收界面与配色。"""
    ov = Overlay(cfg, verbose=True)
    st = demo_snapshot()
    ov.root.after(200, lambda: ov.render(st))
    ov.root.after(int(seconds * 1000), ov.destroy)
    ov.run()
    return 0
