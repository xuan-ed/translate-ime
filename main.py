# -*- coding: utf-8 -*-
"""translate-ime 命令行入口。

::

    run.bat                        # 启动（会装全局键盘钩子）
    run.bat --check                # 自检：依赖 / 索引 / 翻译 / 热键 / 窗口
    run.bat --demo                 # 只弹候选窗，不装钩子，用来验收界面
    run.bat --build-index          # 只重建拼音索引后退出
    run.bat --no-hook              # 不装钩子（界面照常渲染，便于排错）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

#: 可写根目录（配置 / 日志 / 索引缓存都在这儿，打包后是 exe 旁边）。
ROOT = Path(__file__).resolve().parent if not getattr(sys, "frozen", False) else Path(sys.executable).parent
# 打包后模块在解包目录里，直接 import 就行；源码跑时要把仓库根塞进 sys.path。
_BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))
for _p in (str(ROOT), str(_BUNDLE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _fix_console_encoding() -> None:
    """见 t_ime/console.py —— 真控制台不动，重定向时才切 UTF-8。"""
    from t_ime.console import fix_console_encoding

    fix_console_encoding()


def _ensure_streams() -> None:
    """打包版双击时 ``sys.stdout`` / ``sys.stderr`` 是 ``None``，print 一下就炸。

    PyInstaller 的 windowed 引导（``console=False``）不会给进程分配控制台，
    于是 Python 按「没有标准流」处理：``sys.stdout is None``。而 main.py 里到处
    是 ``print``（「配置来自 …」「已在后台启动 …」），第一个 print 就会
    ``AttributeError: 'NoneType' object has no attribute 'write'`` —— 用户双击
    exe 看到的是「什么都没发生」，连实例都没起来（第 125 轮真机）。

    这里给它们塞一个 ``os.devnull``，行为和从控制台启动时「输出没人看」一致：
    该写的日志照写文件，屏幕上的那些话丢弃（窗口版本来也没地方显示）。
    """
    import os

    for name in ("stdout", "stderr"):
        if getattr(sys, name, "missing") is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:  # pragma: no cover - devnull 打不开就真没辙了
                pass


def _double_click_args(argv: list[str]) -> list[str]:
    """双击打包版 exe（一个参数都没给）时，等价于双击 ``run.bat``。

    ``run.bat`` 里写的是 ``main.py --background --panel``，所以源码模式双击
    run.bat 一直有窗口；但打包版没有 run.bat，用户双击的是 exe 本身，argv 为空
    —— 不补这两个开关就会掉进「前台跑一个没有控制台的实例、也不弹设置窗口」，
    表现就是「双击了，什么都没出现」（第 125 轮）。

    只对 frozen 生效：源码模式 ``python main.py`` 不带参数仍然是前台跑（很多
    文档和测试就是这么用的），不能被这条静默改掉。
    """
    if argv or not getattr(sys, "frozen", False):
        return list(argv)
    return ["--background", "--panel"]


def _hotkey_epilog() -> str:
    """``--help`` 末尾的快捷键表：**从配置读**。

    写死那四个组合键的话，用户改过 ``hotkeys`` 之后这份帮助就成了假话。
    配置读不到就干脆不列组合键（总比列错强）。
    """
    try:
        from t_ime.config import load as load_config
        from t_ime.hotkey import pretty
        cfg, _path = load_config()
        hk = cfg.get("hotkeys") or {}
        order = (("compose", "直译上屏"), ("toggle_default", "换默认上屏语言"),
                 ("toggle_enabled", "挂起 / 恢复"), ("toggle_direct", "拼音 / 英文直通"),
                 ("quit", "退出"))
        parts = [f"{pretty(hk[key])} {label}" for key, label in order if hk.get(key)]
        if parts:
            return "快捷键（按当前 config.json）：" + " / ".join(parts)
    except Exception:
        pass
    return "快捷键：见 config.json 的 hotkeys 段"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="translate-ime",
        description="中英互译输入法（拼音输入 + 实时翻译候选，Tab 切换上屏语言）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_hotkey_epilog(),
    )
    p.add_argument("--config", metavar="PATH", help="指定 config.json（默认找项目根目录下的）")
    p.add_argument("--check", action="store_true", help="跑自检并退出")
    p.add_argument("--demo", action="store_true", help="只显示候选窗示例，不装钩子")
    p.add_argument("--demo-seconds", type=float, default=30.0, help="--demo 显示多少秒后自动关（默认 30）")
    p.add_argument("--build-index", action="store_true", help="只重建拼音索引后退出")
    p.add_argument("--init-config", action="store_true", help="把 config.example.json 复制成 config.json 后退出")
    p.add_argument("--show-config", action="store_true",
                   help="打印每个选项的生效值与来源、并检查有没有拼错的键，然后退出")
    p.add_argument("--no-hook", action="store_true", help="不安装全局键盘钩子")
    p.add_argument("-b", "--background", action="store_true",
                   help="脱离控制台常驻后台（无窗口，日志写文件）")
    p.add_argument("-f", "--foreground", action="store_true",
                   help="强制前台运行（run.bat 默认后台常驻，调试/看日志时用它）")
    p.add_argument("--detached", action="store_true",
                   help=argparse.SUPPRESS)  # 内部用：已经是后台子进程，别再自旋
    p.add_argument("--log", metavar="PATH",
                   help="日志文件位置（默认 项目目录/translate-ime.log）；指到目录时会在里面写 translate-ime.log")
    p.add_argument("--autostart", action="store_true", help="设为开机自启后退出")
    p.add_argument("--no-autostart", action="store_true", help="取消开机自启后退出")
    p.add_argument("--autostart-status", action="store_true", help="查看开机自启状态后退出")
    p.add_argument("--tray-check", action="store_true",
                   help="诊断托盘图标装不上的原因并写出 tray-check.txt 后退出")
    p.add_argument("--hook-check", action="store_true",
                   help="测一下全局键盘钩子能不能看到你敲的键，然后退出")
    p.add_argument("--hook-check-seconds", type=float, default=12.0,
                   help="--hook-check 观察多少秒（默认 12）")
    p.add_argument("--doctor", action="store_true",
                   help="全面体检：环境/托盘/配置/日志/钩子/候选窗/端到端链路，写出 translate-ime-doctor.txt")
    p.add_argument("--doctor-seconds", type=float, default=0.0,
                   help="--doctor 里额外观察真实按键多少秒（默认 0 = 不观察，不用你敲键盘）")
    p.add_argument("--doctor-quick", action="store_true",
                   help="只查静态项：不试装托盘/钩子、不跑端到端（最快）。可单独敲，等于 --doctor --doctor-quick")
    p.add_argument("--web", action="store_true",
                   help="打开本地 Web 控制板：在浏览器里改快捷键 / API key，保存后通知常驻实例热重载")
    p.add_argument("--web-port", type=int, default=8765,
                   help="Web 控制板端口（默认 8765；被占用会自动往上找）")
    p.add_argument("--no-browser", action="store_true",
                   help="--web 时不自动打开浏览器（只打印地址）")
    p.add_argument("--panel", action="store_true",
                   help="打开图形设置窗口：填 API key、改快捷键、看当前状态")
    p.add_argument("--no-panel", action="store_true",
                   help="启动后不自动弹设置窗口（脚本、开机自启、调试用）")
    p.add_argument("-v", "--verbose", action="store_true", help="打印更多过程信息")
    return p


# ---------------------------------------------------------------------- #
def _want_panel(args, cfg: dict) -> bool:
    """启动后要不要自动弹图形设置窗口。

    第 124 轮：托盘在低完整性环境里装不上（``NIM_ADD`` 报 5），而控制台菜单
    填不了 API key —— 新用户双击 run.bat 之后，除了「敲字有没有反应」之外
    拿不到任何入口。所以默认弹一个窗口出来，把配置和状态都摊在他面前。

    **只认 ``--panel`` 这个显式开关**（run.bat 里就写着），不再靠
    ``sys.stdin.isatty()`` 猜：Windows 上把 stdin / stdout 重定向到 NUL 之后
    ``isatty()`` 照样返回 True（第 95 轮踩过的同一个坑），结果是脚本或测试
    一调用就凭空冒出个窗口，而它们等的是控制台菜单那几行字。
    """
    if getattr(args, "no_panel", False):
        return False
    if not getattr(args, "panel", False):
        return False
    if getattr(args, "detached", False):
        # 带 --detached 的 --panel 由前面那个分支就地开窗，不走这里
        return False
    if getattr(args, "foreground", False):
        # --foreground 是「我要盯着控制台看日志」，那时候用户本来就有个窗口
        return False
    ui = cfg.get("ui") if isinstance(cfg, dict) else None
    if isinstance(ui, dict) and ui.get("open_panel_on_start") is False:
        return False
    return True


def _pin_cli_paths(argv: list[str], cfg_path: Path | None,
                   log_path: Path | None) -> list[str]:
    """把 ``--config`` / ``--log`` 的值钉成绝对路径，再交给后台子进程。

    子进程的 cwd 固定是项目根（见 ``daemon.spawn_background``），而用户可能在
    别处敲命令：``cd D:\\work; python D:\\...\\main.py --background --config my.json``。
    父进程按用户的 cwd 读到了 ``D:\\work\\my.json``，子进程却按项目根去找
    ``D:\\...\\translate-ime\\my.json`` —— 找不到就**悄悄**退回默认配置，而父进程
    刚把「配置来自 …」和自定义的退出热键都报了一遍（第 69 轮真机撞到）。

    值一旦转成绝对路径，父子两边读的就是同一份文件。``--config=PATH`` 这种
    等号写法也要处理（``argparse`` 两种都认，自己人别只认一种）。

    ``cfg_path`` 必须是「父进程这次实际加载的那份」；``log_path`` 用
    ``daemon.resolve_log_path()`` 算出来的最终文件路径。对应选项没出现在
    命令行里时原样不动（别硬塞一个用户没要的开关）。
    """
    pinned: dict[str, str] = {}
    if cfg_path is not None:
        pinned["--config"] = str(Path(cfg_path).resolve())
    if log_path is not None:
        pinned["--log"] = str(Path(log_path).resolve())
    out = list(argv)
    for i, tok in enumerate(out):
        for name, want in pinned.items():
            if tok == name and i + 1 < len(out):
                out[i + 1] = want
            elif tok.startswith(name + "="):
                out[i] = f"{name}={want}"
    return out


# ---------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    _ensure_streams()
    _fix_console_encoding()
    # 记下「这一次到底敲了什么」：自启项要照着它把 --config/--log 带过去。
    # 不能直接读 sys.argv —— main(argv) 被测试/驱动调用时，那份 argv 才是真的。
    run_argv = list(sys.argv[1:] if argv is None else argv)
    # 这次**怎么启动**和**要不要记进自启项**是两件事：双击打包版时补出来的
    # --background --panel 只用于本次解析，绝不能写进开机自启项（否则每次开机
    # 都弹一个窗口出来）。
    args = build_parser().parse_args(_double_click_args(run_argv))

    # 修饰参数单独出现时要说清楚 —— 否则它被静默忽略，程序「照常启动」，
    # 用户以为自己刚跑了个检查，其实只是把输入法又拉起了一份。
    _orphans = []
    if args.hook_check_seconds != 12.0 and not args.hook_check:
        _orphans.append(("--hook-check-seconds", "--hook-check"))
    if args.demo_seconds != 30.0 and not args.demo:
        _orphans.append(("--demo-seconds", "--demo"))
    if _orphans:
        for got, want in _orphans:
            print(f"[main] {got} 只有跟 {want} 一起用才有意义；照现在这么敲它会被忽略，"
                  f"程序会照常启动。", file=sys.stderr)
        return 2

    # 注意：这里**不**导入 t_ime.app —— App 一导入就会拉起 pinyin / overlay，
    # 也就是 pypinyin / jieba 这些第三方依赖才第一次被需要。在后台模式下，
    # 那一步必须发生在**日志建好之后**，否则错误没人接得住（见下面启动段）。
    from t_ime.config import init_config, load as load_config
    from t_ime import winapi as W

    # 必须赶在任何 GetSystemMetrics / GetDC / 建窗口之前。一旦有谁先问过一次，
    # Windows 就把本进程的 DPI 感知锁死成 UNAWARE：之后候选窗会被系统整体拉伸
    # （字发虚），而且屏幕坐标与实际像素差一个缩放比。
    W.enable_dpi_awareness()

    if args.init_config:
        # --config 指哪儿就在哪儿生成：以前这里写死 init_config()，
        # 用户 `--config D:\my\ime.json --init-config` 只会得到 root\config.json，
        # 然后实例 `--config D:\my\ime.json` 永远读不到这份（第 61 轮）。
        asked = Path(args.config) if args.config else None
        existed = bool(asked and asked.exists())
        dst = init_config(asked)
        shown = dst.resolve()
        if existed:
            print(f"[main] 配置文件已存在，没动它：{shown}")
        else:
            print(f"[main] 配置文件已就绪：{shown}")
        print("[main] 打开它，把 api.api_key 换成你自己的 DeepSeek key（或改用环境变量）。")
        return 0

    cfg, cfg_path = load_config(args.config)
    if cfg_path.exists():
        print(f"[main] 配置来自 {cfg_path}")
    else:
        print(f"[main] 未找到 {cfg_path.name}，使用内置默认配置")
    # 这句话以前只写在 stdout 上：后台实例的 stdout 是 DEVNULL（日志还没建好），
    # 前台那份也没落进文件。用户把 --config 指错（或相对路径在子进程里换了基准，
    # 见 _pin_cli_paths）时，日志里一个字都没有，只能靠猜（第 69 轮）。
    cfg_missing = not cfg_path.exists()

    if args.show_config:
        return run_show_config(cfg, cfg_path)

    if args.web:
        # 先把「用户到底在编辑哪份配置」钉成绝对路径：Web 服务可能从任何 cwd
        # 启动，保存时写错文件比读不到更糟（第 108 轮）。
        from t_ime import webpanel

        return webpanel.serve(cfg_path, ROOT, port=args.web_port,
                              open_browser=not args.no_browser)

    if args.build_index:
        from t_ime import pinyin as _pinyin_mod
        from t_ime.pinyin import load_or_build_index

        # 第 123 轮：先确认依赖，再动旧缓存。以前的顺序是「删缓存 → 建索引」：
        # 用系统那个 python（没装 jieba / pypinyin）跑这条命令时，2 MB 的好缓存
        # 先被删掉，然后才崩在 ModuleNotFoundError 上 —— 用户只是想重建索引，
        # 结果连原来能用的缓存都没了，屏幕上还只有一段 traceback。
        missing = _pinyin_mod.missing_dependencies()
        if missing:
            print(f"[main] 缺依赖：{'、'.join(missing)} —— 这台解释器读不到词库，没法建索引。")
            print("[main] 用项目里的解释器（直接 pip 会装到系统的 python 里）：")
            print(r"[main]   .venv\Scripts\python.exe -m pip install -r requirements.txt")
            print("[main] 旧索引没动，照常能用。")
            return 1
        cache = ROOT / cfg["pinyin"]["dict_cache"]
        # 建到旁边一个新文件上，成功了再原子换上去：中途失败（缺依赖、词库读不
        # 出来、参数把索引筛成空的、磁盘写不进去）都不该连累现在这份缓存。
        tmp_cache = cache.with_name(cache.name + ".rebuild")
        tmp_cache.unlink(missing_ok=True)
        print(f"[main] 先建到临时文件 {tmp_cache.name}，建好了再替换旧索引")
        t0 = time.time()
        try:
            idx = load_or_build_index(
                tmp_cache,
                min_freq=int(cfg["pinyin"]["min_freq"]),
                max_word_len=int(cfg["pinyin"]["max_word_len"]),
                verbose=True,
            )
        except Exception as exc:  # 建索引失败不该让用户在 traceback 里找答案
            tmp_cache.unlink(missing_ok=True)
            print(f"[main] 建索引失败：{type(exc).__name__}: {exc}")
            print(f"[main] 旧索引没动（{cache.name}），照常能用。")
            return 1
        took = time.time() - t0
        print(f"[main] 索引 {len(idx):,} 个键，耗时 {took:.1f}s")
        if not idx:
            tmp_cache.unlink(missing_ok=True)
            print(f"[main] 建出来的索引是空的（pinyin.min_freq 现在是 {cfg['pinyin']['min_freq']}）—— "
                  f"换上新索引等于让打字没有候选，旧索引没动（{cache.name}）。")
            print("[main] 把 pinyin.min_freq 调小（例如 20）再跑一次。")
            return 1
        # 这条命令的活儿就是「重建缓存」：写不进去 = 没干成。以前照样 rc 0，
        # 用户以为缓存更新了，其实下次启动还得再建一遍（第 89 轮）。
        if _pinyin_mod.LAST_CACHE_ERROR:
            tmp_cache.unlink(missing_ok=True)
            print(f"[main] 索引缓存写不进去：{_pinyin_mod.LAST_CACHE_ERROR}")
            print(f"[main] 这次只在内存里建好，旧索引没动（{cache.name}）—— "
                  "检查 pinyin.dict_cache 指向的位置")
            return 1
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp_cache.replace(cache)
        except OSError as exc:
            tmp_cache.unlink(missing_ok=True)
            print(f"[main] 新索引换不上去：{type(exc).__name__}: {exc}")
            print(f"[main] 旧索引没动（{cache.name}），照常能用。")
            return 1
        print(f"[main] 已换上新的 {cache.name}")
        return 0

    if args.check:
        # cfg_path 只有在用户真的用 --config 指定过时才算「指定」：默认路径不存在是
        # 首次运行（走内置默认值），不该被当成「你指的这份读不到」。
        return run_check(cfg, ROOT, W, cfg_path=cfg_path if args.config else None)

    if args.tray_check:
        return run_tray_check(cfg, ROOT, W)

    if args.hook_check:
        from t_ime import daemon as _daemon_hc
        return run_hook_check(cfg, ROOT, W, args.hook_check_seconds,
                              log_path=_daemon_hc.resolve_log_path(ROOT, args.log, cfg))

    # `--doctor-quick` / `--doctor-seconds` 单独出现时也要进体检。
    # 它们原本只是 `--doctor` 的修饰参数，而 argparse 里的修饰参数是独立开关：
    # 用户照着 README「加 --doctor-quick 会跳过三项」直接敲 `python main.py --doctor-quick`，
    # 那一行**谁都不认领**，于是流程一路走到最后，程序当场当常驻实例跑起来 ——
    # 用户本想体检，结果起了一个输入法，还占着控制台不退出（第 32 轮真机撞到）。
    if args.doctor or args.doctor_quick or args.doctor_seconds > 0:
        from t_ime import daemon as _daemon  # 局部导入：这条分支比别处更早
        return run_doctor(
            cfg, ROOT, W,
            seconds=args.doctor_seconds,
            probe_tray=not args.doctor_quick,
            probe_hook=not args.doctor_quick,
            pipeline=not args.doctor_quick,
            log_path=_daemon.resolve_log_path(ROOT, args.log, cfg),
            cfg_path=cfg_path,
            cfg_explicit=bool(args.config),
        )

    if args.demo:
        from t_ime.app import run_demo
        return run_demo(cfg, ROOT, seconds=args.demo_seconds)

    # ---- 开机自启开关（不需要跑起来，改完注册表就退出）----
    from t_ime import daemon

    if args.autostart or args.no_autostart or args.autostart_status:
        if args.autostart_status:
            state = daemon.autostart_status()
            human = {"registry": "已开启（注册表）",
                     "startup-folder": "已开启（启动文件夹）",
                     "none": "未开启"}[state]
            # 「注册表里有这么一项」不等于「开机时它真的跑得起来」：换过目录、
            # 重建过 .venv、装了两份副本，都会留下一条指向虚空的命令。见踩坑 55。
            problem = daemon.autostart_problem(ROOT)
            if state != "none" and problem:
                print(f"[main] 开机自启：{human} —— 但它不会生效：{problem}")
                print("[main] 修好：run.bat --autostart 会按当前路径重写这一项")
            else:
                print(f"[main] 开机自启：{human}")
            return 0
        want = bool(args.autostart)
        ok, detail = daemon.set_autostart(want, ROOT, run_argv)
        if not ok:
            print(f"[main] 开机自启设置失败：{detail}", file=sys.stderr)
            return 1
        print(f"[main] 开机自启已{'开启' if want else '关闭'}（{detail}）")
        if want:
            # 「已开启」只说明这一项写进去了，不说明开机时跑的是**你现在这套**：
            # --config/--log 若没跟着记录，开机起来的实例会换回默认配置（第 68 轮）。
            recorded = daemon.autostart_command(ROOT, run_argv)
            carried = daemon.autostart_extra(run_argv)
            if recorded:
                print(f"[main] 开机时执行：{recorded}")
            if carried:
                print(f"[main] 已把 {' '.join(carried)} 写进自启项（路径已转成绝对路径）。")
            else:
                print("[main] 自启项只带 --config/--log；这次两个都没给，"
                      "开机时用的是本目录的 config.json。")
        return 0

    if args.panel:
        from t_ime import panel

        # 三种来源：spawn_panel / restart_elevated 拉起的副本带 --detached，
        # 就地开窗；用户在终端敲 --panel 时不带，改成另起一个进程，终端立刻能关。
        if args.detached:
            # 提权重启走的是这条：那个进程没人替它开窗，所以它自己开；
            # 同时它还把常驻实例拉起来（--background），否则用户确认完 UAC
            # 会发现输入法根本没在跑（见 panel.restart_elevated）。
            if args.background:
                ok, detail = panel.spawn_instance(ROOT, cfg_path, args.log)
                print(f"[main] {detail}")
                if not ok:
                    return 1
            return panel.run_panel(cfg_path, ROOT, W)
        # 只想要窗口（不带 --background / --foreground）：另起一个进程去开。
        # 带那两个开关时必须往下走，不能在这里提前 return —— 否则
        # `run.bat --foreground` 再也不会真正启动输入法，只会弹个窗口（第 124 轮）。
        if not (args.background or args.foreground):
            ok, detail = panel.spawn_panel(ROOT, cfg_path)
            print(f"[main] {detail}")
            return 0 if ok else 1

    # ---- 已经是第二个实例了：变成一个控制面板 ----
    # 为什么不是简单地说一句「已经在跑了」就退出：托盘图标在低完整性环境里
    # 装不上（UIPI 会拦），而托盘本来是唯一能退出/挂起/换语言的入口。用户第二次
    # 双击 run.bat 是很自然的一个动作，把它变成操作面板，就不依赖托盘了。
    if not args.detached and W.instance_exists():
        # 把这次命令行真正加载的那份配置也传进去：用户第二次双击时可能带着
        # --config 另一个文件（第 65 轮），面板若死认本目录的 config.json，
        # 就会一边说「配置来自 X」一边把 Y 指给他看。
        if _want_panel(args, cfg):
            from t_ime import panel

            ok, detail = panel.spawn_panel(ROOT, cfg_path)
            if ok:
                print(f"[main] {detail}：输入法已经在跑了，窗口里的「保存」会通知它热重载。")
                return 0
            print(f"[main] {detail}", file=sys.stderr)
        return run_control_panel(cfg, ROOT, W, cfg_path=cfg_path)

    # ---- 后台常驻：交给 pythonw 再拉一次自己，父进程立刻退出 ----
    # `--foreground` 永远赢：run.bat 默认会把 --background 传进来，用户想在前台
    # 盯着日志看的时候，一条 `run.bat --foreground` 就该能盖掉它。
    want_background = args.background and not args.foreground

    if want_background and not args.detached:
        # 把 --config / --log 钉成绝对路径再交给子进程：子进程的 cwd 是项目根，
        # 用户却可能从别处启动。相对路径在父子两边解析成不同文件的后果是
        # **实例悄悄用默认配置**，而父进程把「配置来自 …」和自定义热键都报了（第 69 轮）。
        # 用 run_argv 而不是 sys.argv：main(argv) 被测试/驱动调用时，那份 argv 才是真的。
        log = daemon.resolve_log_path(ROOT, args.log, cfg)
        spawn_argv = _pin_cli_paths(run_argv, cfg_path, log)
        # 日志路径也交过去：配置里的 output.log_file 只有父进程算过，
        # 子进程启动逻辑如果自己从命令行重算，就会把「日志见 …」指向另一个文件（第 70 轮）。
        rc = daemon.spawn_background(ROOT, spawn_argv, log_path=log)
        if rc == daemon.SPAWN_ALREADY_RUNNING:
            # 同时按了两下 run.bat / 自启与手动双击撞车：两份子进程里只有一份
            # 抢到互斥体，另一份按约定退出 3。以前这里被当成「后台启动失败」，
            # run.bat 还 pause —— 可输入法根本没事（第 106 轮）。「已经有一份
            # 在跑」这句话由 daemon 那层说（它拿得到 pid），这里只说下一步。
            print("[main] 已经有一份在后台跑着了，没重复启动。")
            print(f"[main] 日志：{log}")
            # 但「再双击一次」的唯一目的就是找入口：双击打包版 exe 时尤其如此
            # （没有 run.bat 可以点，也没有控制台能看菜单），所以这里必须开窗
            # —— 以前直接 return 0，用户看到的就是「双击了，毫无反应」（第 125 轮）。
            if _want_panel(args, cfg):
                from t_ime import panel

                ok, detail = panel.spawn_panel(ROOT, cfg_path, log)
                print(f"[main] {detail}")
                if not ok:
                    print(f"[main] {detail}", file=sys.stderr)
            else:
                print("[main] 想操作它就再双击一次 —— 会弹出一个设置窗口。")
            return 0
        if rc == 0:
            print("[main] 已在后台启动（没有窗口，关掉这个黑框不影响它）。")
            print(f"[main] 日志：{log}")
            if _want_panel(args, cfg):
                from t_ime import panel

                ok, detail = panel.spawn_panel(ROOT, cfg_path, log)
                print(f"[main] {detail}：填 API key、改快捷键都在这个窗口里。")
                if not ok:
                    print(f"[main] {detail}", file=sys.stderr)
            else:
                print("[main] 想操作它：再双击一次 run.bat，会弹出一个控制面板。")
        elif rc == daemon.SPAWN_NOT_READY:
            # 第 93 轮：进程起来了，但 6 秒内还没报到「钩子装上了」—— 冷启动建索引
            # 可能远超 6 秒（max_word_len 大 / min_freq 小时实测 20 秒）。以前这里
            # 照样打印「已在后台启动」并返回 0，和上面那句警告互相打脸，run.bat 也
            # 当作成功安静退出；此刻实例连状态文件都还没写出来，再双击一次会看到
            # 「读不到状态：可能是旧版本在跑」。
            print("[main] 后台进程已经起来，但还没就绪：拼音索引还在建 / 键盘钩子还没装上。")
            print("[main] 这段时间敲的字不会被接管，也不会弹候选窗 —— 等它就绪再打字，"
                  "或者用 run.bat --foreground 盯着日志看。")
            print(f"[main] 日志：{log}")
            # 窗口照开：正是这种「双击了但没反应」的时刻，窗口里的状态卡才有用
            # （它自己会诊断「钩子还没收到按键」）。第 125 轮。
            if _want_panel(args, cfg):
                from t_ime import panel

                ok, detail = panel.spawn_panel(ROOT, cfg_path, log)
                print(f"[main] {detail}")
                if not ok:
                    print(f"[main] {detail}", file=sys.stderr)
        else:
            print(f"[main] 后台启动失败（退出码 {rc}）。", file=sys.stderr)
        return rc

    if args.detached or want_background:
        # 无控制台跑：日志必须落文件，否则出问题完全没线索
        log = daemon.resolve_log_path(ROOT, args.log, cfg)
        log_file = daemon.setup_file_logging(log, verbose=True)
        print(f"[main] 后台模式，日志写往 {log}")
    else:
        # 前台跑也写日志 —— 控制台一关就什么都不剩了，而用户报「不灵了」的时候
        # 日志往往是唯一的线索。控制台还在，所以两边都写。
        log = daemon.resolve_log_path(ROOT, args.log, cfg)
        log_file = daemon.setup_file_logging(log, verbose=True, keep_console=True)
        print(f"[main] 日志：{log}（控制台和文件同时写）")

    # 配置写错的警告是在 load() 里打的，可那时日志还没建好：后台子进程的
    # stdout 直接接的是 DEVNULL，前台那份也没进文件。补一份进日志，否则用户
    # 手改坏了一项配置、重启之后在日志里什么都找不到（第 45 轮补的）。
    from t_ime.config import LAST_PROBLEMS as _cfg_problems
    from t_ime.config import LAST_UNKNOWN as _cfg_unknown
    from t_ime.config import LAST_LOAD_ERROR as _cfg_load_error
    from t_ime.config import LAST_RETIRED as _cfg_retired

    # 「这份配置文件根本不存在」要写进日志：后台实例的 stdout 是 DEVNULL，
    # 实例用的其实是默认值，而用户在日志里找原因时只会看到一切正常。
    if cfg_missing:
        log_file.write(f"\n[config] 没找到配置文件 {Path(cfg_path).resolve()}，"
                       "这次用的是内置默认配置\n")
        log_file.flush()

    # 整份配置没读成（JSON 写坏 / 顶层不是对象）是最彻底的一种「改了没反应」：
    # 所有选项都退回默认值，而这句话偏偏只在 stdout 上。日志里必须有（第 52 轮）。
    if _cfg_load_error:
        log_file.write(f"\n[config] {_cfg_load_error}\n")
        log_file.flush()

    if _cfg_problems:
        log_file.write(f"\n[config] 配置里有 {len(_cfg_problems)} 项写错，已按默认值启动：\n")
        for _msg in _cfg_problems:
            log_file.write(f"[config]   - {_msg}\n")
        log_file.flush()

    # 拼错的键不会报错、也不会生效：用户改完配置重启，看到的是「什么都没发生」。
    # 后台模式的 stdout 同样是 DEVNULL，这句也只能补进日志（第 51 轮补的）。
    if _cfg_unknown:
        log_file.write(
            f"\n[config] 配置里有 {len(_cfg_unknown)} 个选项不认识（多半是拼写错），它们不会生效：\n"
        )
        for _key in _cfg_unknown:
            log_file.write(f"[config]   - {_key}\n")
        log_file.flush()

    # 我们从模板里发出去过、后来发现从来没接上的选项（commit_method 之类）：
    # 用户照着抄过、改过、重启过，程序以前一声不吭。后台模式同样看不到 stdout
    # 上那句解释，补进日志（第 54 轮补的）。
    if _cfg_retired:
        log_file.write(
            f"\n[config] 配置里有 {len(_cfg_retired)} 个从旧模板留下的选项，"
            "从来没生效过，已从模板删掉：\n"
        )
        from t_ime.config import RETIRED_KEYS as _retired_reasons
        for _key in _cfg_retired:
            log_file.write(f"[config]   - {_key}：{_retired_reasons.get(_key, '已删除')}\n")
        log_file.flush()

    # 单实例：装两个钩子会互相抢键，表现是「有时候有候选、有时候没有」，极难查
    # 注意这里要的是**claim**（真把锁拿走），不是 is_running（只问一句）。
    # 用错的话锁会被别的进程带走，而那个进程其实不是实例。
    if not daemon.claim_instance():
        msg = "[main] 已经有一个 translate-ime 在跑了，这次不再启动。"
        print(msg, file=sys.stderr)
        if sys.stdout is not sys.stderr and hasattr(sys.stdout, "write"):
            print(msg)
        return 3

    # 从这里开始才真的进入「启动」：上面那行 `from t_ime.app import App` 会拉起
    # pinyin / overlay，也就是第三方依赖第一次被需要的地方。**它必须在日志建好
    # 之后**——第 41 轮真机 bug：新机器上第一次双击 run.bat，这个 import 抛
    # ModuleNotFoundError（缺 pypinyin / jieba），而它发生在日志建立之前、子进程
    # 的 stderr 又接到 DEVNULL，于是父进程照样报「已在后台常驻」，用户看到的是
    # 「黑框一闪，什么都没发生」，日志里连一行都没有。
    try:
        from t_ime.app import App

        app = App(cfg, ROOT, verbose=True, install_hook=not args.no_hook, log_path=log,
                  config_path=cfg_path)
    except Exception as exc:
        return _fail_startup(exc)

    try:
        app.start()
    except KeyboardInterrupt:
        print("\n[main] 收到 Ctrl+C")
    except Exception as exc:
        traceback.print_exc()
        print(f"[main] 主循环异常退出：{exc!r}", file=sys.stderr)
        print("[main] 完整堆栈见上面几行（后台模式下已经写进日志文件）。", file=sys.stderr)
        return 1
    finally:
        app.shutdown()
    return 0


def _fail_startup(exc: BaseException) -> int:
    """启动阶段就崩了：traceback 之外，还要用一句人话指出最可能的原因。

    这条路径上最典型的是 ``ModuleNotFoundError``（没装依赖）和
    ``FileNotFoundError``（找不到 jieba 的 dict.txt）。双击 ``run.bat`` 的用户
    没有能停住的控制台，日志里那句人话往往是他能看到的唯一线索。
    """
    traceback.print_exc()
    if isinstance(exc, ModuleNotFoundError):
        print(f"[main] 启动失败：缺依赖 {exc.name!r}。", file=sys.stderr)
        print("[main] 装依赖：.venv\\Scripts\\python.exe -m pip install -r requirements.txt",
              file=sys.stderr)
        print("[main] 还没有 .venv 的话：先 python -m venv .venv，装完再双击 run.bat。",
              file=sys.stderr)
    elif isinstance(exc, FileNotFoundError):
        print(f"[main] 启动失败：{exc}", file=sys.stderr)
        print("[main] 多半是依赖没装（中文词库是随 jieba 一起来的）："
              ".venv\\Scripts\\python.exe -m pip install -r requirements.txt", file=sys.stderr)
    else:
        print(f"[main] 启动失败：{exc!r}", file=sys.stderr)
    print("[main] 完整堆栈见上面几行（后台模式下已经写进日志文件）。", file=sys.stderr)
    return 1


def _effective_keys(hk: dict) -> int:
    """「敲进钩子、并被当成输入处理」的按键数 = 真实按键 + 别的程序转发的。

    「候选窗一次都没出现」「有没有人敲过键」这类判断必须用它，不能用 ``real``：
    装了改键器 / 宏 / 软键盘的机器（PowerToys Keyboard Manager、AutoHotkey、
    罗技/雷蛇驱动…）上 ``real`` 永远是 0，而用户明明已经敲了字 ——
    用 ``real`` 判断会让面板和体检一边说「收到过 4 个有效按键」、
    一边说「你还没敲过键」，同一屏上自相矛盾（第 71 轮真机撞到的）。
    """
    return int(hk.get("real") or 0) + int(hk.get("foreign_injected") or 0)


def _describe_hook(st: dict, hk: dict) -> str:
    """用一句话回答「我敲的键到底进没进钩子」。

    这是「打字没反应」排查里最关键的一格：有效按键数为 0（键根本没进来，看钩子侧）
    还是 > 0（键进来了，看候选窗 / 上屏侧）是两种完全不同的故障。
    钩子被 Windows 悄悄摘掉、被安全软件拦、焦点在提权窗口 —— 都会表现为一个都没有。

    「有效按键」= 键盘硬件来的 + **别的程序转发来的**（改键器 / 宏 / 软键盘）。
    后者同样带 ``LLKHF_INJECTED`` 标志，但它是用户实实在在敲下去的，
    所以钩子只忽略带我们自己标记的回灌（见 ``winapi.T_IME_INJECT_TAG``）。
    """
    # 顺序很关键：安装失败时 `hook` 现在也是 False（第 107 轮改的），
    # 先问 error 才能报出真正的原因，而不是把「安全软件拦了」说成「你用 --no-hook 起的」。
    if hk.get("error"):
        return f"安装失败：{hk['error']}（打字不会有反应）"
    if not st.get("hook"):
        if hk.get("disabled_by_flag"):
            return "没安装（是用 --no-hook 起的，打字不会有反应）"
        return "没安装（这个实例没装键盘钩子，打字不会有反应）"
    real = int(hk.get("real") or 0)
    foreign = int(hk.get("foreign_injected") or 0)
    own = int(hk.get("self_injected") or 0)
    # 钩子探活有两套，别把它们混成一句话：
    #   · 常驻进程用被动对账（每 50 毫秒问系统「刚才有人按键吗」，钩子却没收到 = 漏键）
    #     —— 常驻是 pythonw 且候选窗不抢焦点，注入式自检在那里根本发不进系统。
    #   · `--hook-check` 用注入式自检（那里我们会自己造一个前台窗口），
    #     所以 probes / probe_* 只在跑过 --hook-check 的状态里才有值。
    probes = int(hk.get("probes") or 0)
    fails = int(hk.get("probe_failures") or 0)
    send_fails = int(hk.get("probe_send_failures") or 0)
    misses = int(st.get("hook_misses") or 0)
    reconciles = int(st.get("hook_reconciles") or 0)
    again = int(st.get("hook_reinstalls") or 0)
    trail = ""
    if again:
        trail += f" · 被系统摘掉过、已自动重装 {again} 次"
    if st.get("hook_giveup"):
        trail += " · 重装后仍然漏键，已停止自动重装（多半不是钩子的问题）"
    if misses:
        trail += f" · 探活发现漏键 {misses} 次"
    elif reconciles:
        # 这个数 > 0 才算「探活真的在跑」：没报漏键也可能只是没人在采样。
        trail += f" · 与系统对账成功 {reconciles} 次（探活在跑，钩子当时是活的）"
    if send_fails:
        trail += f" · 自检按键发不出去 {send_fails} 次（SendInput 被拒）"
    if fails:
        trail += f" · 注入自检失败 {fails} 次"
    elif probes:
        trail += f" · 注入自检通过 {probes} 次"
    if real + foreign <= 0:
        # 措辞要留余地：刚起来还没人敲过键时当然是 0，那不是故障。
        extra = f"（只看到 {own} 个我们自己注入的按键）" if own else ""
        return f"还没收到过真实按键{extra}{trail} —— 如果你已经敲过字，故障就在这（键没进钩子）"
    age = hk.get("last_effective_age", hk.get("last_real_age"))
    when = "刚刚" if age is None or age < 5 else f"{float(age):.0f} 秒前"
    text = f"收到过 {real + foreign} 个有效按键（最近 {when}）"
    if foreign:
        text += f"；其中 {foreign} 个是别的程序转发的（改键器/宏/软键盘，已按真实按键处理）"
    text += f" · 吞掉 {int(hk.get('swallowed') or 0)} 个"
    if own:
        text += f" · 另有 {own} 个是我们自己注入的（已忽略）"
    text += trail
    if not hk.get("alive", True):
        text += " · ⚠ 钩子线程已经没了"
    return text


# 控制面板（双击 main.py / run.bat 时看到的那个）的菜单。
# 命令名与托盘菜单的 item_id **完全一致**，所以 App._handle_tray_action 一个处理器
# 同时服务「托盘」和「命令文件」两个入口 —— tests/test_tray_menu.py 盯着两边不跑偏。
PANEL_MENU: tuple[tuple[str, str], ...] = (
    ("toggle_enabled", "挂起 / 恢复输入法"),
    ("toggle_direct", "英文直通 开 / 关"),
    ("toggle_default", "换默认上屏语言（中文 ⇄ 译文）"),
    ("help", "在候选窗上显示使用帮助"),
    ("open_log", "打开日志文件"),
    ("open_config", "打开配置文件"),
    ("quit", "退出 translate-ime"),
)
PANEL_COMMANDS: tuple[str, ...] = tuple(cmd for cmd, _ in PANEL_MENU)


def run_control_panel(cfg: dict, root_dir: Path, W,
                      cfg_path: Path | None = None) -> int:
    """已经在跑的时候，第二次启动变成控制面板。

    它读运行中实例写下的状态文件，把命令投进命令文件；两条路都是普通文件，
    所以不需要任何 IPC，也不需要托盘图标（后者在低完整性环境里会被 UIPI 拦掉）。

    `cfg_path` 是本次命令行真正加载的那份配置（可能来自 --config）。不传就退回
    `<root>/config.json`；传了却还写死本目录那份，面板会自相矛盾：上面刚打印
    「配置来自 X」，下面又指着 Y 说「配置文件」，连「几个选项没写」也是在数 Y。
    """
    from t_ime import daemon
    from t_ime.config import missing_keys, read_raw, unknown_keys

    conf_path = Path(cfg_path) if cfg_path else root_dir / "config.json"
    raw_conf, _raw_conf_path = read_raw(conf_path)
    unk_conf = unknown_keys(raw_conf)
    miss_conf = missing_keys(raw_conf)
    if not conf_path.exists():
        conf_note = "（不存在，用内置默认值）"
    elif unk_conf:
        conf_note = f"（{len(unk_conf)} 个键拼错、会被忽略：{', '.join(unk_conf[:3])} —— 跑 --show-config 看）"
    elif miss_conf:
        conf_note = f"（另有 {len(miss_conf)} 个选项没写，正在用默认值）"
    else:
        conf_note = "（全部选项都写在里面）"

    def describe(st: dict | None) -> list[str]:
        if not st:
            # 实例是真的在跑、只是状态文件还没写出来（冷启动建索引时就是这样）——
            # 以前一律说「可能是旧版本在跑，或者状态文件被删了」，把「正在启动」
            # 说成了故障（第 93 轮真机：max_word_len=12 时索引要建 20 秒，这期间
            # 双击 run.bat 得到的就是这句）。
            try:
                booting = daemon.is_running()
            except Exception:  # noqa: BLE001 —— 探测不了就按老话术说
                booting = False
            if booting:
                return ["（实例正在启动：还没写出状态文件 —— 多半在建拼音索引 / 装键盘钩子，"
                        "稍等几秒再双击一次）"]
            return ["（读不到状态：可能是旧版本在跑，或者状态文件被删了）"]
        ov = st.get("overlay") or {}
        shows = int(ov.get("shows") or 0)
        # 短提示（启动引导、挂起/切模式的气泡）也走同一条显示路径，会 +1 —— 只数
        # shows 的话，静置不动也会变成「候选窗 显示过 2 次」，而候选窗一次都没弹过
        # （第 86 轮真机）。老版本的状态文件没有 cand_shows，退回 shows 保持原话术。
        cand_raw = ov.get("cand_shows")
        cand_shows = shows if cand_raw is None else int(cand_raw or 0)
        toast_only = shows - cand_shows
        rect = ov.get("rect")
        effective = _effective_keys(st.get("hook_info") or {})
        if ov.get("error"):
            cand = f"显示失败：{ov['error']}"
        elif cand_shows <= 0:
            # 第 58 轮：0 次本身不是故障证据。三种情况下它本来就不该弹：
            # 挂起（字母直通）、英文直通（字母原样透传）、以及刚起完还没人敲过键。
            if not st.get("enabled"):
                # 挂起时 Shift 单击不响应（core 里那条件是 tapped and shift_tap_toggles
                # and enabled），恢复只认 hotkeys.toggle_enabled 或面板菜单。
                resume = _resume_hint(cfg)
                cand = ("从未显示过 —— 输入法现在是「挂起」状态，挂起时字母直通、"
                        "本来就不弹候选（"
                        + (f"按 {resume} 或面板菜单恢复" if resume else "用面板菜单恢复")
                        + "）")
            elif st.get("direct"):
                switch = _direct_hint(cfg)
                cand = ("从未显示过 —— 现在是「英文直通」，字母原样透传、本来就不弹候选"
                        + (f"（{switch} 切回拼音就会弹）" if switch else
                           "（没有切回拼音的键：input.shift_tap_toggles 关着，"
                           "hotkeys.toggle_direct 也没设）"))
            elif not st.get("hook"):
                # 钩子没装上的时候，「敲几个拼音字母」是一句**不可能生效**的建议：
                # 字母根本进不了程序，用户敲多久都不会有候选窗（第 107 轮）。
                # 这里也顺手把「安全软件拦了」和「你自己用 --no-hook 关的」分开说。
                hkv = st.get("hook_info") or {}
                why = str(hkv.get("error") or "")
                if why:
                    cand = (f"不会出现 —— 键盘钩子没装上（{why}），按键根本进不了程序；"
                            "先把钩子修好（多半是安全软件拦了全局钩子，加白名单后重启），"
                            "这一行才有意义")
                elif hkv.get("disabled_by_flag"):
                    cand = ("不会出现 —— 这个实例是用 --no-hook 起的，键盘输入没接过来"
                            "（想看候选窗就重启时去掉 --no-hook，或者 run.bat --demo 预览）")
                else:
                    cand = "不会出现 —— 这个实例没装键盘钩子，打字不会有反应"
            elif effective <= 0:
                cand = "还没显示过 —— 还没收到过任何按键（敲几个拼音字母，它应该出现）"
            else:
                cand = (f"从未显示过 —— 你敲的 {effective} 个有效按键已经进钩子了，"
                        "候选窗却一次都没出现，这就是故障点")
            if toast_only > 0:
                # 上面那些「从未显示过」说的是候选窗；不点明的话，用户会拿
                # 屏幕上确实出现过的那几条提示来反驳。
                cand += f"（窗口显示过 {shows} 次，但那 {toast_only} 条都是提示，不是候选窗）"
        elif rect:
            where = "在显示器上" if ov.get("on_monitor") else "不在任何显示器上（跑到屏幕外了！）"
            cand = (f"显示过 {cand_shows} 次 · 最近 "
                    f"{rect[2]}x{rect[3]}+{rect[0]}+{rect[1]}（{where}）")
            if toast_only > 0:
                cand += f" · 另有 {toast_only} 条提示"
        else:
            cand = f"显示过 {cand_shows} 次"
            if toast_only > 0:
                cand += f" · 另有 {toast_only} 条提示"
        hk = st.get("hook_info") or {}
        # 开机自启是「当前状态」的一部分：它可能被系统悄悄退回启动文件夹，也可能压根没开成，
        # 用户不该靠记忆去分辨。这里是第二次双击最容易看到的地方，就顺手报出来。
        auto = daemon.autostart_status()
        auto_txt = {
            "registry": "已开启（注册表）",
            "startup-folder": "已开启（启动文件夹）",
        }.get(auto, "未开启（跑 run.bat --autostart 打开）")
        auto_problem = daemon.autostart_problem(root_dir) if auto != "none" else ""
        if auto_problem:
            auto_txt += f" ⚠ 但它不会生效：{auto_problem}"
        nb = int(st.get("boosts") or 0)
        # 文件名跟着 output.boost_file 走：写死的 user_boosts.json 在用户改过路径后
        # 就是一句假话。写不进去时更要说原因 —— 只显示「已记住 N 个词」会让人以为
        # 改选的词都存下来了，其实一条也没落盘（第 81 轮）。
        nb_name = Path(str(st.get("boost_file") or "user_boosts.json")).name
        nb_err = str(st.get("boost_error") or "")
        nb_load = str(st.get("boost_load_error") or "")
        if nb_load:
            # 权重文件读不懂：以前只打一行日志，面板照样说「还没记住任何词」，
            # 用户以为自学习从头开始是正常的 —— 其实上次记的词就在那份坏文件里（第 87 轮）。
            nb_txt = (f"{nb_name} 读不了（{nb_load}）—— 已留成 .broken 备份，"
                      f"这次自学习从零开始")
            if nb:
                nb_txt = f"已记住 {nb} 个词（新的），但 {nb_txt}"
        elif nb_err:
            nb_txt = f"已记住 {nb} 个词，但写不进 {nb_name}（{nb_err}）" if nb else \
                     f"写不进 {nb_name}（{nb_err}）"
        elif nb:
            nb_txt = f"已记住 {nb} 个词"
            nb_txt += (f"，已写入 {nb_name}" if int(st.get("boost_saves") or 0)
                       else "（最多 5 秒后落盘）")
        else:
            nb_txt = "还没记住任何词（用数字键改选候选就会记住）"
        # 「翻译到底有没有在工作」是第二个最常被问的：配了 key 却一直不出译文，
        # 以前只能盯着悬浮窗那行一两秒就消失的字。这里把引擎和最近一次失败摆出来。
        tr = st.get("translate") or {}
        if not tr:
            tr_txt = "（这个实例没报翻译状态，可能是旧版本在跑）"
        elif tr.get("enabled") is False:
            # 翻译是用户自己关的，不是故障：先说清楚状态，再说引擎是哪家 ——
            # 以前这里照旧说「离线词表（没配 API key）」，把「关掉了」说成「没配 key」（第 78 轮）。
            tr_txt = "已关闭（translate.enabled=false，不联网也不查词表）"
        else:
            if tr.get("mock"):
                why = str(tr.get("mock_reason") or "没配 API key")
                prov = f"离线词表（{why}）"
            else:
                prov = f"在线 {tr.get('provider', '?')}"
            tr_txt = (f"{prov} · 请求 {int(tr.get('requests') or 0)} 次"
                      f"，命中缓存 {int(tr.get('cache_hits') or 0)} 次")
            tr_errs = int(tr.get("errors") or 0)
            if tr_errs:
                why = tr.get("last_error") or "原因没记下来，看日志"
                # 第 117 轮：连不上网和 key 写错在原文里长得一样，面板照抄会把
                # 用户送去改 config.json。按种类先说人话，原文放后面。
                kind = str(tr.get("last_error_kind") or "")
                code = tr.get("last_error_status")
                if kind == "network":
                    tr_txt += (f" · 失败 {tr_errs} 次（连不上网 / 被防火墙拦了，"
                               f"不是配置写错；原文：{why}）")
                elif kind == "http":
                    tr_txt += (f" · 失败 {tr_errs} 次（服务器拒绝，HTTP {code}："
                               f"key / 额度 / 模型名的问题；原文：{why}）")
                elif kind == "parse":
                    tr_txt += (f" · 失败 {tr_errs} 次（回来的不是 JSON，"
                               f"多半是 base_url / path 指错了；原文：{why}）")
                else:
                    tr_txt += f" · 失败 {tr_errs} 次（最近：{why}）"

        # 托盘关掉和托盘装不上是两件事：output.tray=false 时说「没装上」会让用户
        # 去查 UIPI / 完整性级别，可他本来就关掉了这个功能（第 82 轮）。
        if st.get("tray_enabled") is False:
            tray_txt = "已关闭（output.tray=false，你自己关的）"
        elif st.get("tray"):
            tray_txt = "已装上"
        else:
            tray_txt = "没装上（不影响打字）"
        lang_txt = "译文" if st.get("active") == "en" else "中文"
        if st.get("active") == "en" and (st.get("translate") or {}).get("enabled") is False:
            # 译文行拿不到译文时 _commit 会退回中文；不写清楚的话，这一行就是一句空话。
            lang_txt += "（翻译关着，实际上屏会退回中文）"
        lines = [
            f"进程号     {st.get('pid', '?')}",
            f"输入法     {'已启用' if st.get('enabled') else '已挂起'}",
            f"输入模式   {'英文直通' if st.get('direct') else '拼音'}",
            f"上屏语言   {lang_txt}",
            f"托盘图标   {tray_txt}",
            f"开机自启   {auto_txt}",
            f"自学习     {nb_txt}",
            f"翻译       {tr_txt}",
            f"键盘钩子   {_describe_hook(st, hk)}",
            f"候选窗     {cand}",
        ]
        _note = str(st.get("status") or "")
        if _note:
            # 第 105 轮：实例拒绝某个动作时（翻译关着却去切「译文」），原因以前只弹
            # 在候选窗的 toast 上、1.6 秒就没。面板用户正盯着控制台，看到的只是
            # 「上屏语言 中文」原地不动 —— 会以为菜单项坏了。把实例的话摆出来。
            lines.append(f"实例提示   {_note}")
        errs = int(st.get("tick_errors") or 0)
        if errs:
            lines.append(f"主循环异常 累计 {errs} 次（带伤运行中，详情看日志）")
        cfails = int(st.get("commit_failures") or 0)
        if cfails:
            why = st.get("last_commit_error") or "原因没记下来，看日志"
            lines.append(f"上屏失败   累计 {cfails} 次（最近一次：{why}）")
        if st.get("caps"):
            lines.append("大写锁定   开（现在敲字母会直接打出英文大写，不弹候选；"
                         "关掉 CapsLock 就回到拼音）")
        lines.append(f"日志       {st.get('log', '?')}")
        # 实例用的配置可能不是本目录这一份（启动时带了 --config）。不说的话，
        # 上面这些行、以及下面那些热键提示，都会被当成「按本目录那份配置」的结果。
        inst_cfg = st.get("config")
        if inst_cfg:
            try:
                same = Path(str(inst_cfg)).resolve() == conf_path.resolve()
            except OSError:
                same = Path(str(inst_cfg)) == conf_path
            if not same:
                lines.append(f"实例配置   {inst_cfg}  ← 正在跑的实例用的是这一份，"
                             "不是本目录的 config.json（热键、上屏语言以它为准）")
        else:
            lines.append("实例配置   （这个实例没报配置路径，可能是旧版本在跑）")
        lines.append(f"配置文件   {conf_path} {conf_note}")
        return lines

    print()
    print("== translate-ime 已经在跑了 ==")
    print()
    st = daemon.read_status(root_dir)
    if st and daemon.status_is_stale(root_dir):
        age = daemon.status_age(root_dir)
        ago = "读不到更新时间" if age is None else f"{age:.0f} 秒没更新了"
        print(f"（状态文件 {ago} —— 那个实例可能已经卡死或被强杀了；")
        print(f"  它还在跑的话每 {daemon.STATUS_HEARTBEAT_SECONDS:.0f} 秒就会写一次心跳，"
              "超过两个心跳没来才算真的不动了）")
    for line in describe(st):
        print(f"  {line}")
    print()

    def _print_passive_hints() -> None:
        """非交互时的收尾提示：怎么关掉它、托盘为什么没有。"""
        # 别写死组合键：hotkeys.quit 改了以后，写死的那句话就成了假话。
        # 更要紧的是**用实例自己报的那个键**：面板读的 config.json 未必是实例
        # 启动时用的那一份（--config 可以指别处），按面板这份去提示，用户按下去
        # 什么都不会发生（第 60 轮真机：面板说 Ctrl+Alt+Q，实例其实是 F9）。
        from t_ime.hotkey import pretty
        quit_hk = str((st or {}).get("quit_hotkey") or "") or pretty(cfg["hotkeys"]["quit"])
        if quit_hk and quit_hk != "（没设热键）":
            print(f"[main] 想关掉它：按 {quit_hk}。")
        else:
            print("[main] 想关掉它：托盘图标右键 → 退出；托盘也没装上的话，用任务管理器"
                  "结束上面那个 pid（hotkeys.quit 是空的，没有退出热键可用）。")
        # st 可能是 None（状态文件读不到，上面已经说过了）—— 那就别猜托盘，
        # 只把「能用热键退出」这句留着。tests/test_control.py 就走过这条路。
        if st is not None and st.get("tray_enabled") is False:
            # 托盘是用户自己关的：别说成「装不上」，也别提 UIPI —— 改回 true 就有。
            print("[main] 托盘图标是按你的配置关掉的（output.tray=false）；"
                  "想要鼠标入口，把它改回 true 就有了。")
        elif st is not None and not st.get("tray"):
            print("[main] 托盘图标也没装上（本次启动的进程完整性级别低于 explorer），"
                  "所以这次连鼠标入口都没有 —— 不过键盘热键照常有效。")

    if not (sys.stdin and sys.stdin.isatty()):
        print("[main] 这里不是交互式控制台，没法给菜单。")
        _print_passive_hints()
        return 3

    menu = [(str(i), cmd, label) for i, (cmd, label) in enumerate(PANEL_MENU, start=1)]
    by_key = {k: (cmd, label) for k, cmd, label in menu}

    while True:
        print("  " + "   ".join(f"{k}) {lab}" for k, _, lab in menu if k != "7"))
        print("  7) 退出 translate-ime          0) 什么都不做")
        try:
            raw = input("  选择：").strip()
        except KeyboardInterrupt:
            print()
            return 0
        except EOFError:
            # 第 95 轮真机：Windows 上把 stdin 重定向到 NUL 时 ``sys.stdin.isatty()``
            # 仍然返回 True，于是走进了菜单却一个字符也读不到。以前这里静默
            # ``return 0``：脚本看得懂退出码的话，会以为「第二份实例也起来了」，
            # 其实只是面板空转了一次；而 ``--config`` 的脚本正是靠 3 判断这个。
            # 读不到输入 == 没法给菜单，按非交互的契约返回 3。
            print()
            print("[main] 读不到键盘输入（stdin 被重定向或已关闭），没法给菜单。")
            _print_passive_hints()
            return 3
        if raw in ("", "0", "q", "Q"):
            return 0
        entry = by_key.get(raw)
        if entry is None:
            print("  （没有这个选项）")
            print()
            continue
        cmd, label = entry
        if not daemon.send_command(root_dir, cmd):
            print("  [main] 写不进命令文件，可能没有目录写权限。", file=sys.stderr)
            return 1
        print(f"  已发出：{label}")
        if cmd == "quit":
            deadline = time.time() + 5.0
            while time.time() < deadline and W.instance_exists():
                time.sleep(0.2)
            print("  已退出。" if not W.instance_exists() else
                  "  还没退干净 —— 可能卡住了，用任务管理器结束那个 pid。")
            return 0
        time.sleep(0.5)
        st = daemon.read_status(root_dir)
        print()
        for line in describe(st):
            print(f"  {line}")
        print()


def _fmt_cfg_val(key: str, val) -> str:
    """把配置值印成人看的样子；名字像密钥的一律打码，别把 key 漏进日志。

    只看**最后一段**名字：``api.api_key`` 要打码，而 ``hotkeys.compose``
    里也含 "key"，它不是密钥、必须原样打出来。
    """
    leaf = key.rsplit(".", 1)[-1].lower()
    if leaf.endswith(("key", "token", "secret", "password")):
        s = str(val or "")
        return "（未配置）" if not s else f"***（已配置，{len(s)} 位）"
    return json.dumps(val, ensure_ascii=False)


def run_show_config(cfg: dict, cfg_path: Path) -> int:
    """把「配置到底长什么样」一次摊开：每个选项的生效值、它来自哪里、
    有哪些选项没写进 config.json、有没有拼错的键。

    只读，不改任何文件。拼错的键会被程序静静忽略，所以这里遇到就返回 1。

    整段类型写错（``"hotkeys": 5``）**不算**拼错：那是值写错，由 ``[config]``
    那几行负责说明，这里只附一句「另有 N 个键是整段类型写错」，退出码按
    「坏值」的老约定保持 0（第 122 轮；以前它会把这个键列进「不认识的键」
    并把退出码翻成 1，和同一次运行里 ``[config]`` 的计数互相打架）。
    """
    from t_ime.config import (
        DEFAULTS,
        EXAMPLE_PATH,
        effective_rows,
        env_api_key,
        flat_keys,
        get_dotted,
        missing_keys,
        read_raw,
        type_problem_prefixes,
        unknown_keys,
    )

    raw, path = read_raw(cfg_path)
    rows = effective_rows(cfg, raw)
    defaults_flat = flat_keys(DEFAULTS)

    print()
    print("== 生效配置 ==")
    print(f"  配置文件  {path}{'' if path.exists() else '（不存在，全部使用内置默认值）'}")
    print(f"  选项模板  {EXAMPLE_PATH}{'' if EXAMPLE_PATH.exists() else '（缺！跑 --init-config 会生成）'}")
    print(f"  API key   {'来自环境变量（优先级最高）' if env_api_key() else '来自配置文件，或未配置'}")
    print()
    label = {"file": "文件", "default": "默认", "env": "环境"}
    shown = [(k, s, v) for k, s, v in rows if s != "ignored"]
    width = max((len(k) for k, _, _ in shown), default=0)
    for key, src, val in shown:
        print(f"  [{label[src]}] {key.ljust(width)} = {_fmt_cfg_val(key, val)}")

    miss = missing_keys(raw)
    # 整段写成标量（"hotkeys": 5 / "output": 5）是**值**写错：上面 config.load()
    # 的 [config] 那几行已经点名说过原因，这里不能再按「拼错的键」算一遍 ——
    # 否则同一次运行里两处说法打架（[config] 说「1 个不认识」，这里说「2 个」），
    # 退出码也会把「坏值」误判成「拼错」（坏值的约定是 rc 0，第 122 轮）。
    # 口径与 config.load() 用的是同一个 helper，不许各写一份。
    type_prefixes = type_problem_prefixes()
    unk_all = unknown_keys(raw)
    unk = [
        key for key in unk_all
        if not any(key == p or key.startswith(p + ".") for p in type_prefixes)
    ]
    type_only = len(unk_all) - len(unk)
    print()
    if unk:
        print(f"== 有 {len(unk)} 个键 DEFAULTS 不认识（多半是拼错，程序会静静忽略） ==")
        for key in unk:
            print(f"  ! {key} = {_fmt_cfg_val(key, get_dotted(raw, key))}")
        print("  写错的键不会报错，只是永远不生效 —— 「改了配置却没反应」经常是这个。")
        print(f"  全部合法选项见 {EXAMPLE_PATH.name}。")
    else:
        print("== 没有拼错的键 ✓ ==")
    if type_only:
        print(f"  另有 {type_only} 个键是整段类型写错（上面 [config] 那几行已经说过原因），"
              "不按「拼错」算。")
    if miss:
        print()
        print(f"== config.json 里没写、正在用默认值的选项（{len(miss)} 个） ==")
        for key in miss:
            print(f"  · {key} = {_fmt_cfg_val(key, defaults_flat.get(key))}")
        print("  它们照样生效；想让它们出现在 config.json 里，照 config.example.json 抄一段即可。")
    print()
    return 1 if unk else 0


def _unbound_note(name: str, cfg: dict) -> str:
    """热键空着时（合法配置）说明「这个动作还能怎么用」。

    以前对所有动作都写「（未绑定，靠点一下 Shift）」—— 可 Shift 单击只管
    「拼音 ⇄ 英文直通」，对 quit / compose / toggle_enabled 一点用都没有；
    而 ``input.shift_tap_toggles=false`` 时连切模式也不归 Shift 管。

    第 92 轮：``toggle_default`` 也从这一族里摘出去 —— 它切的是「空格上屏
    中文还是译文」，Shift 单击切的是英文直通，两件事。以前这里说「可以点一下
    Shift 切换」，用户按下去得到的是直通，不是译文。
    """
    from t_ime.hotkey import tap_switch_hint

    if name == "toggle_direct":
        if tap_switch_hint(cfg):
            return "（未绑定，可以点一下 Shift 切换）"
        return "（未绑定 —— 这个动作没有快捷键了）"
    if name == "toggle_default":
        return "（未绑定 —— 用托盘 / 面板菜单切）"
    if name in ("quit", "toggle_enabled"):
        return "（未绑定 —— 只能用面板 / 托盘菜单）"
    return "（未绑定 —— 这个动作没有快捷键了）"


def _resume_hint(cfg: dict) -> str:
    """挂起之后怎么恢复（只有 ``hotkeys.toggle_enabled`` 管用；Shift 单击挂起时不响应）。"""
    from t_ime.hotkey import resume_hint

    return resume_hint(cfg)


def _direct_hint(cfg: dict) -> str:
    """「拼音 ⇄ 英文直通」现在按什么（Shift 单击 / ``hotkeys.toggle_direct``）。"""
    from t_ime.hotkey import direct_switch_hint

    return direct_switch_hint(cfg)


def _interpreter_note(root_dir: Path) -> tuple[bool, str]:
    """现在跑检查的这个 python，是不是项目 ``.venv`` 里那个？

    第 118 轮的真实场景：用户照文档敲 ``python main.py --check``，命中的是
    **系统装的** Python（依赖却在项目的 ``.venv`` 里），于是 jieba / pypinyin
    全「没装」，索引建不出来 —— 屏幕上一片 FAIL，可 ``run.bat`` 起的那份实例
    其实好好的。检查本身没错，缺的是「你在用哪个解释器」这一行。
    """
    import os

    if getattr(sys, "frozen", False):
        # 打包版：没有 .venv、也没有可换的解释器，依赖全在 exe 里。别拿它去比 .venv。
        return True, f"{Path(sys.executable).name}（打包版，依赖都在 exe 里）"

    venv_py = Path(root_dir) / ".venv" / "Scripts" / "python.exe"
    me = Path(sys.executable)
    try:
        same = os.path.normcase(str(me.resolve())) == os.path.normcase(str(venv_py.resolve()))
    except OSError:
        same = False
    if same:
        return True, str(me)

    from t_ime import pinyin as _pinyin

    missing = _pinyin.missing_dependencies()
    if missing:
        return False, (
            f"{me} —— 不是项目 .venv 里的 python，依赖可能没装在它里面"
            f"（缺：{'、'.join(missing)}）；换成 .venv\\Scripts\\python.exe 再跑一次，"
            f"或者直接跑 run.bat --check"
        )
    return True, f"{me}（不是 .venv 里的，但依赖齐全，可以继续）"


def run_check(cfg: dict, root_dir: Path, W, cfg_path: Path | None = None) -> int:
    import ctypes
    import urllib.parse

    from t_ime.pinyin import SYLLABLES, _jieba_dict_path, can_segment, load_or_build_index
    from t_ime.translator import Translator, ZH2EN

    ok = True

    def line(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        if not good:
            ok = False
        print(f"  [{'ok ' if good else 'FAIL'}] {label:<28} {detail}")

    def warn(label: str, detail: str = "") -> None:
        """「这台机器上验不了」，但不装成通过、也不算故障。

        第 117 轮：没网时 `--check` 以前把翻译往返打成 `[FAIL] … <urlopen error
        [WinError 10061] 由于目标计算机积极拒绝，无法连接。>` —— 用户拿着一个
        FAIL 去翻 config.json，可配置一个字都没错。环境造成的验不了用 [warn]
        说清楚，跟 `[warn] 翻译走离线词表` 一个口径：不翻 ok，也不假装成功。
        """
        print(f"  [warn] {label:<28} {detail}")

    print()
    print("== 运行环境 ==")
    line("Python", True, sys.version.split()[0])
    _interp_ok, _interp_detail = _interpreter_note(Path(root_dir))
    if _interp_ok:
        print(f"  [info] {'解释器':<28} {_interp_detail}")
    else:
        # 依赖不全 + 不是 .venv 的 python：下面的 FAIL 全是这一行引起的（第 118 轮）。
        line("解释器", False, _interp_detail)
    line("sizeof(INPUT) == 40", ctypes.sizeof(W.INPUT) == 40, str(ctypes.sizeof(W.INPUT)))

    print()
    print("== 配置文件 ==")
    # 第 83 轮：配置文件里「值写错」（整段类型不对 → 全部退回默认值）或「键拼错」
    # （永远不生效）时，实际跑的配置和文件里写的不是一回事。以前这些只在启动时由
    # config.load() 打一行警告，`--check` 末尾照样说「全部通过 ✅」—— 用户拿它当
    # 闸门看，就以为配置没问题。这几类一律进 ok 判定。
    from t_ime import config as _config

    _conf_path = Path(cfg_path) if cfg_path is not None else Path(root_dir) / "config.json"
    if not _conf_path.exists():
        if cfg_path is not None:
            line("配置文件", False,
                 f"{_conf_path} 不存在 —— 你指定了这一份，但读不到，下面检查的是内置默认值")
        else:
            print(f"  [info] {'配置文件':<28} {_conf_path} 不存在，全部用内置默认值"
                  "（run.bat --init-config 生成一份）")
    else:
        # 按文件重新读一遍：cfg 可能是别处构造好的，而「文件里到底写了什么」只有
        # 读文件才知道。config.load() 对同一份文件重复调用不会重复打印（按内容去重），
        # 所以这里再读一次不会刷出重复的警告。
        _config.load(_conf_path)
        _probs = list(_config.LAST_PROBLEMS)
        _unknown = list(_config.LAST_UNKNOWN)
        _retired = list(_config.LAST_RETIRED)
        _load_err = _config.LAST_LOAD_ERROR
        line("配置文件", True, str(_conf_path))
        if _load_err:
            line("配置能读", False, _load_err)
        if _probs:
            _more = f"；另外 {len(_probs) - 1} 项看上面的启动提示" if len(_probs) > 1 else ""
            line("写错的选项", False,
                 f"{len(_probs)} 项已按默认值顶上（你写的值没生效）：{_probs[0]}{_more}")
        if _unknown:
            _shown = "、".join(_unknown[:3])
            if len(_unknown) > 3:
                _shown += f" … 共 {len(_unknown)} 个"
            line("不认识的键", False, f"{_shown} —— 多半是拼写错，永远不会生效")
        if _retired:
            print(f"  [info] {'旧模板留下的选项':<28} {len(_retired)} 个"
                  "（从来没有生效过，删掉即可）")
        try:
            _raw, _ = _config.read_raw(_conf_path)
        except Exception:
            _raw = {}
        if not isinstance(_raw, dict):
            _raw = {}
        _miss = _config.missing_keys(_raw)
        if _miss:
            print(f"  [info] {'没写的选项':<28} {len(_miss)} 个，正在用内置默认值")

    # 第 121 轮：`--check --config 别的.json` 以前只说「全部通过 ✅」，一个字都不提
    # **正在跑的那个实例用的是哪份配置**。用户照着报告改那份文件、再按新热键，按下去
    # 没反应（实例读的是另一份）—— 这跟第 109 轮网页控制板的「假同步」是同一个坑：
    # 检查结果只对一个文件成立，用户却拿它当整个输入法的结论。
    from t_ime import daemon as _daemon

    _inst_st: dict = {}
    if _daemon.is_running():
        _inst_st = _daemon.read_status(root_dir) or {}
        _inst_cfg = str(_inst_st.get("config") or "")
        _inst_quit = str(_inst_st.get("quit_hotkey") or "")

        def _same_file(a: str, b: str) -> bool:
            try:
                import os

                return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
            except Exception:
                return str(a) == str(b)

        if not _inst_cfg:
            print(f"  [info] {'常驻实例':<28} 有一个实例在跑（pid {_inst_st.get('pid')}），"
                  "但状态里没写它用的是哪份配置；这份报告只对上面那个文件成立")
        elif cfg_path is not None and not _same_file(str(cfg_path), _inst_cfg):
            warn("常驻实例", f"它用的是 {_inst_cfg}（退出键 {_inst_quit or '没配'}）—— "
                             f"不是这次检查的 {cfg_path}。改配置要改它那一份，"
                             "或者退出实例后重启才会生效")
        else:
            print(f"  [info] {'常驻实例':<28} 正在跑（pid {_inst_st.get('pid')}），"
                  f"用的就是这份配置（退出键 {_inst_quit or '没配'}）")

    print()
    print("== 系统输入法 ==")
    # 本工具不是 Windows 输入法：没有 TSF 文本服务，也没注册键盘布局，所以
    # 语言栏 / Win+Space 的列表里永远不会有它。以前这里一个字都不提，用户的
    # 第一反应就是「我怎么选它？」—— 在语言栏里找不到，就以为没装好。
    print("  [info] 本工具不是系统输入法：语言栏 / Win+Space 里不会出现它，")
    print("         启动后靠全局键盘钩子接管字母，不需要选中。")
    _layouts = W.installed_input_layouts() if hasattr(W, "installed_input_layouts") else []
    if _layouts:
        from t_ime.hotkey import pretty as _pretty

        print("  [info] 系统里装着的输入法：" + "、".join(n for _, n in _layouts) + "。")
        _suspend = _pretty(cfg["hotkeys"]["toggle_enabled"]) if cfg["hotkeys"]["toggle_enabled"] else ""
        print("         它们照旧可用；本工具运行时会先接管字母（微软拼音拿不到这些键），")
        print("         想把手感还给系统输入法就"
              + (f"按 {_suspend} 挂起本工具。" if _suspend else "把 hotkeys.toggle_enabled 配上再挂起。"))
    else:
        print("  [info] 读不出系统里装了哪些输入法（不影响本工具使用）。")

    print()
    print("== 热键解析 ==")
    # 第 63 轮：光验证「字符串能解析」不够 —— 动作名拼错（hotkeys.quitt）和两个动作
    # 撞同一个组合键时，这里以前照样全是 [ok]，真实例却只把一句 [core] … 写进日志。
    # 用户照着「全部通过 ✅」的体检结论去按退出键，结果按下去把输入法挂起了。
    from t_ime.hotkey import check_hotkeys as _check_hotkeys

    for name, spec, problem in _check_hotkeys(cfg["hotkeys"], W.parse_hotkey):
        if not spec.strip():
            line(name, True, _unbound_note(name, cfg))
        elif problem:
            line(name, False, f"{spec} -> {problem}")
        else:
            mods, vk = W.parse_hotkey(spec)
            line(name, True, f"{spec} -> mods={sorted(mods)} vk={vk}")

    # 第 90 轮：input.shift_tap_ms=0 是合法值（0–2000），但时间窗为零时
    # IMECore.handle_key 里 held_ms <= 0 永不成立，Shift 单击完全无效 ——
    # 而 banner / 面板 / 引导都还在教用户按它，两个开关自相矛盾要说出来。
    from t_ime.hotkey import shift_tap_problem as _shift_tap_problem

    _tap_msg = _shift_tap_problem(cfg)
    if _tap_msg:
        _tap_ms = int((cfg.get("input") or {}).get("shift_tap_ms") or 0)
        if _tap_ms <= 0:
            line("Shift 单击", False, _tap_msg)
        else:
            print(f"  [info] Shift 单击                      {_tap_msg}")

    print()
    print("== 中文词库 ==")
    dp = _jieba_dict_path()
    line("jieba dict.txt", dp is not None,
         str(dp) if dp else
         "没找到 —— 多半是没装依赖（这台解释器里没有 jieba，或它的 dict.txt 缺了）；"
         "装的时候要用项目里的解释器：.venv\\Scripts\\python.exe -m pip install -r requirements.txt")
    line("拼音音节表", len(SYLLABLES) > 400, f"{len(SYLLABLES)} 个音节")
    for probe, want in (("nihao", True), ("n", False), ("zhuang", True), ("xyz", False)):
        line(f"can_segment({probe!r})", can_segment(probe) is want)

    print()
    print("== 拼音索引 ==")
    t0 = time.time()
    min_freq = int(cfg["pinyin"]["min_freq"])
    cache_name = str(cfg["pinyin"]["dict_cache"])
    try:
        idx = load_or_build_index(
            root_dir / cache_name,
            min_freq=min_freq,
            max_word_len=int(cfg["pinyin"]["max_word_len"]),
            verbose=True,
        )
        if idx:
            line("索引可用", True, f"{len(idx):,} 键 / {time.time() - t0:.1f}s")
        else:
            # 0 个键的索引一条候选都查不出来，打字等于没反应。
            # 以前这里照样判 [ok ]，然后下面的候选检查整段静默消失 ——
            # 屏幕上「全部通过 ✅」而输入法已经废了（第 79 轮）。
            line("索引可用", False,
                 f"{len(idx):,} 键 —— 索引是空的，打字不会有候选；"
                 f"pinyin.min_freq 现在是 {min_freq}（设太大就把所有词都筛掉了），"
                 f"调小它，或删掉 {cache_name} 重新生成")
    except ModuleNotFoundError as exc:
        line("索引可用", False, f"缺依赖 {exc.name!r}")
        print("         装依赖要用项目里的解释器（直接 pip 会装到系统的 python 里）：")
        print("         .venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        idx = None
    except Exception as exc:
        line("索引可用", False, repr(exc))
        idx = None

    if idx:
        # 缓存写不进去 = 每次启动都要重建一遍（约 2 秒）。以前只往日志打一行
        # 「不影响使用」，--check 照样全绿，用户永远不知道为什么每次双击都慢（第 89 轮）。
        from t_ime import pinyin as _pinyin_mod

        if _pinyin_mod.LAST_CACHE_ERROR:
            line(
                "索引缓存",
                False,
                f"{cache_name}  写不进去：{_pinyin_mod.LAST_CACHE_ERROR} —— "
                f"每次启动都要重建索引（这次 {time.time() - t0:.1f} 秒），"
                f"把 pinyin.dict_cache 指到可写的位置就能省下这段时间",
            )
        from t_ime.pinyin import PinyinEngine
        eng = PinyinEngine(idx, beam_width=int(cfg["pinyin"]["beam_width"]),
                           max_candidates=int(cfg["pinyin"]["max_candidates"]))
        for probe, want in (("nihao", "你好"), ("beijing", "北京"), ("zhongguo", "中国"),
                            ("woshi", "我是"), ("xiexie", "谢谢")):
            got = eng.candidates(probe)
            line(f"候选 {probe}", bool(got) and got[0] == want, f"-> {got[:3]}")
    elif idx is not None:
        print("  [info] 候选词测试                     跳过：索引是空的（0 键），没有词可查 —— 原因见上面那条")
    else:
        print("  [info] 候选词测试                     跳过：索引没建起来 —— 原因见上面那条")

    print()
    print("== 自学习权重 ==")
    # output.boost_file 指到还不存在的子目录时，以前实例每个心跳写失败一次（日志被灌）
    # 而 --check 一个字都不说，面板永远挂着「最多 5 秒后落盘」（第 81 轮）。
    # 检查本身不许造文件：目录不在就只说「实例会自己建」，真要试写也是写完就删。
    _bf = str(cfg["output"].get("boost_file", "user_boosts.json"))
    _bfp = Path(_bf) if Path(_bf).is_absolute() else root_dir / _bf
    if _bfp.exists():
        # 「文件存在」不等于「里面的词生效」：坏 JSON / 不是字典时实例会整份忽略
        # （并把它改名留证），而这里以前照样打 [ok] 已存在 —— 自检说没事，自学习却没了（第 87 轮）。
        try:
            _bdata = json.loads(_bfp.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            line("自学习权重", False,
                 f"{_bfp}  读不了：{type(exc).__name__}: {exc} —— 里面的词不会生效；"
                 f"实例会把它留成 .broken 备份，然后从零开始")
        else:
            if isinstance(_bdata, dict):
                line("自学习权重", True, f"{_bfp}  {len(_bdata)} 个词")
            else:
                line("自学习权重", False,
                     f"{_bfp}  内容不是字典（拿到 {type(_bdata).__name__}）—— 实例会整份忽略它，"
                     f"自学习从零开始")
    elif not _bfp.parent.exists():
        print(f"  [info] {'自学习权重':<28} {_bfp}  目录还不存在（实例第一次落盘时自己建）")
    else:
        _probe = _bfp.parent / f".{_bfp.name}.check-{time.time_ns()}.tmp"
        try:
            _probe.write_text("x", encoding="utf-8")
            _probe.unlink()
        except Exception as exc:
            line("自学习权重", False,
                 f"{_bfp}  写不进去：{exc} —— 自学习会永远落不了盘")
        else:
            line("自学习权重", True, f"{_bfp}  目录可写，文件还没生成")

    print()
    print("== 翻译引擎 ==")
    tr = Translator(cfg, verbose=False)
    print(f"  [info] {tr.describe()}")
    if not getattr(tr, "enabled", True):
        # translate.enabled=false 是用户自己关的，往返测试跑不出东西来；
        # 以前照样执行、拿到 []，然后判 FAIL +「连失败原因都没记下来，看日志」——
        # 一句合法配置被诊断成故障，还让人去翻一份根本没写错的日志（第 78 轮）。
        print("  [info] 翻译往返                       跳过：translate.enabled=false（翻译是你关的，不是坏了）")
        print("         想用翻译就把 config.json 里的 translate.enabled 改回 true。")
    else:
        if tr.is_mock:
            why = getattr(tr, "mock_reason", "") or "没配 API key"
            print(f"  [warn] 翻译走离线词表：{why}。")
            if "API key" in why:
                print("         设置环境变量 DEEPSEEK_API_KEY 或在 config.json 里填 api.api_key 后重试。")
            else:
                print("         想联网翻译就把 api.provider 改回 \"deepseek\"（并配好 api.api_key）。")
        out = tr.translate_many("你好", ZH2EN, n=3)
        detail = f"你好 -> {out}"
        if out:
            line("翻译往返", True, detail)
        else:
            # 第 117 轮：以前不管什么原因失败，都是一行 FAIL + 原始异常字符串。
            # 「连不上网」和「key 写错了」长得一模一样，可修法完全相反：
            # 前者去检查网络，后者去改配置。按种类分开说。
            why = str(tr.stats.get("last_error") or "")
            kind = tr.error_kind
            _base = str((cfg.get("api") or {}).get("base_url") or "")
            try:
                _host = urllib.parse.urlsplit(_base).netloc or _base
            except Exception:
                _host = _base
            if kind == "network":
                warn("翻译往返",
                     f"你好 -> []（连不上 {_host or 'api.base_url'}：{why[:120]}）\n"
                     "         —— 这台机器现在没有网络，或者防火墙 / 代理拦住了；不是配置写错。\n"
                     "            翻译这会儿用不了，联网后再跑一次 --check 就能验。")
            elif kind == "http":
                line("翻译往返", False,
                     f"你好 -> []（HTTP {tr.error_status}）—— 连上了，服务器拒绝了：\n"
                     f"         {why[:160]}\n"
                     "         多半是 API key / 额度 / 模型名的问题，按上面的原文改 config.json。")
            elif kind == "parse":
                line("翻译往返", False,
                     f"你好 -> []（服务器回了 200，但内容不是 JSON：{why[:120]}）\n"
                     "         多半是 api.base_url / api.path 指到了网页或别的东西，"
                     "不是 API 接口 —— 对着 https://api.deepseek.com + /chat/completions 核一下。")
            else:
                detail += f"（{why[:160]}）" if why else "（连失败原因都没记下来，看日志）"
                line("翻译往返", False, detail)


    # 默认上屏语言和翻译开关是一对：译文行只能靠 Translator 出词。翻译关着还要
    # 默认上屏译文，等于配了一个永远到不了的模式 —— 每次空格都会退回中文（第 91 轮）。
    if str(cfg["output"].get("default_language", "zh")).lower() == "en" and not getattr(tr, "enabled", True):
        line("上屏语言", False,
             "译文 —— 但翻译关着（translate.enabled=false），每次上屏都会退回中文；"
             "想用译文先打开 translate.enabled，或把 output.default_language 改成 zh")

    print()
    print("== 窗口层 ==")
    # 这一项以前是假的：只建了一个空白的 `tk.Tk()`，然后无条件打一行
    # 「候选窗可构造 (未显示)」。可 App 启动时建的是 `Overlay(...)`
    # （t_ime/app.py:123），窗口层真坏掉时 `--check` 照样全绿，
    # 而程序连候选窗都起不来 —— 「自检说通过、程序起不来」是最容易把
    # 人带偏的一种失败，所以这里必须真的把候选窗建一遍（不显示）。
    try:
        from tkinter import font as tkfont

        from t_ime.overlay import Overlay
        ov = Overlay(cfg, verbose=False)
    except Exception as exc:
        line("候选窗构造", False,
             f"{exc!r} —— 候选窗起不来，打字时你看不到候选；这里就是你真实的失败原因")
        ov = None
    if ov is not None:
        try:
            root = ov.root
            line("tkinter", True, f"Tk {root.tk.call('info', 'patchlevel')}")
            want = cfg["overlay"].get("font_family", "Microsoft YaHei UI")
            try:
                fams = set(tkfont.families(root))
            except Exception:
                fams = set()
            if want in fams:
                print(f"  [info] {'字体 ' + repr(want):<28} 已找到")
            else:
                print(f"  [info] {'字体 ' + repr(want):<28} 没找到，候选窗会用回退字体 {ov._fam!r}")
            w, h = root.winfo_reqwidth(), root.winfo_reqheight()
            line("候选窗构造", w > 0 and h > 0,
                 f"{w}x{h} 像素 @{ov.scale:.2f}x（不显示，真打字时贴着光标出现）")
            # 第 99 轮：overlay.min_width 是下限、overlay.max_width 是上限，两个值
            # 在 _INT_SPECS 里各自合法，但可以配成「下限 > 上限」。真机实测
            # min_width=1200 / max_width=200 时窗口只有 300px（@1.5x），候选词被裁掉
            # 一半，而这里原来只报 reqwidth、一个字都不说 —— 配置自相矛盾必须说出来。
            min_w = int(cfg["overlay"].get("min_width", 520))
            max_w = int(cfg["overlay"].get("max_width", 900))
            if min_w > max_w:
                line("窗口宽度", False,
                     f"min_width {min_w} > max_width {max_w} —— 两个值自相矛盾；"
                     f"我按下限 {min_w} 算宽度（否则窗口会比你要的最小宽度还窄、候选被裁掉），"
                     f"把 max_width 调到 ≥ {min_w}，或者把 min_width 调小")
            else:
                print(f"  [info] {'窗口宽度':<28} min {min_w} / max {max_w}（换屏时按那块屏的缩放比再算一次）")
        except Exception as exc:
            line("候选窗构造", False, repr(exc))
        finally:
            try:
                ov.destroy()
            except Exception:
                pass

    print()
    print("  " + ("全部通过 ✅" if ok else "有项目未通过 ❌"))
    print()
    return 0 if ok else 1


def run_tray_check(cfg: dict, root_dir: Path, W) -> int:
    """托盘图标装不上时，把「为什么」查清楚。

    这条命令存在的理由：`Shell_NotifyIconW` 失败时只有一个干巴巴的错误码，而可能的
    原因散落在好几个地方（进程完整性级别、explorer 是否在跑、窗口站/桌面、shell 是否
    响应）。把这几项一次性打出来，就不用靠猜了。报告同时写一份到 ``tray-check.txt``，
    方便直接贴给别人看。
    """
    import ctypes
    import os

    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    def item(label: str, value: object) -> None:
        say(f"  {label:<26} {value}")

    say()
    say("== 托盘图标诊断 ==")

    # 第 103 轮：先问一句「这个功能是不是用户自己关的」。output.tray=false 时程序
    # 启动时根本不会去装图标，可这条诊断以前照样试一次 NIM_ADD，然后在 Low 完整性
    # 会话里打出 ACCESS_DENIED，再下结论「这不是程序的问题」 —— 用户明明是自己关的，
    # 却被引去查 UIPI。体检（第 82 轮）修过同一个坑，这条命令漏了。
    tray_on = bool(cfg["output"].get("tray", True))

    def write_report() -> None:
        report = root_dir / "tray-check.txt"
        try:
            report.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print()
            print(f"  报告已写入 {report}")
        except Exception as exc:
            print(f"  （报告写不出来：{exc!r}）")
        print()

    say()
    say("[0] 配置")
    item("output.tray",
         "true（启动时会装托盘图标）" if tray_on
         else "false —— 托盘图标是你自己在配置里关掉的")

    if not tray_on:
        say()
        say("[1]–[3] 跳过：output.tray=false，程序不会去装图标，这些项不适用")
        say()
        say("[4] 结论")
        say("  托盘图标是你自己在配置里关掉的（output.tray=false），所以「右下角没有图标」")
        say("  在这里不是故障，也不该用完整性级别 / UIPI 来解释。想用托盘就把")
        say("  output.tray 改成 true，再启动一次。")
        write_report()
        return 0

    say()
    say("[1] 进程身份")
    rid, name = W.integrity_level()
    item("完整性级别", f"0x{rid:04X}  {name}")
    # 第 120 轮：和体检一样，这行是「跑这条检查的进程」，不是常驻实例的进程号。
    item("诊断进程 pid", f"{os.getpid()}（跑这条检查的进程，不是输入法实例）")
    try:
        item("进程名", Path(sys.executable).name)
    except Exception:
        pass
    try:
        ws = ctypes.create_unicode_buffer(256)
        need = ctypes.c_ulong()
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.GetProcessWindowStation.restype = ctypes.c_void_p
        u32.GetUserObjectInformationW(ctypes.c_void_p(u32.GetProcessWindowStation()),
                                      2, ws, 512, ctypes.byref(need))
        item("窗口站", ws.value or "(拿不到)")
        dt = ctypes.create_unicode_buffer(256)
        u32.GetThreadDesktop.restype = ctypes.c_void_p
        u32.GetUserObjectInformationW(ctypes.c_void_p(u32.GetThreadDesktop(
            ctypes.WinDLL("kernel32").GetCurrentThreadId())), 2, dt, 512, ctypes.byref(need))
        item("桌面", dt.value or "(拿不到)")
    except Exception as exc:
        item("窗口站/桌面", f"查询失败 {exc!r}")

    say()
    say("[2] shell（explorer）状态")
    tray = W.user32.FindWindowW("Shell_TrayWnd", None)
    item("Shell_TrayWnd", tray or "(不存在 → shell 没起来)")
    if tray:
        pid = ctypes.c_ulong()
        W.user32.GetWindowThreadProcessId(tray, ctypes.byref(pid))
        item("  所属进程 pid", pid.value)
        item("  跨进程消息能否送达",
             bool(W.user32.SendMessageTimeoutW(tray, 0, 0, 0, 0x0002, 800, None)))
        item("  进程名", W.process_name(pid.value) or "(读不到)")
    prog = W.user32.FindWindowW("Progman", None)
    item("Progman", prog or "(不存在)")

    say()
    say("[3] 实际试一次 NIM_ADD")
    tray_ok = False
    started = False
    thread_up = False
    try:
        from t_ime.tray import TrayIcon, _default_icon_path

        icon = _default_icon_path()
        item("图标文件", f"{icon}  {'存在' if icon.exists() else '缺失'}")
        t = TrayIcon("translate-ime 诊断", icon if icon.exists() else None,
                     __import__("queue").Queue(), verbose=False)
        # 第 74 轮：以前这里把「start() 返回」写成固定的 "True"，而这句话在
        # 托盘线程根本没起来的时候就是假话（用户会照着它去查 NIM_ADD，方向全错）。
        # start() 现在如实返回「图标到底装上没有」，这里也只报实测值。
        started = t.start()
        time.sleep(0.6)
        tray_ok = bool(t.tray_added)
        thread_up = bool(t.running)
        item("启动托盘线程", "已启动" if thread_up else "没起来（见 last_error）")
        item("start() 返回", "True（图标真的装上了）" if started else "False（图标没装上）")
        item("图标真的装上了", "是" if tray_ok else "否")
        item("last_error", t.last_error or "(无)")
        item("隐藏窗口 hwnd", hex(t.hwnd) if t.hwnd else "0")
        t.stop()
    except Exception as exc:
        item("试装失败", repr(exc))

    say()
    say("[4] 结论")
    if not thread_up:
        # 托盘线程都没起来，NIM_ADD 压根没机会执行 —— 这时去怪完整性级别 / UIPI
        # 是在指错方向，用户会照着改一个不存在的毛病。
        say("  托盘线程没能起来（不是 NIM_ADD 被拒，也不是完整性级别的问题）。")
        say("  原因见上面 [3] 的 last_error；这条建议原样发出来，我按它继续查。")
        verdict = 4
    elif tray_ok:
        say("  托盘图标可以装。如果右下角还是看不到，先点一下托盘区的「^」展开隐藏图标。")
        verdict = 0
    elif rid >= 0 and rid < 0x2000:
        say(f"  本进程完整性级别是 {name}，低于 explorer 的 Medium。")
        say("  Windows 的 UIPI 会拦下低完整性进程发给托盘窗口的消息，所以图标装不上。")
        say("  这不是程序的问题 —— 用正常方式启动（双击 run.bat，或从你平时用的终端里跑）即可。")
        verdict = 2
    elif not tray:
        say("  系统里根本没有 Shell_TrayWnd，shell 没起来。重启 explorer 后重试。")
        verdict = 3
    else:
        say("  完整性级别正常、shell 也在跑，但 NIM_ADD 仍然失败 —— 请把上面 [3] 的")
        say("  last_error 原文发出来，我按那个错误码继续查。")
        verdict = 4

    write_report()
    return verdict


def run_doctor(cfg: dict, root_dir: Path, W, *, seconds: float = 0.0,
               probe_tray: bool = True, probe_hook: bool = True,
               pipeline: bool = True, log_path: Path | None = None,
               cfg_path: Path | None = None,
               cfg_explicit: bool = False) -> int:
    """把「它为什么不动」一次查到底，并写出一份可以直接贴出去的报告。

    和 ``--check`` 的分工：``--check`` 查的是**代码和依赖对不对**（纯 CPU、换台机器
    结果一样）；``--doctor`` 查的是**在你这台机器、这个进程、这个权限下，这条链路能不能
    真的跑起来**。所以要实测：真装一次托盘图标、真装一次键盘钩子、真把候选窗显示出来
    看它落在哪块显示器上、真跑一遍「注入按键 → 出候选 → 上屏」。

    存在的理由：这个项目最难查的两个症状（「托盘图标没出现」和「打字没反应、候选窗
    根本不出现」）都**无法在别人的机器上复现** —— 它们取决于完整性级别、shell 状态、
    多显示器布局这些东西。把每一项都变成一行实测结论，就不用隔着屏幕猜了。

    返回 0 表示没有 ``[FAIL]`` 项（``[warn]`` 不算失败，那是环境限制）。
    """
    import ctypes
    import os
    import subprocess

    lines: list[str] = []
    bad = 0
    warns = 0
    # 托盘图标是用户在配置里关掉的（output.tray=false），还是想装却没装上？
    # 这两件事后面每一段都得分开说：不然诊断会去怪 UIPI / 完整性级别，而用户
    # 本来就把这个功能关了（第 82 轮）。
    tray_on = bool(cfg["output"].get("tray", True))

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    def item(label: str, value: object, level: str = "ok") -> None:
        nonlocal bad, warns
        if level == "fail":
            bad += 1
        elif level == "warn":
            warns += 1
        mark = {"ok": "ok ", "warn": "warn", "fail": "FAIL", "info": "info"}[level]
        say(f"  [{mark}] {label:<26} {value}")

    say()
    say("== translate-ime 体检 ==")
    say(f"   {time.strftime('%Y-%m-%d %H:%M:%S')}   报告会写到 translate-ime-doctor.txt")

    # ---------------- [0] 正在跑的那个实例 ----------------
    # 放在最前面：多数问题不是「程序坏了」而是「那个常驻实例其实是这样/根本没在跑」。
    # 这段读的是**运行中实例自己写下的状态文件**，不是本进程的现场。
    say()
    say("[0] 正在运行的实例")
    from t_ime import daemon

    st = daemon.read_status(root_dir)
    if not st:
        item("常驻实例", "没有在跑（状态文件不存在）", "info")
    else:
        stale = daemon.status_is_stale(root_dir)
        pid = int(st.get("pid") or 0)
        # 状态文件可能是**上一个实例**的残留：被强杀时它来不及清理自己。
        # 这时候把里面的字段当成活的实例来报，就是在骗用户（和骗我自己）。
        alive = bool(pid) and bool(W.process_name(pid))
        if not alive:
            item("常驻实例", f"状态文件是残留的：进程 {pid or '?'} 已经不在了", "warn")
            item("启动于", time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(st.get("started") or 0)))
            say("        上次多半是被强杀（taskkill /F、崩溃、注销）而不是正常退出，")
            say("        状态文件和命令收件箱都没来得及清理。现在双击 run.bat 即可重新起来：")
            say("        启动时会自动丢掉残留的命令（不会被上一条 quit 带去自杀），")
            say("        并覆盖这个残留的状态文件。")
        else:
            item("进程号", pid, "ok" if not stale else "warn")
            age = daemon.status_age(root_dir)
            if age is None:
                age_txt = "读不到更新时间（文件在，但没有 mtime）"
            elif stale:
                age_txt = (f"{age:.0f} 秒没更新了，可能卡死或被强杀"
                           f"（心跳是 {daemon.STATUS_HEARTBEAT_SECONDS:.0f} 秒一次，"
                           f"超过 {daemon.STATUS_STALE_SECONDS:.0f} 秒没来才算）")
            else:
                age_txt = f"{age:.0f} 秒前更新过（心跳正常）"
            item("状态文件", age_txt, "ok" if not stale else "warn")
            item("输入法", "已启用" if st.get("enabled") else "已挂起")
            # 语言栏里找不到本工具是设计，不是故障 —— 这一行要主动说出来，
            # 否则用户的结论就是「没装好，还是微软拼音」。
            item("登记方式", "没登记成系统输入法 —— 语言栏 / Win+Space 里不会出现，启动即接管字母",
                 "info")
            _lays = W.installed_input_layouts() if hasattr(W, "installed_input_layouts") else []
            item("系统输入法", "、".join(n for _, n in _lays) if _lays else "读不出来（不影响使用）",
                 "info")
            item("输入模式", "英文直通" if st.get("direct") else "拼音")
            inst_cfg = st.get("config")
            if not inst_cfg:
                item("配置文件", "（这个实例没报，可能是旧版本）", "info")
            else:
                try:
                    _inst_exists = Path(str(inst_cfg)).exists()
                except OSError:
                    _inst_exists = False
                # 实例照着一份**不存在**的配置在跑 = 它其实在用内置默认值。
                # 打 [ok] 等于替这件事打包票（第 100 轮真机：`--config nope.json`
                # 起来的实例，这里一行 [ok] 配置文件 …nope.json）。
                item("配置文件",
                     str(inst_cfg) if _inst_exists
                     else f"{inst_cfg}  不存在（这个实例其实在用内置默认值）",
                     "ok" if _inst_exists else "warn")
            if st.get("tray_enabled") is False:
                item("托盘图标", "已关闭（output.tray=false，这个实例没往托盘里放图标）",
                     "info")
            else:
                item("托盘图标", "已装上" if st.get("tray") else "没装上（不影响打字）",
                     "ok" if st.get("tray") else "warn")
            hk = st.get("hook_info") or {}
            effective = _effective_keys(hk)
            hook_level = "ok"
            if not st.get("hook") or hk.get("error") or effective <= 0:
                hook_level = "warn"      # 刚起完还没人敲过键也会是 0，不能算 FAIL
            item("键盘钩子", _describe_hook(st, hk), hook_level)
            if st.get("hook") and not hk.get("error") and effective <= 0:
                say("        一个有效按键都没进钩子（键盘硬件来的、别的程序转发的都算过）：")
                say("        在旁边那个窗口里敲几个字母，再回头双击一次 run.bat 看这一行")
                say("        有没有变成「收到过 N 个有效按键」。")
            ov = st.get("overlay") or {}
            shows = int(ov.get("shows") or 0)
            # 提示条（启动引导、挂起/切模式的气泡）也走同一条显示路径、也 +1，
            # 所以「shows > 0」完全不能证明候选窗能用；第 86 轮之前在真机上静置
            # 不敲键就能得到 shows=2，而这个分支会把「候选窗根本不出现」判成正常。
            cand_raw = ov.get("cand_shows")
            cand_shows = shows if cand_raw is None else int(cand_raw or 0)
            toast_only = shows - cand_shows
            rect = ov.get("rect")
            tips = f"（窗口显示过 {shows} 次，其中 {toast_only} 条是提示，不是候选窗）" \
                if toast_only > 0 else ""
            if ov.get("error"):
                item("候选窗", f"显示失败：{ov['error']}", "fail")
            elif cand_shows <= 0 and not st.get("enabled"):
                # 第 58 轮：这几条原本一律判 FAIL。可是挂起 / 英文直通 / 还没敲过键
                # 这三种情况本来就不该弹候选窗 —— 把正常状态判成故障，比漏报更坏：
                # 用户会照着「候选窗根本不出现」去修一个没坏的东西。
                item("候选窗", "从未显示过 —— 但输入法现在是挂起的，挂起时字母直通、"
                              "本来就不弹候选" + tips, "info")
            elif cand_shows <= 0 and st.get("direct"):
                item("候选窗", "从未显示过 —— 但现在是英文直通，字母原样透传、"
                              "本来就不弹候选" + tips, "info")
            elif cand_shows <= 0 and (not st.get("hook") or hk.get("error")):
                # 钩子没装上时，「敲几个拼音字母再看这一行」是一句**不可能生效**的建议：
                # 按键根本进不了程序，敲多久都不会有候选窗（第 107 轮）。
                why = str(hk.get("error") or "")
                if why:
                    item("候选窗", f"不会出现 —— 键盘钩子装不上（{why}），按键进不了程序；"
                                   "先把钩子的问题解决（多半是安全软件拦了全局钩子），"
                                   "这一行才有意义", "fail")
                elif hk.get("disabled_by_flag"):
                    item("候选窗", "不会出现 —— 这次是用 --no-hook 起的，键盘输入没接过来"
                                   "（去掉 --no-hook 重启，或者 run.bat --demo 预览）",
                         "info")
                else:
                    item("候选窗", "不会出现 —— 这个实例没装键盘钩子，打字不会有反应",
                         "warn")
            elif cand_shows <= 0 and effective <= 0:
                item("候选窗", "还没显示过 —— 还没收到过任何按键，敲几个拼音字母再看这一行"
                     + tips, "warn")
            elif cand_shows <= 0:
                item("候选窗", f"启动至今一次都没弹过，而你确实敲过键（有效按键 {effective} 个）"
                              f"{tips} —— 这是「候选窗根本不出现」的直接证据", "fail")
            else:
                where = ("在显示器上" if ov.get("on_monitor")
                         else "不在任何显示器上（跑到屏幕外了）")
                pos = f"{rect[2]}x{rect[3]}+{rect[0]}+{rect[1]}" if rect else "位置未知"
                item("候选窗", f"显示过 {cand_shows} 次 · 最近 {pos}（{where}）{tips}",
                     "ok" if ov.get("on_monitor") else "fail")
            errs = int(st.get("tick_errors") or 0)
            if errs:
                item("主循环异常", f"累计 {errs} 次（程序带伤在跑，日志里有 traceback）", "fail")
            cfails = int(st.get("commit_failures") or 0)
            if cfails:
                why = st.get("last_commit_error") or "原因没记下来"
                item("上屏失败", f"累计 {cfails} 次 —— 最近一次：{why}", "warn")
                say("        这条不是猜测：文字确实没打进那个窗口（多半是往管理员权限的")
                say("        窗口里打字，Windows 不允许低权限进程往里送按键）。程序那一刻")
                say("        已经把文字放进剪贴板，Ctrl+V 就能救回来。")
            item("启动于", time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(st.get("started") or 0)))
            log = st.get("log")
            if log:
                item("日志", f"{log}  （体检不读它，出事时整份贴出来）", "info")

    # ---------------- [1] 进程身份 ----------------
    say()
    say("[1] 进程身份与环境")
    rid, rname = W.integrity_level()
    item("完整性级别", f"0x{rid:04X}  {rname}")
    low = rid < 0x2000
    if low:
        if tray_on:
            item("低完整性的后果", "托盘图标会被 UIPI 拦掉；向别的程序注入按键也可能被拒", "warn")
        else:
            item("低完整性的后果",
                 "向别的程序注入按键可能被拒（托盘图标你已经关了，UIPI 那条不适用）", "warn")
    item("Python", sys.version.split()[0])
    item("解释器", sys.executable)
    # 第 120 轮：这里以前只写 `pid`，而 `[0]` 里写的是 `进程号` —— 同一个词、两个
    # 不同的进程。[0] 那个是常驻实例（不变），这一行是**正在跑体检的这个进程**
    # （每次跑都换），用户拿两份报告一对比就会以为输入法在反复重启。
    item("诊断进程 pid",
         f"{os.getpid()}（跑这次体检的进程，每次都换；[0] 的「进程号」才是常驻实例）")
    try:
        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        item("是否提权", "是（管理员）" if elevated else "否")
        if elevated:
            item("提权的后果", "注入会被 UIPI 拦；建议用普通权限跑", "warn")
    except Exception as exc:
        item("是否提权", f"查询失败 {exc!r}", "info")
    sw, sh = W.screen_size()
    item("主显示器", f"{sw}x{sh}    共 {W.monitor_count()} 个显示器")
    aware = W.dpi_awareness()
    item("DPI 感知", aware, "info" if "UNAWARE" not in aware else "warn")
    item("缩放比", f"{W.dpi_scale():.2f}x"
         + ("（窗口按物理像素绘制）" if "UNAWARE" not in aware
            else "（⚠ 进程不感知 DPI，候选窗会被系统拉伸、字发虚）"))

    # ---------------- 开机自启（objective ①：可选开机自启）----------------
    auto = daemon.autostart_status()
    auto_txt = {"registry": "已开启（注册表）",
                "startup-folder": "已开启（启动文件夹）"}.get(
                    auto, "未开启（可选：run.bat --autostart）")
    problem = daemon.autostart_problem(root_dir) if auto != "none" else ""
    if problem:
        item("开机自启", f"{auto_txt} —— 但它不会生效：{problem}", "warn")
    elif auto == "none":
        item("开机自启", auto_txt, "info")
    else:
        item("开机自启", auto_txt)

    # ---------------- [2] shell / 托盘 ----------------
    say()
    say("[2] shell 与托盘图标")
    tray_hwnd = W.user32.FindWindowW("Shell_TrayWnd", None)
    item("Shell_TrayWnd", tray_hwnd or "不存在（explorer 没在跑）", "ok" if tray_hwnd else "warn")
    if tray_hwnd:
        pid = ctypes.c_ulong()
        W.user32.GetWindowThreadProcessId(tray_hwnd, ctypes.byref(pid))
        item("  shell 进程", f"{pid.value}  {W.process_name(pid.value) or '读不到'}")

    tray_ok = None
    if not tray_on:
        # 用户自己关掉的托盘不是「装不上」，这一项从一开始就不该试：真去 NIM_ADD
        # 一次再报「失败、UIPI 拦的」，等于把一个不存在的故障写成结论（第 82 轮）。
        say("  [info] output.tray=false —— 托盘图标是你自己关的，这一项跳过（不是装不上）")
    elif not probe_tray:
        say("  [info] （--doctor-quick：跳过真装托盘这一项）")
    else:
        try:
            import queue as _queue

            from t_ime.tray import TrayIcon, _default_icon_path

            icon = _default_icon_path()
            item("图标文件", f"{icon}  {'存在' if icon.exists() else '缺失'}",
                 "ok" if icon.exists() else "info")
            t = TrayIcon("translate-ime 体检", icon if icon.exists() else None,
                         _queue.Queue(), verbose=False)
            t.start()
            time.sleep(0.6)
            tray_ok = bool(t.tray_added)
            if tray_ok:
                item("试装托盘图标", "成功（右下角看不到就点一下 ^ 展开隐藏图标）")
            elif low:
                item("试装托盘图标", "失败", "warn")
                item("  原因", t.last_error or "(没记下原因)", "info")
                say("        这个进程的完整性级别低于 explorer，UIPI 会拦下 NIM_ADD ——")
                say("        不是程序的问题。用正常方式启动（双击 run.bat）就能装上。")
            elif not tray_hwnd:
                item("试装托盘图标", "失败（shell 没起来）", "warn")
            else:
                item("试装托盘图标", "失败", "fail")
                item("  原因", t.last_error or "(没记下原因)", "info")
            item("  隐藏窗口 hwnd", hex(t.hwnd) if t.hwnd else "0", "info")
            t.stop()
        except Exception as exc:
            item("试装托盘图标", f"试装过程抛异常：{exc!r}", "fail")

    # ---------------- [3] 配置 / 词库 ----------------
    say()
    say("[3] 配置、词库与翻译")
    cfgp = Path(cfg_path).resolve() if cfg_path else Path(root_dir) / "config.json"
    # 「你点名要的这份读不到」和「首次运行、还没有 config.json」是两件事，退出码
    # 也该不同：前者是用户以为在用某份配置、其实一份都没读到（--check 第 83 轮
    # 已经判 FAIL）；不区分的话，同一行命令 --check 说 rc 1、--doctor 说 rc 0。
    if cfgp.exists():
        item(cfgp.name, f"{cfgp}  存在", "ok")
    elif cfg_explicit:
        item(cfgp.name,
             f"{cfgp}  不存在 —— 你指定了这一份，但读不到，下面体检的是内置默认值",
             "fail")
    else:
        item(cfgp.name,
             f"{cfgp}  不存在（首次运行，全部走内置默认值；--init-config 生成一份）",
             "info")
    # 第 65 轮：以前这里写死 root/config.json，于是 `--doctor --config X` 时，
    # 上面刚用 X 起了实例、下面却去分析 root/config.json 的拼写和缺项 ——
    # 甚至会出现「[warn] X 里有 1 个键不认识」紧接着「[ok] 配置拼写 没有不认识的键」
    # 这种自相矛盾。现在描述的就是本次真正加载的那份。
    # 正在跑的实例可能是用 --config 另一份文件起来的（例如「另开一个挂起态的
    # 实例」）。下面这些行讲的全是 cfgp 这份，跟那个实例没关系 —— 不说清楚，
    # 用户就会拿这里的 output.start_enabled / 热键 当成实例的现状（第 60 轮）。
    inst_cfg = (st or {}).get("config") if st else None
    if inst_cfg:
        try:
            inst_cfg_path = Path(str(inst_cfg)).resolve()
        except OSError:
            inst_cfg_path = Path(str(inst_cfg))
        try:
            same_cfg = inst_cfg_path == cfgp.resolve()
        except OSError:
            same_cfg = str(inst_cfg_path) == str(cfgp)
        if not same_cfg:
            item("实例配置", f"{inst_cfg_path} —— 正在跑的实例用的是这一份；"
                            f"[3] 下面这些行描述的是 {cfgp}，不是它", "warn")
    from t_ime import config as _config_mod
    from t_ime.config import missing_keys, read_raw, unknown_keys

    raw_cfg, _raw_path = read_raw(cfgp)
    unk_cfg = unknown_keys(raw_cfg)
    miss_cfg = missing_keys(raw_cfg)
    # 第 116 轮：JSON 写坏时 config.load **不抛异常**，它是「警告一句 + 整份改用
    # 内置默认值」。以前这里没有任何一项看这个分支，于是 [3] 拿默认值去做拼写 /
    # 取值校验，报出「没有 DEFAULTS 不认识的键」「没有写错的值」，体检整体全绿；
    # 而同一份配置 `--check` 是 [FAIL] 配置能读 —— 两份报告互相打脸（第 100 轮
    # 修的是「写错的选项」那半，这是「读不懂」这半）。
    _load_err = str(getattr(_config_mod, "LAST_LOAD_ERROR", "") or "")
    if cfgp.exists() and not _load_err:
        # 自己再读一遍：不能假设「上游加载的一定是这份文件」。这里只解析、不
        # 调 config.load —— 那个函数会把警告再打印一遍，[3] 里就会出现两次同一句。
        try:
            json.loads(cfgp.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            _load_err = f"读取 {cfgp} 失败：{type(exc).__name__}: {exc}（整份配置被忽略、改用内置默认值）"
    if _load_err:
        item("配置能读", f"读不懂：{_load_err}", "fail")
        say("        整份配置被忽略、现在跑的是内置默认值 —— 下面「没写进配置的选项」")
        say("        描述的是默认值，不是你的文件。先把它修成合法 JSON，")
        say("        或者删掉它、跑一次 `--init-config` 重新生成。")
    if not cfgp.exists():
        pass
    elif _load_err:
        item("配置拼写", "没检查（文件读不懂，先修好它）", "info")
    elif unk_cfg:
        item("配置拼写", f"{len(unk_cfg)} 个键 DEFAULTS 不认识：{', '.join(unk_cfg[:5])}", "warn")
        say("        拼错的键会被静静忽略、永远不生效 —— 跑 `--show-config` 看详情。")
    else:
        item("配置拼写", "没有 DEFAULTS 不认识的键", "ok")
    if cfgp.exists():
        # 值写错（整段类型不对 → 退回默认值）在 --check 里是 FAIL（第 83 轮），
        # 体检这边以前只报「配置拼写」—— 于是「你的 quit 热键写成了数字、整段
        # 热键退回默认」这种事，--check 红、--doctor 绿，两份报告互相打脸。
        # 注意：类型校验必须看**合并默认值之后**的配置。直接拿文件里的原始 dict
        # 去 sanitize，没写进文件的键全是 None，会被报成一片「现在是 null」的假错。
        if _load_err:
            item("写错的选项", "没法检查（配置读不懂，上面那行有原因）", "info")
        else:
            _probs = list(_config_mod.LAST_PROBLEMS)
            if _probs:
                item("写错的选项",
                     f"{len(_probs)} 项已按默认值顶上（你写的值没生效）：{_probs[0]}",
                     "fail")
                say("        跑 `--show-config` 能看到现在真正在用的值。")
            else:
                item("写错的选项", "没有写错的值", "ok")
    if cfgp.exists() and miss_cfg:
        item("没写进配置的选项", f"{len(miss_cfg)} 个（正在用默认值生效）", "info")
    item("output.start_enabled", cfg["output"].get("start_enabled"),
         "ok" if cfg["output"].get("start_enabled") else "warn")
    if not cfg["output"].get("start_enabled"):
        say("        启动时输入法是**挂起**的，打拼音不会有任何反应。")
    item("output.default_language", cfg["output"].get("default_language"))
    item("translate.enabled", cfg["translate"].get("enabled"))
    tr_on = bool(cfg["translate"].get("enabled", True))
    has_key = bool(cfg["api"].get("api_key") or os.environ.get("DEEPSEEK_API_KEY"))
    provider = str(cfg["api"].get("provider") or "deepseek").strip().lower()
    item("api.provider", provider)
    if not tr_on:
        # 翻译关着的时候，「没配 key」不是问题、更没「退回离线引擎」这回事（第 78 轮）。
        item("API key", "未配置（翻译已关闭，这项用不上）" if not has_key else "已配置（翻译已关闭，暂时用不上）",
             "info")
    else:
        item("API key", "已配置" if has_key else "未配置（翻译退回离线 mock 引擎）",
             "ok" if has_key else "warn")
    # 「有没有 key」和「会不会真的联网翻译」是两件事：第 55 轮 api.provider 真的
    # 起作用之后，provider=mock 时即使配了 key 也只走离线词表。以前这里只说
    # 「已配置」，用户会以为在线翻译在工作（第 62 轮）。
    if not cfg["translate"].get("enabled", True):
        # 关掉翻译是合法配置，不是故障：以前这里照样执行到下面的 provider 分支
        # （说「离线词表（没配 API key）」），用户会去查一个根本不存在的 key（第 78 轮）。
        item("实际引擎", "翻译已关闭（translate.enabled=false）—— 不联网、也不查离线词表", "info")
        say("        想要翻译就把 config.json 里的 translate.enabled 改回 true。")
    elif provider == "mock":
        item("实际引擎", "离线词表（api.provider=mock，你主动选的离线模式）", "info")
        say("        这一项优先于 API key：配了 key 也不会联网、不会产生费用。")
    elif has_key:
        item("实际引擎", "在线 deepseek")
    else:
        item("实际引擎", "离线词表（没配 API key）", "warn")
    # 逐个动作分诊：能不能解析只是第一关，动作名拼错 / 两个动作撞车同样会让热键
    # 「按了没反应」（第 63 轮），所以这里和 --check 共用一个判据。
    from t_ime.hotkey import check_hotkeys as _check_hotkeys

    for name, spec, problem in _check_hotkeys(cfg["hotkeys"], W.parse_hotkey):
        if not spec.strip():
            item(f"热键 {name}", "（未绑定）", "info")
        elif problem:
            item(f"热键 {name}", f"{spec} -> {problem}", "fail")
        else:
            mods, vk = W.parse_hotkey(spec)
            item(f"热键 {name}", f"{spec} -> mods={sorted(mods)} vk={vk}")

    idx = None
    t0 = time.time()
    try:
        from t_ime.pinyin import PinyinEngine, load_or_build_index

        idx = load_or_build_index(
            Path(root_dir) / cfg["pinyin"]["dict_cache"],
            min_freq=int(cfg["pinyin"]["min_freq"]),
            max_word_len=int(cfg["pinyin"]["max_word_len"]),
            verbose=False,
        )
        if idx:
            item("拼音索引", f"{len(idx):,} 键 / {time.time() - t0:.1f}s")
            # 缓存没落盘时下次启动还得重建（约 2 秒）——以前只打一行「不影响使用」。
            from t_ime import pinyin as _pinyin_mod

            if _pinyin_mod.LAST_CACHE_ERROR:
                item("索引缓存",
                     f"{cfg['pinyin']['dict_cache']}  写不进去：{_pinyin_mod.LAST_CACHE_ERROR}"
                     f" —— 每次启动都要重建索引（这次 {time.time() - t0:.1f}s），"
                     f"把 pinyin.dict_cache 指到可写的位置就能省下这段时间",
                     "fail")
        else:
            # 空索引不是「装好了」，是「查不到任何词」——以前照样打绿。
            item("拼音索引",
                 f"{len(idx):,} 键 —— 索引是空的，打字不会有候选；"
                 f"pinyin.min_freq 现在是 {int(cfg['pinyin']['min_freq'])}，调小它再试",
                 "fail")
    except Exception as exc:
        item("拼音索引", f"加载失败：{exc!r}", "fail")
    if idx:
        try:
            eng = PinyinEngine(idx, beam_width=int(cfg["pinyin"]["beam_width"]),
                               max_candidates=int(cfg["pinyin"]["max_candidates"]))
            got = eng.candidates("nihao")
            item("候选 nihao", f"-> {got[:3]}", "ok" if got and got[0] == "你好" else "fail")
        except Exception as exc:
            item("候选 nihao", f"取候选失败：{exc!r}", "fail")
    elif idx is not None:
        item("候选 nihao", "跳过：索引是空的（0 键）—— 见上面那条「拼音索引」", "info")
    else:
        item("候选 nihao", "跳过：索引没建起来 —— 见上面那条「拼音索引」", "info")

    # ---------------- [4] 日志 ----------------
    say()
    say("[4] 日志文件")
    from t_ime import daemon

    log = daemon.resolve_log_path(Path(root_dir), None, cfg) if log_path is None else Path(log_path)
    note = ""
    # 有实例在跑、而且它写的不是这个默认路径（例如启动时带了 --log）时，
    # 说实话告诉用户日志实际在哪 —— 不然这份报告会把人指到错误的文件上。
    if daemon.status_path(Path(root_dir)).exists():
        try:
            st = daemon.read_status(Path(root_dir)) or {}
            real = st.get("log")
            if real and Path(str(real)) != log:
                log = Path(str(real))
                note = "（这是正在跑的那个实例实际写的位置）"
        except Exception:
            pass
    # 体检只做检查，不该顺手把被检查的东西造出来。以前这里 `open("a")`（还可能
    # 先 `mkdir`），于是第一次跑 `--doctor` 就在项目根留下一个空的
    # `translate-ime.log` —— 用户会以为实例已经跑过、只是没写日志（第 66 轮）。
    # 现在：目录不在就说「不在」，日志不在就往同目录写一个临时探针再删掉。
    try:
        if not log.parent.exists():
            item("日志文件", f"{log}  目录还不存在（实例第一次启动时会自动建）{note}", "info")
        elif not log.exists():
            probe = log.parent / f".{log.name}.doctor-{os.getpid()}.tmp"
            try:
                probe.write_text("x", encoding="utf-8")
            finally:
                probe.unlink(missing_ok=True)
            item("日志文件", f"{log}  目录可写，日志还没生成{note}")
        else:
            with log.open("a", encoding="utf-8"):
                pass
            item("日志文件", f"{log}  可写，当前 {log.stat().st_size} B{note}")
    except Exception as exc:
        item("日志文件", f"{log} 写不进去：{exc!r}", "fail")

    # ---------------- [5] 键盘钩子 ----------------
    say()
    say("[5] 全局键盘钩子")
    hook_ok = None
    if not probe_hook:
        say("  [info] （--doctor-quick：跳过真装钩子这一项）")
    else:
        hk = W.KeyboardHook(lambda vk, is_down, injected: False, "t-ime-doctor")
        hook_ok = bool(hk.start(timeout=5.0))
        if hook_ok:
            item("安装钩子", "成功")
        else:
            item("安装钩子", f"失败：{hk.error}", "fail")
            say("        连钩子都装不上，输入法不可能收到你的按键。常见原因：")
            say("        安全软件拦了 WH_KEYBOARD_LL；把本程序加进白名单再试。")
        if hook_ok:
            hk.stop()

    if hook_ok and seconds > 0:
        say()
        say(f"[5b] 真实按键观察（{seconds:.0f} 秒 —— 现在请敲几个字母）")
        seen: list[tuple[int, bool, bool]] = []
        hk2 = W.KeyboardHook(
            lambda vk, is_down, injected: (seen.append((vk, is_down, injected)), False)[1],
            "t-ime-doctor-watch",
        )
        if hk2.start(timeout=5.0):
            real = 0
            injected = 0
            deadline = time.monotonic() + max(1.0, seconds)
            while time.monotonic() < deadline:
                batch = seen[:]
                seen.clear()
                for _vk, _down, inj in batch:
                    if inj:
                        injected += 1
                    else:
                        real += 1
                left = deadline - time.monotonic()
                print(f"\r    剩余 {left:4.1f}s   收到 {real + injected} 个事件（真实 {real}）",
                      end="", flush=True)
                time.sleep(0.15)
            print()
            hk2.stop()
            item("真实按键", f"{real} 个（另有 {injected} 个是别的程序注入的）",
                 "ok" if real else "warn")
            if not real:
                say("        一个真实按键都没收到。往上看 [1]：焦点是不是在提权窗口里，")
                say("        或者键盘是不是走远程桌面 / KVM 通道进来的。")
        else:
            item("真实按键观察", f"第二个钩子装不上：{hk2.error}", "fail")

    # ---------------- [6] 候选窗能不能显示出来 ----------------
    say()
    say("[6] 候选窗能不能真的出现在屏幕上")
    try:
        from t_ime.app import demo_snapshot
        from t_ime.overlay import Overlay

        ov = Overlay(cfg, verbose=False)
        try:
            ov.render(demo_snapshot())
            ov.show()
            ov.root.update()
            hwnd = ov.top_hwnd()
            item("窗口句柄", hex(hwnd) if hwnd else "0", "ok" if hwnd else "fail")
            if hwnd:
                vis = W.is_window_visible(hwnd)
                item("IsWindowVisible", vis, "ok" if vis else "fail")
                mon = W.monitor_of_window(hwnd)
                if mon:
                    item("落在显示器上", f"是（0x{mon:X}）")
                else:
                    item("落在显示器上",
                         "否 —— 窗口在屏幕外，用户根本看不见！", "fail")
                    say("        这就是「候选窗根本不出现」：窗口句柄有效、IsWindowVisible 也是")
                    say("        True，但它落在了任何显示器之外。多显示器和负坐标最容易触发。")
                rect = W.window_rect(hwnd)
                if rect:
                    x, y, w, h = rect
                    # 和这块窗口**自己所在显示器**比，别拿主屏比：多显示器下
                    # 拿主屏量副屏会误判。两个值都是同一坐标系、同一时刻问的。
                    mr = W.monitor_rect(hwnd)
                    if mr:
                        mx, my, mw, mh = mr
                        inside = (mx - w < x < mx + mw) and (my - h < y < my + mh)
                        item("位置", f"{w}x{h}+{x}+{y}  （所在显示器 {mw}x{mh}+{mx}+{my}）",
                             "ok" if inside else "warn")
                        if not inside:
                            say("        窗口有一部分在显示器外 —— 用户可能只看到半截或者根本看不见。")
                    else:
                        item("位置", f"{w}x{h}+{x}+{y}", "info")
        finally:
            try:
                ov.destroy()
            except Exception:
                pass
    except Exception as exc:
        item("候选窗", f"构造 / 显示失败：{exc!r}", "fail")

    # ---------------- [7] 端到端链路 ----------------
    say()
    say("[7] 端到端链路（钩子 → 状态机 → 候选 → 上屏，不需要你敲键）")
    chain = Path(root_dir) / "tests" / "test_hook_chain.py"
    if not pipeline:
        say("  [info] （--doctor-quick：跳过端到端）")
    elif not chain.exists():
        item("端到端", f"跳过：找不到 {chain}", "info")
    else:
        try:
            proc = subprocess.run(
                [sys.executable, str(chain)],
                cwd=str(root_dir), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180,
            )
            out = proc.stdout or ""
            summary = ""
            for ln in out.splitlines():
                if ln.strip().startswith("结果："):
                    summary = ln.strip()
            item("exit code", proc.returncode, "ok" if proc.returncode == 0 else "fail")
            if summary:
                item("摘要", summary, "ok" if proc.returncode == 0 else "fail")
            if proc.returncode != 0:
                for ln in out.splitlines():
                    if "[FAIL]" in ln:
                        say(f"        {ln.strip()}")
                for ln in (proc.stderr or "").splitlines()[-5:]:
                    say(f"        {ln.strip()}")
        except subprocess.TimeoutExpired:
            item("端到端", "超时 180s 没跑完", "fail")
        except Exception as exc:
            item("端到端", f"跑不起来：{exc!r}", "fail")

    # ---------------- [8] 结论 ----------------
    say()
    say("== 结论 ==")
    if bad == 0 and warns == 0:
        say("  全部通过。这套程序在你这台机器上是能正常工作的。")
        verdict = 0
    elif bad == 0:
        say(f"  没有致命问题（{warns} 项环境限制）。")
        if low and tray_on:
            say("  注意完整性级别那一项：这里装不上托盘图标是 UIPI 导致的，正常双击不受影响。")
        elif low:
            # 托盘关着的时候，上面那条 warn 讲的是注入，不是托盘 —— 结论不能再说
            # 「这里装不上托盘图标」，那是用户自己关的（第 82 轮）。
            say("  注意完整性级别那一项：托盘图标是你关着的（output.tray=false），"
                "那条只影响往别的程序注入按键。")
        verdict = 0
    else:
        say(f"  有 {bad} 项 [FAIL]，请看上面标了 FAIL 的行 —— 那才是问题所在。")
        verdict = 1
    say()
    say("  还想更细地查：")
    say("    run.bat --tray-check     # 只查托盘")
    say("    run.bat --hook-check     # 只查键盘钩子（会请你敲 12 秒键盘）")
    say("    run.bat --check          # 只查代码和依赖")
    say("    日志：" + str(log))

    report = Path(root_dir) / "translate-ime-doctor.txt"
    try:
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print()
        print(f"  报告已写入 {report}")
        print("  这一整个文件都可以直接贴给我。")
    except Exception as exc:
        print(f"  （报告写不出来：{exc!r}）")
    print()
    return verdict


def _hook_nothing_text(seconds: float, own: int) -> str:
    """「这 N 秒什么都没收到」这半句必须说清楚：没收到的是**谁**的按键。

    自检按键是自己注入的，它会回来（说明钩子活着），但它不是用户的按键 ——
    以前这里写死「一个按键都没收到」，同一屏下面又说「自检按键回来了」，
    用户只能认为诊断在胡说。所以按 ``own`` 分两种说法，别让一句话盖过另一句话。
    """
    if own > 0:
        return (f"  这 {seconds:.0f} 秒里没收到**真实**按键 —— 只收到 {own} 个自检按键，"
                "那是我自己注入的，只能证明钩子还活着。")
    return f"  这 {seconds:.0f} 秒里**一个按键都没收到**（连自检按键都没回来）。"


def _hook_check_help_text(seconds: float) -> str:
    """``--hook-check`` 请用户敲键时那两行说明。

    第 96 轮：原来承诺「只观察、一个键都不发」，可 ``hook.probe()`` 本身
    就是本程序注入的一个键，同一屏的结论行还会写「我们自己注入的 N 个」——
    同一块屏幕上两句话互相打脸。这里改成明说自己会注入一个。
    """
    return (f"[3] 请在**刚弹出的那个小窗口里**敲几个字母（比如 nihao），{seconds:.0f} 秒\n"
            "    （只观察、不吞你的键；开头会注入一个自检按键 —— 那是本程序自己发的，\n"
            "      只用来确认钩子还活着，不会打进任何程序）")


def run_hook_check(cfg: dict, root_dir: Path, W, seconds: float = 12.0,
                   log_path: Path | None = None) -> int:
    """测「全局键盘钩子到底看不看得见你敲的键」。

    用户报「打字没反应、候选窗根本不出现」时，这一条能一刀切开两种可能：
    钩子根本没收到按键（环境/权限/安全软件的问题），还是收到了但状态机
    没进拼音模式（挂起/直通）。代码路径本身用 tests/test_hook_chain.py 覆盖，
    但这个命令测的是**你这台机器、你这个进程**的真实情况。

    这个命令会自己开一个小窗口并抢到前台：注入式自检**必须**这样才准 ——
    实测在 Low 完整性下，进程自己没有前台窗口时 ``SendInput`` 照样返回成功，
    按键却连系统都没进（UIPI 拦的，返回值不体现失败），好钩子会被冤枉成坏的。

    第 104 轮：**先看有没有实例在跑**。常驻实例的钩子比本检查先装上，字母键
    被它吞掉（回调返回非 0，事件不再往下传），所以本检查要么收不到任何真实
    按键、要么连小窗口都打不进字 —— 这跟环境无关，是「自己家的钩子挡在前面」。
    这种情况下直接说清楚并返回 8（本次没做成），别让用户去查提权窗口和安全软件。
    """
    import os
    import tkinter as tk

    if log_path is None:
        from t_ime import daemon as _d
        log_path = _d.resolve_log_path(Path(root_dir), None, cfg)
    log_path = Path(log_path)

    seen: list[tuple[int, bool, bool]] = []

    def on_key(vk, is_down, injected):
        seen.append((vk, is_down, injected))
        return False        # 只观察，不吞

    print()
    print("== 键盘钩子自检 ==")
    print()

    from t_ime import daemon as _daemon_hc

    if _daemon_hc.is_running():
        from t_ime.hotkey import pretty as _pretty_hc

        pid = 0
        try:
            pid = int((_daemon_hc.read_status(Path(root_dir)) or {}).get("pid") or 0)
        except (TypeError, ValueError, OSError):
            pid = 0
        quit_txt = _pretty_hc((cfg.get("hotkeys") or {}).get("quit"))
        how = (f"按 {quit_txt}、或者右下角托盘图标右键 → 退出"
               if quit_txt else "右下角托盘图标右键 → 退出")
        print("[0] 实例状态           有一个 translate-ime 正在常驻"
              + (f"（pid {pid}）" if pid else ""))
        print("  它的钩子比本检查先装上，而它会把字母键吞掉：你在下面那个小窗口里敲的")
        print("  字母，既进不了小窗口，也到不了本检查。所以这时候的「真实按键 0」")
        print("  一定是它造成的，不能拿来判断环境（安全软件 / 提权窗口 / 远程桌面），")
        print("  照着这个数字去查环境只会走错路。")
        print(f"  先退出它（{how}），再跑一遍本命令。")
        print()
        return 8

    print(f"[1] 进程完整性级别  {W.integrity_level()[1]}")

    hook = W.KeyboardHook(on_key, "t-ime-hookcheck")
    if not hook.start(timeout=5.0):
        print(f"[2] 钩子安装           失败：{hook.error}")
        print()
        print("[3] 结论")
        print("  连全局键盘钩子都装不上，输入法不可能收到你的按键。")
        print("  常见原因：安全软件（含部分杀毒/输入法保护）拦了 WH_KEYBOARD_LL。")
        print("  试试把本程序加进白名单，或者关掉那个安全软件再跑一次。")
        print()
        return 5
    print("[2] 钩子安装           成功")

    # 真正开一个自己的前台窗口（上面解释过为什么必须有），顺带也是给用户的敲键地点。
    root = tk.Tk()
    root.title("translate-ime 键盘钩子自检")
    root.geometry("480x130+80+80")
    root.attributes("-topmost", True)
    tk.Label(
        root, justify="left", padx=16, pady=14, font=("Microsoft YaHei UI", 11),
        text=(f"请在这个窗口里敲几个字母（比如 nihao）。\n"
              f"{seconds:.0f} 秒后自动关闭；只观察、不吞你的键"
              f"（开头本程序会注入一个自检按键）。"),
    ).pack(fill="both", expand=True)
    root.deiconify()
    root.lift()
    root.focus_force()
    root.update()
    W.set_foreground(int(root.winfo_id()))
    root.update()

    def tick() -> None:
        """让窗口活着（Tk 得有人泵消息，不然它只会是一块白板）。"""
        try:
            root.update()
        except Exception:
            pass

    try:
        time.sleep(0.2)
        tick()
        if W.window_pid(W.foreground_hwnd()) != os.getpid():
            # 拿不到前台窗口 → 注入式自检这一刻没有意义。宁可说「没做成」，
            # 也不能把「送不出去」说成「钩子不生效」。
            probe_ok: bool | None = None
            print("[2b] 自检按键          跳过（这个进程没拿到前台窗口，注入送不进系统）")
        else:
            probe_ok = hook.probe(timeout=1.0)
            if probe_ok:
                print("[2b] 自检按键          回来了（钩子此刻确实在生效）")
            else:
                print("[2b] 自检按键          **没回来** —— 钩子装上了，但立刻就不生效了")

        print()
        print(_hook_check_help_text(seconds))
        print()
        real = 0
        injected = 0
        deadline = time.monotonic() + max(1.0, seconds)
        while time.monotonic() < deadline:
            tick()
            batch = seen[:]          # 先取快照再清空：钩子线程还在往里 append，
            seen.clear()             # 直接边遍历边 clear 会漏掉刚好挤进来的那个键
            for _vk, _down, inj in batch:
                if inj:
                    injected += 1
                else:
                    real += 1
            left = deadline - time.monotonic()
            # 先记账再打印：以前是先打印后抽干，进度条上的数字永远慢一拍。
            print(f"\r    剩余 {left:4.1f}s   收到 {real + injected} 个事件"
                  f"（真实 {real} · 注入 {injected}）   ", end="", flush=True)
            time.sleep(0.15)
        print()
        st = hook.stats()
    finally:
        try:
            root.destroy()
        except Exception:
            pass
        hook.stop()

    real = int(st.get("real") or 0)
    foreign = int(st.get("foreign_injected") or 0)
    own = int(st.get("self_injected") or 0)

    print()
    print("[4] 结论")
    probe_txt = {True: "通过", False: "**失败**", None: "没做成（拿不到前台窗口）"}[probe_ok]
    print(f"    真实按键 {real} 个 · 别的程序转发的 {foreign} 个 · 我们自己注入的 {own} 个"
          f" · 自检 {probe_txt}")
    print()
    if real > 0:
        print(f"  钩子看到了 {real} 个**真实**按键 —— 这一层是通的。")
    elif foreign > 0:
        print(f"  钩子一共收到 {foreign} 个事件，但它们**全都带着「别的程序注入」的标志**：")
        print("  你机器上有软件在转发你的按键（PowerToys Keyboard Manager、AutoHotkey、")
        print("  罗技/雷蛇驱动、某些笔记本快捷键工具、软键盘…）。")
        print("  这类事件以前会被输入法当成「自己回灌的」直接丢掉 —— 症状就是**打字完全没反应**；")
        print("  现在这个版本会把它当成真实按键处理，所以应该已经能打字了。")
        print("  如果还不行，请把这一屏原样发出来。")
        print()
        return 0
    else:
        print(_hook_nothing_text(seconds, own))
        if probe_ok is None:
            print("  而且这次**连自检都没做成**（这个进程没拿到前台窗口，注入的按键")
            print("  送不出系统），所以没法判断钩子到底还生不生效。")
            print("  再试一次：把别的窗口最小化，重新跑一遍这条命令；")
            print("  或者直接双击 run.bat 把输入法起起来，然后敲几个字母看看。")
            print("  请把这一屏原样发出来，我按这个继续查。")
            print()
            return 8
        if not probe_ok:
            print("  而且刚才那个自检按键**也没回来** —— 钩子装上了却立刻不生效：")
            print("    · 多半是系统按 LowLevelHooksTimeout 把它摘了（回调太慢）")
            print("    · 或者安全软件/另一个钩子吃掉了事件")
            print("  常驻实例会每 50 毫秒拿你自己的按键跟钩子收到的对账：发现漏键")
            print("  连着两次就自动重装钩子，最多重装一次（再不行就不再折腾）。")
            print("  如果这里反复失败，请把这一屏原样发出来。")
            print()
            return 7
        print("  说明钩子装上了、也还在生效（自检按键回来了），但收不到你的键盘输入。可能的原因：")
        print("    · 你敲的时候焦点在提权窗口（以管理员身份运行的程序）里")
        print("    · 安全软件在中间拦截了事件派发")
        print("    · 键盘是走远程桌面 / 虚拟机 / 某些 KVM 通道进来的，没进本会话的桌面")
        print("    · 你敲的时候那个小窗口已经不在前台了（敲到别的窗口也照样能收到，")
        print("      但若焦点跑到提权窗口就收不到）")
        print("  请把这一屏原样发出来，我按这个继续查。")
        print()
        return 6

    print("  如果你还是觉得「打字没反应」，那多半是状态问题，不是钩子问题：")
    from t_ime.hotkey import direct_switch_hint, pretty, tap_switch_hint

    if tap_switch_hint(cfg):
        tap_s = float((cfg.get("input") or {}).get("shift_tap_ms") or 600) / 1000.0
        print("    · 点一下 Shift 会切到「英文直通」，那时字母原样透传、不弹候选窗")
        print(f"      （是「点一下」：按住超过 {tap_s:.1f} 秒它就不算切换了，因为 Shift 还要")
        print("        兼职打大写和 Shift+点选；这一行没动过键盘就说明确实切过去了）")
    else:
        switch = direct_switch_hint(cfg)
        if switch:
            print(f"    · {switch} 会切到「英文直通」，那时字母原样透传、不弹候选窗")
        else:
            print("    · 切「英文直通」的键现在没有（input.shift_tap_toggles 关着，"
                  "hotkeys.toggle_direct 也没设）")
    suspend_hk = pretty(cfg["hotkeys"].get("toggle_enabled"))
    print(f"    · {suspend_hk} 会挂起输入法，此时所有键都透传"
          if suspend_hk else "    · 挂起输入法（hotkeys.toggle_enabled）时所有键都透传")
    print("  上面这些状态下连打 3 个字母，候选窗会提示你一次。")
    print(f"  另外看一眼日志 {log_path}，里面记了状态和错误。")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
