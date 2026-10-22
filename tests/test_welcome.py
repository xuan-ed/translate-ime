# -*- coding: utf-8 -*-
"""启动引导：后台常驻时没有控制台，候选窗上这七行就是用户唯一能看到的「怎么用」。

为什么单独一个文件：``README.md`` 写着「起来就告诉你怎么用 —— 启动后候选窗会显示一份
6 行引导，7 秒后缩成一句短提示」，但此前没有任何测试碰过 ``show_help()`` 的内容 ——
``tests/`` 里只有 ``_welcome_done`` 这个开关被别的用例读来读去。
而这个程序最要命的一条用户反馈恰好是「双击了没反应」：引导是「有没有反应」的唯一凭据。
所以这里逐条钉住：

1. 引导真的是七行（标题 + 一句「语言栏里没有它是正常的」+ 五步），第 3 步的秒数跟着 ``input.shift_tap_ms`` 走；
2. 引导里出现的四个热键**全部**来自 ``config.json``（第 22 轮踩过「提示里写死热键」的坑，
   那次的源码扫描只扫了 app.py/main.py/daemon.py/core.py 里的 ``Ctrl+Alt+Q`` / ``Ctrl+Alt+Z``，
   没管这几个键名会被拼出来）；
3. 7 秒之后真的会去缩成一句短提示（``_welcome`` 的 ``after(7000+300)``），
   以及启动时那 0.6 秒的延迟（等窗口把第一帧画完）；
4. 短提示那句话说得对：已启用 / 已挂起、点 Shift 切英文、配置里的退出键；用户正在打字时
   不许拿它打断；有坏消息时先让位给坏消息；
5. 用户把某个热键设成空字符串（``_parse_hotkeys`` 会跳过空 spec，这是**合法**的关掉方式）
   时，引导不能留个空槽让用户猜。

直接跑：``python tests/test_welcome.py``
"""
import contextlib
import copy
import io
import shutil
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime import daemon  # noqa: E402
from t_ime.app import TICK_MS, WELCOME_SECONDS, WELCOME_TIP_SECONDS, App  # noqa: E402
from t_ime.config import load as load_config  # noqa: E402
from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

OK = "[OK]  "
BAD = "[FAIL]"
results: list[tuple[bool, str]] = []


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"  — {detail}" if detail else ""))


print("=" * 74)
print("  启动引导：候选窗上那七行与后面那句短提示")
print("=" * 74)

cfg, cfg_path = load_config()
print(f"配置来自 {cfg_path}")

# 这个测试会碰状态文件 / 命令收件箱，别把用户正在用的那个实例的痕迹弄丢
backup: dict[Path, bytes] = {}
for p in (daemon.status_path(PROJ), daemon.ctl_path(PROJ)):
    if p.exists():
        backup[p] = p.read_bytes()

# 换一套**完全不同的**热键 + 不同的 Shift 时间窗：引导里出现的每一个都必须跟着变。
cfg_custom = copy.deepcopy(cfg)
cfg_custom["hotkeys"].update({
    "compose": "ctrl+shift+f10",
    "toggle_default": "ctrl+shift+f9",
    "toggle_enabled": "ctrl+shift+f8",
    "quit": "ctrl+shift+f7",
})
cfg_custom.setdefault("input", {})["shift_tap_ms"] = 1200
DEFAULTS = ("Ctrl+Alt+Space", "Ctrl+Alt+L", "Ctrl+Alt+Z", "Ctrl+Alt+Q")

try:
    # ------------------------------------------------------------------ #
    # 1. 引导的七行
    # ------------------------------------------------------------------ #
    print("\n-- 1. 引导的七行：内容、编号、秒数 --")
    app = App(cfg_custom, PROJ, verbose=False, install_hook=False)
    toasts: list[tuple[str, float]] = []
    app.core.show_toast = lambda text, seconds=3.0: toasts.append((text, seconds))

    secs = app.show_help()
    check(secs == WELCOME_SECONDS, "show_help() 默认停留 WELCOME_SECONDS（README 说 7 秒）",
          f"{secs}")
    check(WELCOME_SECONDS == 7.0, "那个常量就是 7 秒（文案与代码没分家）", f"{WELCOME_SECONDS}")
    check(len(toasts) == 1, "引导是通过候选窗的 toast 显示出来的", f"toasts={len(toasts)}")
    text = toasts[0][0] if toasts else ""
    lines = text.splitlines()
    check(len(lines) == 7, "引导正好七行（标题 + 一句「语言栏」说明 + 五步）", f"{len(lines)} 行")
    check(lines and "translate-ime" in lines[0] and "已就绪" in lines[0],
          "第一行是「translate-ime 已就绪」", lines[:1])
    check(any("语言栏" in ln for ln in lines),
          "有一行直说「语言栏里没有它是正常的」（否则用户会以为没装好）",
          [ln for ln in lines if "语言栏" in ln][:1])
    check(all(not ln.startswith("0.") for ln in lines), "那句说明不占编号，五步仍然是 1–5")
    check(text.splitlines()[2].startswith("1. "),
          "说明行插在标题之后、第 1 步之前（用户先看「不用选」，再看怎么用）",
          text.splitlines()[1:3])
    for i in range(1, 6):
        check(any(ln.startswith(f"{i}. ") for ln in lines),
              f"第 {i} 步有自己的编号（用户能照着数）",
              [ln for ln in lines if ln.startswith(f"{i}. ")][:1])
    check("拼音" in text and "空格上屏" in text, "第 1 步说清楚「打拼音、空格上屏」", lines[1:2])
    check("1.2 秒" in text, "第 3 步的 Shift 时间窗跟着 input.shift_tap_ms 走（1200ms → 1.2 秒）",
          [ln for ln in lines if "Shift" in ln][:1])
    check("上屏译文" in text and "上屏中文" in text, "第 2 步说清楚切的是「上屏中文 / 上屏译文」")

    # ------------------------------------------------------------------ #
    # 2. 四个热键全部来自配置
    # ------------------------------------------------------------------ #
    print("\n-- 2. 引导里的热键：全部来自 config.json --")
    check("Ctrl+Shift+F9" in text, "第 2 步用的是配置里的 toggle_default",
          [ln for ln in lines if "Ctrl+Shift+F9" in ln][:1])
    check("Ctrl+Shift+F10" in text, "第 4 步用的是配置里的 compose",
          [ln for ln in lines if "Ctrl+Shift+F10" in ln][:1])
    check("Ctrl+Shift+F8" in text, "第 5 步用的是配置里的 toggle_enabled",
          [ln for ln in lines if "Ctrl+Shift+F8" in ln][:1])
    check("Ctrl+Shift+F7" in text, "第 5 步用的是配置里的 quit",
          [ln for ln in lines if "Ctrl+Shift+F7" in ln][:1])
    for d in DEFAULTS:
        check(d not in text, f"引导里没有写死的默认键 {d}")

    # ------------------------------------------------------------------ #
    # 3. 7 秒之后缩成一句短提示（时序接线）
    # ------------------------------------------------------------------ #
    print("\n-- 3. 引导 → 短提示 的时序接线 --")
    after_calls: list[tuple[int, object]] = []
    app.overlay.after = lambda ms, fn, *a: after_calls.append((ms, fn))  # type: ignore[assignment]
    app._welcome()
    check(len(toasts) >= 2, "_welcome() 先把完整引导弹出来", f"toasts={len(toasts)}")
    shot = [c for c in after_calls if c[1] == app._welcome_tip]
    check(bool(shot), "7 秒后要去做的那件事就是 _welcome_tip（接到了同一个方法上）",
          [ms for ms, _ in shot][:1])
    want_ms = int(WELCOME_SECONDS * 1000) + 300
    check(bool(shot) and shot[0][0] == want_ms,
          f"延迟是 7 秒 + 300ms 余量（{want_ms}ms，避开计时器抖动）",
          f"{[ms for ms, _ in shot][:1]} vs {want_ms}")

    app2 = App(cfg_custom, PROJ, verbose=False, install_hook=False)
    app2.core.show_toast = lambda text, seconds=3.0: toasts.append((text, seconds))
    started: list[tuple[int, object]] = []
    app2.overlay.after = lambda ms, fn, *a: started.append((ms, fn))  # type: ignore[assignment]
    app2.overlay.run = lambda *a, **kw: None  # type: ignore[assignment]
    app2.start()
    check(any(ms == 600 and fn == app2._welcome for ms, fn in started),
          "启动时先等 600ms 再弹引导（等窗口把第一帧画完，免得位置算到一半）",
          [(ms, getattr(fn, "__name__", fn)) for ms, fn in started][:3])
    check(any(ms == TICK_MS for ms, _ in started), "顺手也把主循环那一拍接上了",
          f"TICK_MS={TICK_MS}")

    # ------------------------------------------------------------------ #
    # 4. 短提示那句话
    # ------------------------------------------------------------------ #
    print("\n-- 4. 短提示：状态、切英文、配置里的退出键 --")
    toasts.clear()
    app._welcome_done = False
    app._welcome_tip()
    check(app._welcome_done is True, "短提示弹完就把「引导播过了」记下来")
    tip = toasts[-1][0] if toasts else ""
    check(len(toasts) == 1, "只弹了一条短提示", f"toasts={len(toasts)}")
    check("已启用" in tip, "短提示说清现在是「已启用」", tip)
    check("Shift" in tip, "短提示教了最快的那个键：点一下 Shift 切英文", tip)
    check("Ctrl+Shift+F7" in tip, "短提示里的退出键也是配置里的那个", tip)
    for d in DEFAULTS:
        check(d not in tip, f"短提示里没有写死的默认键 {d}")
    check(toasts and toasts[-1][1] == WELCOME_TIP_SECONDS,
          "短提示停了 WELCOME_TIP_SECONDS 秒（比引导短，别挡打字）", f"{toasts[-1][1] if toasts else None}")

    toasts.clear()
    app.core.enabled = False
    app._welcome_tip()
    check(toasts and "已挂起" in toasts[-1][0], "挂起状态时短提示说「已挂起」",
          toasts[-1][0] if toasts else "")

    # 用户已经在打字了：别拿提示打断
    toasts.clear()
    app.core.enabled = True
    for vk in (0x4E, 0x49):  # n, i
        app.core.handle_key(vk, True, False)
        app.core.handle_key(vk, False, False)
    check(app.core.buffer != "", "先造出「用户正在打字」的状态", app.core.buffer)
    app._welcome_tip()
    check(app._welcome_done is True, "这种情况下也要记下「引导播过了」（否则坏消息永远等不到）")
    check(toasts == [], "正在打字时不弹短提示（不能拿提示盖住候选窗）", f"toasts={len(toasts)}")

    # 有坏消息：先让位给坏消息
    toasts.clear()
    app.core.buffer = ""
    app._welcome_done = False
    app._add_warning("键盘钩子没能装上，打拼音不会有反应")
    app._welcome_tip()
    check(len(toasts) == 1 and "键盘钩子没能装上" in toasts[-1][0],
          "有坏消息时短提示让位给坏消息（这条比「怎么用」更要紧）",
          toasts[-1][0] if toasts else "")
    check("点一下 Shift" not in toasts[-1][0] if toasts else False,
          "这时候不再弹那句「怎么用」")

    # ------------------------------------------------------------------ #
    # 5. 空热键：合法配置，但不该在引导里留个空槽
    # ------------------------------------------------------------------ #
    print("\n-- 5. 把热键设成空字符串（合法的关掉方式）--")
    cfg_empty = copy.deepcopy(cfg_custom)
    cfg_empty["hotkeys"]["compose"] = ""
    cfg_empty["hotkeys"]["quit"] = ""
    app3 = App(cfg_empty, PROJ, verbose=False, install_hook=False)
    toasts3: list[tuple[str, float]] = []
    app3.core.show_toast = lambda text, seconds=3.0: toasts3.append((text, seconds))
    app3.show_help()
    text3 = toasts3[-1][0] if toasts3 else ""
    lines3 = text3.splitlines()
    step4 = [ln for ln in lines3 if ln.startswith("4. ")]
    step5 = [ln for ln in lines3 if ln.startswith("5. ")]
    check(len(lines3) == 7, "空热键时引导仍然是七行", f"{len(lines3)} 行")
    check(bool(step4) and step4[0].replace("4. ", "").strip() not in ("", "一键直译上屏"),
          "compose 空着时第 4 步不是「4.  一键直译上屏」那种空槽",
          step4)
    check("没设" in text3 or "关掉" in text3,
          "空热键时明说「没设这个热键」，而不是让用户对着空白猜", step4 + step5)

    toasts3.clear()
    app3._welcome_done = False
    app3._welcome_tip()
    tip3 = toasts3[-1][0] if toasts3 else ""
    check("退出" in tip3 and tip3.strip() != "", "退出键空着时短提示也不留空槽", tip3)
    check("没设" in tip3 or "关掉" in tip3, "短提示同样明说没设退出键", tip3)

    # ------------------------------------------------------------------ #
    # 6. input.shift_tap_toggles=false：别再教用户点一个不存在的功能
    # ------------------------------------------------------------------ #
    print("\n-- 6. Shift 单击被关掉之后：提示不许再说「点一下 Shift」--")
    # 第 72 轮：Shift 单击是 config 里的开关（input.shift_tap_toggles）。
    # 关掉它的人照旧被引导去点 Shift，结果什么都不会发生 —— 提示必须跟着配置走。
    cfg_notap = copy.deepcopy(cfg_custom)
    cfg_notap.setdefault("input", {})["shift_tap_toggles"] = False
    cfg_notap["hotkeys"]["toggle_direct"] = "ctrl+shift+f11"
    app4 = App(cfg_notap, PROJ, verbose=False, install_hook=False)
    toasts4: list[tuple[str, float]] = []
    app4.core.show_toast = lambda text, seconds=3.0: toasts4.append((text, seconds))

    app4._welcome_tip()
    tip4 = toasts4[-1][0] if toasts4 else ""
    check("点一下 Shift" not in tip4,
          "Shift 单击关掉后，短提示不再教「点一下 Shift」", tip4)
    check("Ctrl+Shift+F11" in tip4,
          "短提示改用配置里的 hotkeys.toggle_direct", tip4)

    app4.core.enabled = False
    toasts4.clear()
    app4._welcome_tip()
    tip4b = toasts4[-1][0] if toasts4 else ""
    check("已挂起" in tip4b and "恢复" in tip4b,
          "挂起时短提示教的是「恢复」，不是「切英文」", tip4b)
    check("Ctrl+Shift+F8" in tip4b and "Shift 恢复" not in tip4b.replace("Ctrl+Shift+F8", ""),
          "恢复用的就是 hotkeys.toggle_enabled（挂起时 Shift 根本不响应）", tip4b)

    toasts4.clear()
    app4.show_help()
    text4 = toasts4[-1][0] if toasts4 else ""
    step3 = [ln for ln in text4.splitlines() if ln.startswith("3. ")]
    check(bool(step3) and "点一下 Shift" not in text4,
          "引导第 3 步也不再教「点一下 Shift」", step3)
    check(bool(step3) and "Ctrl+Shift+F11" in step3[0],
          "引导第 3 步改用 hotkeys.toggle_direct", step3)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        app4._print_banner()
    ban = buf.getvalue()
    check("点一下 Shift" not in ban,
          "前台 banner 也不再说「点一下 Shift」", ban.strip()[:120])
    check("Ctrl+Shift+F11" in ban,
          "前台 banner 只列真正能用的那个键", ban.strip()[:160])

    # 第 77 轮：配置里写了、核心却拒绝注册的热键（裸字母没有修饰键），
    # banner 的快捷键表和「退出 :」那行都不许再把它当成能用的键播报。
    cfg_bare = copy.deepcopy(cfg_custom)
    cfg_bare["hotkeys"]["quit"] = "q"
    app6 = App(cfg_bare, PROJ, verbose=False, install_hook=False)
    check(app6._quit_spec() == "（没设热键）",
          "quit 配成裸字母 q 时，界面不再说「按 Q 退出」", app6._quit_spec())
    buf6 = io.StringIO()
    with contextlib.redirect_stdout(buf6):
        app6._print_banner()
    ban6 = buf6.getvalue()
    check("Q（没用上）" in ban6,
          "banner 的快捷键表把没注册上的那个标成「（没用上）」", ban6.strip()[:220])

    # 第 84 轮：shift+q —— Shift 单独出现等于「打大写字母」，同样注册不上；
    # 没用上的那行要用给人看的写法（Shift+Q），而不是配置原文（shift+q）。
    cfg_shift = copy.deepcopy(cfg_custom)
    cfg_shift["hotkeys"]["quit"] = "shift+q"
    app7 = App(cfg_shift, PROJ, verbose=False, install_hook=False)
    check(app7._quit_spec() == "（没设热键）",
          "quit 配成 shift+q 时，界面不再说「按 Shift+Q 退出」", app7._quit_spec())
    buf7 = io.StringIO()
    with contextlib.redirect_stdout(buf7):
        app7._print_banner()
    ban7 = buf7.getvalue()
    check("Shift+Q（没用上）" in ban7,
          "banner 用漂亮写法标出没注册上的 shift+q", ban7.strip()[:220])
    check("shift+q（没用上）" not in ban7,
          "banner 不照抄配置原文", ban7.strip()[:220])

    # 第 90 轮：shift_tap_ms=0（合法值）时 Shift 单击永远不会触发（held_ms <= 0
    # 永不成立），所以 banner / 引导第 3 步都不许再教「点一下 Shift」，而且要说清
    # 是哪个选项把时间窗设成了零 —— 不能糊弄成「toggles 关着」。
    cfg_tap0 = copy.deepcopy(cfg_custom)
    cfg_tap0["input"]["shift_tap_toggles"] = True
    cfg_tap0["input"]["shift_tap_ms"] = 0
    cfg_tap0["hotkeys"]["toggle_direct"] = ""
    app8 = App(cfg_tap0, PROJ, verbose=False, install_hook=False)
    buf8 = io.StringIO()
    with contextlib.redirect_stdout(buf8):
        app8._print_banner()
    ban8 = buf8.getvalue()
    check("点一下 Shift" not in ban8,
          "shift_tap_ms=0 时 banner 不再教「点一下 Shift」", ban8.strip()[:220])
    check("shift_tap_ms" in ban8,
          "banner 说清是 shift_tap_ms 把时间窗设成了零", ban8.strip()[:260])
    check("shift_tap_toggles 关着" not in ban8,
          "banner 不再错怪 shift_tap_toggles", ban8.strip()[:260])
    toasts8: list = []
    app8.core.show_toast = lambda text, seconds=None: toasts8.append(text)  # type: ignore[assignment]
    app8.show_help()
    text8 = toasts8[-1] if toasts8 else ""
    check("点一下 Shift" not in text8,
          "引导第 3 步也不再教「点一下 Shift」", text8.splitlines()[:4])
    check("shift_tap_ms" in text8,
          "引导第 3 步说清为什么没有切换键", text8.splitlines()[:4])

    # 第 91 轮：默认上屏=译文 + 翻译关着，是一对自相矛盾的配置 —— core 启动时就
    # 该说出来，banner 也不能把「译文」写成已经生效的样子（每次空格都会退回中文）。
    cfg_den = copy.deepcopy(cfg_custom)
    cfg_den["output"]["default_language"] = "en"
    cfg_den["translate"]["enabled"] = False
    buf9 = io.StringIO()
    with contextlib.redirect_stdout(buf9):
        app9 = App(cfg_den, PROJ, verbose=False, install_hook=False)
        app9._print_banner()
    ban9 = buf9.getvalue()
    dline = next((ln.strip() for ln in ban9.splitlines() if "默认上屏" in ln), "")
    check("默认上屏=译文但翻译关着：banner 点明会退回中文",
          "译文" in dline and "退回中文" in dline, dline)
    check("启动时就说清这对配置自相矛盾",
          "默认上屏设成了「译文」" in ban9 and "translate.enabled=false" in ban9,
          ban9.strip()[:260])

    # 连 toggle_direct 也没设：这时该直说「没有切换键」，而不是留个假动作
    cfg_none = copy.deepcopy(cfg_notap)
    cfg_none["hotkeys"]["toggle_direct"] = ""
    app5 = App(cfg_none, PROJ, verbose=False, install_hook=False)
    toasts5: list[tuple[str, float]] = []
    app5.core.show_toast = lambda text, seconds=3.0: toasts5.append((text, seconds))
    app5.show_help()
    text5 = toasts5[-1][0] if toasts5 else ""
    step3b = [ln for ln in text5.splitlines() if ln.startswith("3. ")]
    check(bool(step3b) and "没有切换键" in step3b[0],
          "两个办法都没有时，引导第 3 步直说没有切换键", step3b)
    check("点一下 Shift" not in text5, "这时候更不该出现「点一下 Shift」")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        app5._print_banner()
    ban5 = buf.getvalue()
    check("没有切「拼音 / 英文直通」的键" in ban5,
          "前台 banner 也直说没有切换键，而不是空着让用户找")

    # ------------------------------------------------------------
    # 第 79 轮：索引 0 键时，程序看起来一切正常（钩子装上了、引导也弹了），
    # 就是打什么字都没有候选。启动时必须自己把这句话说出来。
    cfg_emptyidx = copy.deepcopy(cfg_custom)
    cfg_emptyidx["pinyin"]["min_freq"] = 100000000
    cfg_emptyidx["pinyin"]["dict_cache"] = ".tmp/welcome-emptyidx/idx.pkl"
    cfg_emptyidx["output"]["tray"] = False
    buf0 = io.StringIO()
    with contextlib.redirect_stdout(buf0):
        app7 = App(cfg_emptyidx, PROJ, verbose=False, install_hook=False)
    warn = buf0.getvalue()
    check("拼音索引是空的" in warn, "索引 0 键时启动就警告「打字不会有候选」", warn.strip()[:120])
    check("min_freq" in warn, "警告里点名 pinyin.min_freq，用户知道改哪个参数")

finally:
    for p, data in backup.items():
        try:
            p.write_bytes(data)
        except OSError:
            pass
    for p in (daemon.status_path(PROJ), daemon.ctl_path(PROJ)):
        if p not in backup:
            try:
                p.unlink()
            except OSError:
                pass
    shutil.rmtree(PROJ / ".tmp" / "welcome-emptyidx", ignore_errors=True)

total = len(results)
passed = sum(1 for ok, _ in results if ok)
print("\n" + "=" * 74)
print(f"结果：{passed}/{total} 通过")
print("=" * 74)
sys.exit(0 if passed == total else 1)
