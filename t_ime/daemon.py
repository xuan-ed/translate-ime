# -*- coding: utf-8 -*-
"""常驻后台所需的那点东西：日志落文件、脱离控制台、单实例、开机自启。

输入法跟普通命令行工具不一样 —— 它是**一直在后台等按键**的。所以：

* 不能占着一个黑窗口（用户会顺手关掉它，输入法就死了）→ 用 ``pythonw.exe``
  跑，控制台没了，但 stdout/stderr 还在，得把它们接到日志文件上去。
* 不能开着两份（两个进程装两个钩子会互相抢键）→ 具名互斥体。
* 用户得找得到入口（看不到窗口就只能靠托盘图标）→ 见 ``t_ime/tray.py``。
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from . import winapi as W

__all__ = [
    "DEFAULT_LOG_NAME",
    "LOG_MAX_BYTES",
    "LOG_MAX_ENV",
    "log_max_bytes",
    "resolve_log_path",
    "setup_file_logging",
    "pythonw_path",
    "background_command",
    "spawn_background",
    "READY_TIMEOUT_SECONDS",
    "SPAWN_NOT_READY",
    "SPAWN_ALREADY_RUNNING",
    "wait_ready",
    "autostart_command",
    "autostart_status",
    "autostart_problem",
    "autostart_enabled",
    "set_autostart",
    "startup_dir",
    "startup_script_path",
    "is_running",
    "claim_instance",
    "already_running",
    "STATUS_NAME",
    "CTL_NAME",
    "status_path",
    "ctl_path",
    "write_status",
    "read_status",
    "clear_status",
    "status_is_stale",
    "status_age",
    "STATUS_HEARTBEAT_SECONDS",
    "STATUS_STALE_SECONDS",
    "send_command",
    "take_commands",
    "discard_commands",
]

DEFAULT_LOG_NAME = "translate-ime.log"
# 日志超过这个大小就轮转一次，免得常驻几个月后变成几百 MB
LOG_MAX_BYTES = 2 * 1024 * 1024
# 测试/排障时可以把阈值调小（和 T_IME_TEST_AUTOSTART 一样，只在进程启动时读一次）
LOG_MAX_ENV = "T_IME_LOG_MAX_BYTES"
# 轮转失败之后隔多久再试一次。改名失败（被别的程序占着、权限不够）时如果每写
# 一行都去撞一次，等于把日志变成了 I/O 忙等；一分钟一次足够，也不至于永远放弃。
ROTATE_RETRY_SECONDS = 60.0


def log_max_bytes() -> int:
    """本次进程的日志轮转阈值：环境变量能覆盖，值不合法就退回默认。

    ``T_IME_LOG_MAX_BYTES`` 存在的理由很实际：要不这么写，测试就只能靠写几 MB
    数据去撞那个 2 MB 的阈值，而**常驻实例中途轮转**这件事又必须用真进程验。
    """
    raw = os.environ.get(LOG_MAX_ENV)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return LOG_MAX_BYTES

# 状态文件的心跳。常驻实例只在「状态有变化」的时候写盘（这是故意的：不然每秒
# 都要写一次盘），于是 mtime 在空闲时会冻住 —— 而外面判断「那个实例还活着吗」
# 靠的正是 mtime。结果就是：用户只要十来秒不打字，--doctor 和第二次双击的面板
# 就会说它「超过 10 秒没更新，可能卡死或被强杀了」，而它其实好得很。
# 心跳让 mtime 每 STATUS_HEARTBEAT_SECONDS 秒至少前进一次；「两个心跳都没来」
# （STATUS_STALE_SECONDS）才是真的不动了。
STATUS_HEARTBEAT_SECONDS = 5.0
STATUS_STALE_SECONDS = 10.0
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "translate-ime"


# ---------------------------------------------------------------------- #
# 日志
# ---------------------------------------------------------------------- #
class _LogFile:
    """长得像文件对象，但每一行前面盖上时间戳。

    直接 ``sys.stdout = open(log)`` 也行，但那样日志里没有时间，出问题根本
    不知道是什么时候的事 —— 输入法这种常驻程序，「什么时候开始不灵的」往往是
    唯一的线索。

    它还会**在写到一半的时候自己轮转**：以前轮转只在 ``setup_file_logging``
    里做一次，也就是只有「下次启动」才会执行 —— 而常驻实例恰恰是几周不重启的
    那一个，于是 README 里那句「超过 2 MB 自动轮转」对最需要它的场景完全不成立。
    """

    def __init__(self, path: Path, *, max_bytes: int | None = None):
        self.path = path
        self.max_bytes = int(max_bytes) if max_bytes else log_max_bytes()
        self._fh = open(path, "a", encoding="utf-8", errors="replace")
        self._at_line_start = True
        self.encoding = "utf-8"
        self.errors = "replace"
        self.rotations = 0
        self.rotate_failures = 0
        self.last_rotate_error = ""
        self._rotate_retry_at = 0.0
        self._size = self._tell()

    def _tell(self) -> int:
        try:
            return int(self._fh.tell())
        except Exception:
            return 0

    def write(self, s: str) -> int:
        if not s:
            return 0
        text = s.replace("\r\n", "\n").replace("\r", "\n")
        for i, part in enumerate(text.split("\n")):
            if i:
                self._fh.write("\n")
                self._at_line_start = True
            if part:
                if self._at_line_start:
                    self._fh.write(time.strftime("[%Y-%m-%d %H:%M:%S] "))
                    self._at_line_start = False
                self._fh.write(part)
        self._fh.flush()
        self._size = self._tell()
        if (self.max_bytes and self._size >= self.max_bytes
                and time.monotonic() >= self._rotate_retry_at):
            self._rotate_mid_run()
        return len(s)

    def _rotate_mid_run(self) -> None:
        """写到超过阈值时当场轮转：关掉、改名成 ``.1``、重新打开。

        整个过程对调用方完全透明（``sys.stdout`` 还是同一个对象、``write()``
        照样能用）。任何一步失败都退回「继续往原文件写」，绝不能因为日志把
        打字搞崩。

        改名**失败**时不许说成功：``rotations`` 不动、也不写「已轮转成 .1」
        那行假话，只留一行「轮转失败（原因）」，然后隔
        :data:`ROTATE_RETRY_SECONDS` 再试。测试抓到过原来的版本：改名一直被
        拒，日志里却连写 8 次「已轮转成 fail.log.1」。
        """
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass
        ok = False
        err = ""
        try:
            _rotate_now(self.path)
            ok = True
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            self.rotate_failures += 1
            self.last_rotate_error = err
            self._rotate_retry_at = time.monotonic() + ROTATE_RETRY_SECONDS
        try:
            self._fh = open(self.path, "a", encoding="utf-8", errors="replace")
        except Exception:
            try:
                self._fh = open(os.devnull, "a", encoding="utf-8", errors="replace")
            except Exception:
                return
        self._at_line_start = True
        try:
            if ok:
                self.rotations += 1
                mb = self.max_bytes / (1024 * 1024)
                self._fh.write(
                    time.strftime("[%Y-%m-%d %H:%M:%S] ")
                    + f"===== 日志超过 {mb:.1f} MB，已轮转成 {self.path.name}.1 =====\n"
                )
            elif self.rotate_failures == 1:
                self._fh.write(
                    time.strftime("[%Y-%m-%d %H:%M:%S] ")
                    + f"===== 日志轮转失败（{err}），继续写这个文件 =====\n"
                )
            self._fh.flush()
        except Exception:
            pass
        self._size = self._tell()

    def flush(self) -> None:
        self._fh.flush()

    def isatty(self) -> bool:
        return False

    def reconfigure(self, **kwargs) -> None:
        """``t_ime.console`` 会无脑调这个方法，这里接受并忽略。"""

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def _as_log_file(p: Path, raw: str = "", default: str = DEFAULT_LOG_NAME) -> Path:
    """``--log`` / ``output.log_file`` 指的若是目录，就在里面用默认文件名。

    第 67 轮真机 bug：``--log .tmp\\logsdir``（一个已经存在的目录）会让后台
    子进程在 ``_LogFile.__init__`` 的 ``open(path, "a")`` 上抛
    ``PermissionError``，而启动器只会说「它连日志都没写出来 —— 最常见的原因
    是依赖没装」，把人往完全错误的方向带。把「目录」当成「目录 + 默认文件名」
    是用户最自然的读法，成本也最低。末尾带分隔符的写法（``logs\\``）同样按
    目录处理，哪怕它此刻还不存在 —— 所以要看**原始写法**：`Path` 会把末尾
    分隔符吃掉，只靠 ``p.is_dir()`` 会漏掉「还不存在的目录」。
    """
    text = raw or str(p)
    looks_like_dir = p.is_dir() or text.endswith(os.sep) or text.endswith("/")
    return p / default if looks_like_dir else p


def resolve_log_path(root_dir: Path, override: str | os.PathLike | None = None,
                     cfg: dict | None = None) -> Path:
    """决定日志写哪儿。优先级：命令行 > 配置 > 项目根目录下的默认名。"""
    name = ((cfg or {}).get("output") or {}).get("log_file") or DEFAULT_LOG_NAME
    if override:
        raw = str(override)
        p = Path(override)
        return _as_log_file(p if p.is_absolute() else Path(root_dir) / p, raw)
    p = Path(name)
    return _as_log_file(p if p.is_absolute() else Path(root_dir) / p, str(name))


def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > log_max_bytes():
            _rotate_now(path)
    except Exception:
        pass  # 轮转失败不该拖垮启动


def _rotate_now(path: Path) -> None:
    """把 ``path`` 改名成 ``.1``（旧的 ``.1`` 直接删掉，只留一代）。"""
    bak = path.with_suffix(path.suffix + ".1")
    if bak.exists():
        bak.unlink()
    if path.exists():
        path.rename(bak)


def setup_file_logging(path: Path, *, verbose: bool = True, keep_console: bool = False) -> _LogFile:
    """把 stdout/stderr 接到日志文件上。返回那个文件对象（调用方持有引用）。"""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    _rotate(path)
    log = _LogFile(path)

    if keep_console:
        # 控制台还在（run.bat 前台跑）——两边都写，用户在屏幕上也看得到
        orig_out, orig_err = sys.stdout, sys.stderr

        class _Tee:
            def __init__(self, a, b):
                self._a, self._b = a, b

            def write(self, s):
                try:
                    self._a.write(s)
                except Exception:
                    pass
                return self._b.write(s)

            def flush(self):
                for x in (self._a, self._b):
                    try:
                        x.flush()
                    except Exception:
                        pass

            def isatty(self):
                return False

            def reconfigure(self, **kwargs):
                pass

        sys.stdout = _Tee(orig_out, log)
        sys.stderr = _Tee(orig_err, log)
    else:
        sys.stdout = log
        sys.stderr = log

    if verbose:
        log.write(f"\n===== translate-ime 启动 pid={os.getpid()} =====\n")
    return log


# ---------------------------------------------------------------------- #
# 脱离控制台
# ---------------------------------------------------------------------- #
def pythonw_path() -> Path | None:
    """找 ``pythonw.exe``：跟当前解释器同目录的那一个。

    用它跑就**没有控制台窗口**。找不到就返回 None，调用方回退成普通 python。

    打包成 exe 之后没有 pythonw.exe 这回事 —— 那个 exe 自己就是窗口版
    （``console=False``），直接返回它自己。
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        return exe if exe.exists() else None
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    if cand.exists():
        return cand
    return None


def background_command(root_dir: Path, argv: list[str]) -> list[str] | None:
    """拼出「后台常驻」版本的命令行（用 pythonw，并带上 --detached 防止再自旋）。

    打包版里没有 ``main.py`` 这个文件可执行 —— 再跑一次 exe、加同样的参数即可。
    """
    py = pythonw_path()
    if py is None:
        return None
    extra = [a for a in argv if a not in ("--background", "--detached")]
    if getattr(sys, "frozen", False):
        return [str(py), "--background", "--detached", *extra]
    main_py = Path(root_dir) / "main.py"
    return [str(py), str(main_py), "--background", "--detached", *extra]


def cli_value(argv: list[str] | None, name: str) -> str | None:
    """从命令行里取出 ``--name VALUE`` 或 ``--name=VALUE`` 的值。

    第 69 轮：``argparse`` 两种写法都认，而这里以前只认前者。于是
    ``--config=alt.json`` 启动时，实例读的是 alt 那份配置，提示里的退出热键、
    失败时去翻的日志尾巴却都退回默认 —— 同一行命令，两边各解析各的。
    """
    args = list(argv or [])
    prefix = name + "="
    for i, tok in enumerate(args):
        if tok == name:
            return args[i + 1] if i + 1 < len(args) else None
        if tok.startswith(prefix):
            return tok[len(prefix):]
    return None


def _quit_hotkey_pair(root_dir: Path,
                      argv: list[str] | None = None) -> tuple[str, str]:
    """退出热键的（能用的写法, 不能用的原因）。两者最多一个非空。

    「不能用」包括：解析失败、以及最坑的那种 —— 没有修饰键的打字键
    （``hotkeys.quit = "q"``）。后者在打字时按到 q 就会触发退出，核心会拒绝注册，
    所以这里也不能再把它当成「按 Q 退出」报给用户。
    """
    raw_conf = cli_value(argv, "--config")
    conf: Path | None = Path(raw_conf) if raw_conf else None
    try:
        from .config import load as load_config
        from .hotkey import pretty, spec_problem
        from .winapi import combo_text, parse_hotkey
        cfg, _path = load_config(conf)
        raw = str((cfg.get("hotkeys") or {}).get("quit") or "")
        text = pretty(raw)
        if not text:
            return "", ""
        problem = spec_problem(raw, parse_hotkey)
        if problem:
            return "", problem
        try:
            mods, main = parse_hotkey(raw)
        except Exception as exc:
            # spec_problem 对「解析不了」是故意返回空串（解析错另有话说），所以这里
            # 要自己接住：否则热键明明不生效，退出提示却像没事一样。
            return "", f"这个写法解析不了：{exc}"
        # 只报真正注册上的组合键：``"ctrl+q+w"`` 生效的是 Ctrl+W，不是原文。
        return combo_text(mods, main), ""
    except Exception:
        return "", ""


def quit_hotkey_text(root_dir: Path, argv: list[str] | None = None) -> str:
    """配置里那个**能用的**退出热键的显示文本（读不到 / 不能用就返回空串）。

    写死 ``Ctrl+Alt+Q`` 的话，用户改过 ``hotkeys.quit`` 之后这句提示就成了假话。
    ``--config PATH`` / ``--config=PATH`` 都要认，否则「按指定配置启动」时提示的
    还是默认那份。``argv`` 省略就不看命令行参数。
    """
    return _quit_hotkey_pair(root_dir, argv)[0]


def quit_hotkey_problem(root_dir: Path, argv: list[str] | None = None) -> str:
    """退出热键配了、但按下去会误触时，为什么不能用（否则空串）。"""
    return _quit_hotkey_pair(root_dir, argv)[1]


def _log_arg(argv: list[str]) -> str | None:
    """从命令行里挑出 ``--log``（``--log PATH`` 与 ``--log=PATH`` 都认）。

    子进程起不来时要用它去找日志尾巴；只认一种写法的话，另一种写法的日志
    会被悄悄漏掉，失败提示就变成「去翻默认位置的日志」。
    """
    return cli_value(argv, "--log")


#: 父进程等子进程「真的能打字」最多等多久（秒）。
#:
#: 第 88 轮真机：`spawn_background()` 只要 1.5 秒确认子进程没死就宣布「已在后台
#: 常驻」，可冷启动时子进程还在建拼音索引（本机实测整轮 2.0 秒，加上窗口层初始化
#: 约 3 秒才装上钩子）。这段时间用户看到的是一个「成功的」黑框，打字却是原样字母、
#: 候选窗一次也不出现 —— 很容易被当成「输入法坏了」。所以父进程要等到状态文件
#: 报到 `hook=True` 再说「已就绪」；等不到也如实说，别让「已常驻」替它打包票。
READY_TIMEOUT_SECONDS = 6.0

#: ``spawn_background()`` 的「起来了，但还没就绪」退出码。
#:
#: 第 93 轮真机：把 ``pinyin.max_word_len`` 调到 12、``min_freq`` 调到 0 之后，
#: 冷启动建索引要 20 秒上下，远超 ``READY_TIMEOUT_SECONDS``。这时父进程一边在
#: stderr 说「6 秒了还没就绪 —— 现在就打字不会有候选」，一边照旧返回 0，于是
#: ``main.py`` 紧接着打印「已在后台启动」、run.bat 也当作成功安静退出 —— 同一屏
#: 两句话互相打脸，而且此刻实例连状态文件都还没写出来，用户再双击一次得到的是
#: 「读不到状态：可能是旧版本在跑」。所以「没就绪」必须是一个能被上层看见的结果。
SPAWN_NOT_READY = 4

#: ``spawn_background()`` 的「已经有一份在跑了，这次没重复启动」退出码。
#:
#: 第 106 轮真机：同时点两下 run.bat（或者开机自启与手动双击撞在一起）时，
#: 两个父进程都看不见实例、都会交棒；两个子进程里只有一个抢到互斥体，另一个
#: 按单实例约定 ``return 3``。父进程只看「非 0 退出码」，于是把它当成失败：
#: 先打「后台进程启动后立刻退出（退出码 3）」，再按日志尾巴列原因，最后
#: ``[main] 后台启动失败（退出码 1）``，run.bat 跟着 pause —— 而输入法其实
#: 好好地常驻着，用户看到的是「启动失败」。这跟第 93 轮是同一族的病：
#: **子进程已经说清楚原因了（3 = 已有实例），父进程却只会翻译成「失败」**。
SPAWN_ALREADY_RUNNING = 3


def wait_ready(root_dir: Path, *, timeout: float | None = None) -> dict | None:
    """等常驻实例真的能接管键盘（状态文件报到 ``hook=True``）。

    返回那一刻的状态字典；``timeout`` 秒内没等到就返回 ``None``。
    只看「进程还在」是不够的：索引没建完之前进程活着但钩子还没装。
    钩子**明确装失败**时也会立刻返回（状态里 ``hook_info.error`` 有原因）——
    这种「等不到」不该再等满超时，更不该由调用方猜成「还在建索引」。
    """
    wait = READY_TIMEOUT_SECONDS if timeout is None else float(timeout)
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        try:
            st = read_status(root_dir)
        except Exception:
            st = None
        if st:
            # 明确失败（键盘钩子装不上）不用等满 6 秒：失败原因已经有值了，再等
            # 也等不到 hook=True。留着等下去，父进程最后只会说一句「还没就绪 ——
            # 多半在建拼音索引」，把「钩子被安全软件拦了」藏起来（第 107 轮）。
            if (st.get("hook_info") or {}).get("error"):
                return st
            if st.get("hook") and not status_is_stale(root_dir):
                return st
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.15)


def _tail_lines(path: Path, n: int = 12) -> list[str]:
    """读日志最后 n 行（文件不存在 / 读不动就返回空列表）。"""
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n") for ln in fh.readlines()[-n:]]
    except Exception:
        return []


def spawn_background(root_dir: Path, argv: list[str],
                     log_path: Path | None = None) -> int:
    """把自己重新拉起来（无窗口），父进程立刻退出。返回 0 表示已交棒。

    ``log_path`` 是**调用方已经解析好的**日志路径（例如 main.py 从
    ``output.log_file`` 算出来的那份）。不传才退回到「只看命令行」的老算法。
    """
    import subprocess

    _log = (Path(log_path) if log_path is not None
            else resolve_log_path(root_dir, _log_arg(argv), None))
    cmd = background_command(root_dir, argv)
    if cmd is None:
        print("[daemon] 找不到 pythonw.exe，无法脱离控制台；请留在前台运行。", file=sys.stderr)
        return 2
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：父进程退出后子进程不受影响
    flags = 0x00000008 | 0x00000200
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(root_dir), close_fds=True, creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        print(f"[daemon] 后台启动失败：{exc!r}", file=sys.stderr)
        return 1

    # **等一下再报成功。** 第 41 轮真机 bug：新机器上缺依赖时，子进程在 import
    # 阶段就死了（连日志都还没建），而父进程立刻打印「已在后台常驻」并退出 0 ——
    # 用户双击 run.bat，只看到一个闪了一下的黑框，然后什么都没发生。
    # 这里给 1.5 秒的宽限：这个窗口足够让「一导入就死」暴露，又短到不拖慢启动。
    # 注意不能用退出码 0 + 很快退出判断失败：venv 里的 pythonw.exe 只是个转发器，
    # 它把活交给真正的解释器之后自己就可能正常退出（见踩坑第 32 条）。
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    code = proc.poll()
    if code not in (None, 0):
        # 子进程说「已经有一份在跑了」（main.py 的单实例约定：抢不到互斥体就
        # ``return 3``）。这不是失败，而是「同时按了两下」的常见结局。以前这里
        # 一律按失败报，用户看到的是「后台启动失败（退出码 1）」+ run.bat 的
        # pause，可输入法明明在跑（第 106 轮）。
        if code == SPAWN_ALREADY_RUNNING and is_running():
            _st = read_status(root_dir) or {}
            _pid = _st.get("pid")
            _who = f"（pid {_pid}）" if _pid else ""
            print(f"[daemon] 已经有一个 translate-ime 在跑了{_who}，"
                  f"这次不再启动第二份。")
            return SPAWN_ALREADY_RUNNING
        print(f"[daemon] 后台进程启动后立刻退出（退出码 {code}）。", file=sys.stderr)
        tail = _tail_lines(_log)
        if tail:
            print(f"[daemon] 日志最后几行（{_log}）：", file=sys.stderr)
            for ln in tail:
                print("  " + ln, file=sys.stderr)
        else:
            print("[daemon] 它连日志都没写出来 —— 最常见的两个原因："
                  "① --log / output.log_file 指的位置写不进去；"
                  "② 依赖没装（pypinyin / jieba）或拼音索引建不出来。", file=sys.stderr)
        print("[daemon] 想看完整原因：run.bat --foreground", file=sys.stderr)
        return 1

    print(f"[daemon] 已在后台常驻（无窗口）。日志见 {_log}")
    # 「进程活着」不等于「能打字」：冷启动还要建拼音索引，这期间钩子没装上，
    # 用户敲的字母原样进文档、候选窗不出现。等它真报到 hook=True 再说「已就绪」。
    _not_ready = False
    if "--no-hook" not in argv:
        _ready = wait_ready(root_dir)
        if _ready is None:
            _not_ready = True
            print(f"[daemon] {READY_TIMEOUT_SECONDS:.0f} 秒了还没就绪（第一次启动要建"
                  f"拼音索引；pinyin.max_word_len 调大、min_freq 调小都会让它更久）"
                  f"—— 就绪之前敲的字不会被接管，也不弹候选；稍等一下，"
                  f"或者看日志确认。", file=sys.stderr)
        elif (_ready.get("hook_info") or {}).get("error"):
            # 「常驻起来了」和「能打字」是两件事：钩子明确装失败时不许报「已就绪」，
            # 也不许把它说成「还在建索引」—— 原因已经有了，就直接说原因和下一步
            # （第 107 轮：以前这条路径由 app 的 `hook: self.hook is not None` 兜着，
            # 于是启动器打印「已就绪：钩子装上了」，实例日志却写着「安装失败」）。
            _not_ready = True
            _why = str((_ready.get("hook_info") or {}).get("error"))
            print(f"[daemon] 进程起来了，但键盘钩子没装上：{_why}", file=sys.stderr)
            print("[daemon] 所以现在打字不会有任何反应 —— 多半是安全软件拦了全局键盘钩子；"
                  "先跑 run.bat --hook-check 看细一点，或者把它加进白名单再重启。",
                  file=sys.stderr)
        else:
            print("[daemon] 已就绪：钩子装上了，现在可以打字了。")
    # 退出键从配置里取：用户改过 hotkeys.quit 之后，写死的那句就成了假话。
    _quit_hk = quit_hotkey_text(root_dir, argv)
    if _quit_hk:
        print(f"[daemon] 退出：托盘图标右键 -> 退出，或按 {_quit_hk}。")
    else:
        _why = quit_hotkey_problem(root_dir, argv)
        if _why:
            print(f"[daemon] 退出：托盘图标右键 -> 退出"
                  f"（hotkeys.quit 配的写法不能用：{_why}）。")
        else:
            print("[daemon] 退出：托盘图标右键 -> 退出，"
                  "或按 config.json 里 hotkeys.quit 设的键。")
    # 没就绪就不许报成功：上层（main.py / run.bat）要能看见这个结果，
    # 别让「已在后台启动」替一个还不能打字的实例担保（第 93 轮）。
    return SPAWN_NOT_READY if _not_ready else 0


# ---------------------------------------------------------------------- #
# 单实例
# ---------------------------------------------------------------------- #
# 第 21 轮踩过的坑：以前这里只有一个 ``already_running()``，名字像「问一句」，
# 实现却是 ``not W.single_instance(...)`` —— 而 ``single_instance()`` 会
# **把锁据为己有并一直挂到进程退出**。于是一个只是想知道「在跑吗」的进程
# （探针、控制面板、任何常驻的辅助程序）会**变成那个实例**：它活着的期间，
# 真正的启动会看到「已经有实例在跑」而拒绝启动（控制面板还会说「读不到状态：
# 可能是旧版本在跑，或者状态文件被删了」）。用户看到的就是「双击了没反应」。
#
# 现在拆成两个名字，语义写在名字里：
#   * :func:`is_running`     —— 只探测，不占锁；任何地方都能安全地调。
#   * :func:`claim_instance` —— 真的把锁拿走；**只有常驻实例自己**该调。
def is_running() -> bool:
    """已经有别的实例在跑吗。

    纯查询：借来句柄看一眼就立刻还回去，**不会**让自己变成那个实例，
    所以探针/控制面板/任何活着很久的东西都可以放心调。
    """
    return W.instance_exists("translate-ime")


def claim_instance() -> bool:
    """把单实例锁据为己有，一直挂到本进程退出。

    返回 True = 抢到了（你可以当那个常驻实例）；False = 已经有人在跑。

    这才是「我要用锁」的那个调用，只有常驻实例自己该用它 —— 别的进程用了，
    锁就被它带走了，真正的实例反而起不来（见上面那段注释）。
    """
    return W.single_instance("translate-ime")


def already_running() -> bool:
    """老名字，保留给历史代码；现在等同于 :func:`is_running`（只探测，不占锁）。"""
    return is_running()


# ---------------------------------------------------------------------- #
# 开机自启
# ---------------------------------------------------------------------- #
# 开机自启项里值得带过去的参数：只有这两项会改变「跑起来的是哪一个实例」。
# 别的参数（--check/--demo/--doctor…）都是一次性动作，带进自启项会让开机时的实例
# 起来就退出；--no-hook/-v 属于调试口味，也不该被悄悄固化。
_AUTOSTART_KEEP = ("--config", "--log")


def autostart_extra(argv: list[str] | None) -> list[str]:
    """从用户这次敲的命令行里挑出要写进自启项的参数（并转成绝对路径）。

    第 68 轮：以前自启项永远只有 ``["--background", "--detached"]``，用户
    ``--autostart --config D:\\我的\\ime.json`` 装好自启后，开机起来的却是
    ``<root>/config.json`` 那份 —— 状态照样报「已开启」，配置、日志、热键全换了，
    用户完全没有线索。开机时的工作目录不是用户敲命令时的目录，所以相对路径
    必须在这里就解析成绝对路径。
    """
    args = list(argv or [])
    keep: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        name, eq, inline = a.partition("=")
        if name not in _AUTOSTART_KEEP:
            i += 1
            continue
        if eq:
            value = inline
            i += 1
            if not value:
                continue
        else:
            # 只有开关没有值（`--config --log x.log`）：只跳过这个开关，别把下一个
            # 开关当成路径吃掉 —— 后面那个 `--log x.log` 还是应该被记下来。
            if i + 1 >= len(args) or args[i + 1].startswith("--"):
                i += 1
                continue
            value = args[i + 1]
            i += 2
        try:
            value = str(Path(value).resolve())
        except OSError:
            pass
        keep += [name, value]
    return keep


def autostart_command(root_dir: Path, argv: list[str] | None = None) -> str | None:
    cmd = background_command(root_dir, autostart_extra(argv))
    if cmd is None:
        return None
    return " ".join(f'"{a}"' for a in cmd)


def startup_dir() -> Path:
    """当前用户的「启动」文件夹。注册表写不进去时的备选方案。"""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_script_path() -> Path:
    return startup_dir() / "translate-ime.cmd"


def _explain(exc: OSError) -> str:
    """把 Windows 的错误码翻译成人能看懂的一句话。"""
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    denied = code == 5 or code == 13  # ERROR_ACCESS_DENIED / EACCES
    if denied:
        return ("拒绝访问 —— 当前进程的完整性级别低于用户登录会话，写注册表和启动文件夹"
                "都被限制了。请在一个正常打开的终端里执行这条命令（双击 run.bat 那种环境"
                "就行），不要在被降权的沙箱里跑。")
    if code in (2, 3):
        return f"路径不存在（错误码 {code}）"
    return f"{type(exc).__name__}: {exc}"


def _registry_autostart(enable: bool, cmd: str | None) -> tuple[bool, str]:
    import winreg

    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            if enable:
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, cmd or "")
            else:
                try:
                    winreg.DeleteValue(k, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return True, ""
    except OSError as exc:
        return False, _explain(exc)


def _cmd_cp() -> int:
    """cmd.exe 解析 ``.cmd`` 时默认用的代码页（控制台/OEM 代码页，中文 Windows 是 936）。"""
    if os.name != "nt":
        return 65001
    try:
        import ctypes

        for name in ("GetOEMCP", "GetACP"):
            fn = getattr(ctypes.windll.kernel32, name, None)
            if fn is None:
                continue
            cp = int(fn())
            if cp > 0:
                return cp
    except Exception:
        pass
    return 936


def _cmd_encoding() -> str:
    """写 ``.cmd`` 时该用什么编码（codec 名）。

    cmd.exe 是**按当时的控制台代码页**去解析批处理文件的，所以文件内容必须和文件里那行
    ``chcp`` 同一个口径 —— 只按「本机代码页」写、却不 ``chcp``，就只有在控制台代码页
    恰好一致时才正常：

    - ``ascii``：路径里一有中文就抛 ``UnicodeEncodeError``（本项目在这台机器上的路径是
      ``D:\\dsh-work\\杂活\\…``，所以它**已经**踩中了：文件被建成 0 字节、异常还一路抛到
      ``main.py``）；
    - ``utf-8``：写出 cmd 按 936 读不懂的字节，轻则乱码重则把解析器搞崩（README 踩坑第 7 条）；
    - ``mbcs``（= 本机 ANSI 代码页）：字节是对的，**但只在控制台代码页恰好也是它时才对**。
      实测把这份 .cmd 交给 ``cmd /c`` 时，中文路径被读成了 ``锟接伙拷``，于是
      ``The system cannot find the file D:\\dsh-work\\锟接伙拷\\…`` —— 自启静默失效。
      （这就是本轮抓到的第二个坑：第一版修完「写得出来」，但「跑得起来」还没验。）

    所以现在的写法是：文件按下面的编码写，**并且**在里面加一行 ``chcp <同一个代码页>``，
    让 cmd 无论从哪个代码页启动都按这份文件的字节读。
    """
    return f"cp{_cmd_cp()}" if os.name == "nt" else "utf-8"


def _drop_tmp(tmp: Path) -> None:
    """失败路径上把写到一半的 ``.cmd.tmp`` 删掉。

    留着它有两个坏处：启动文件夹里多一个看不懂的文件，以及「明明报了失败、
    却有一个名字像自启脚本的东西躺在那里」。**先写 .tmp 再换名**只有在收尾
    干净时才有意义。
    """
    try:
        if tmp.exists():
            tmp.unlink()
    except OSError:
        pass


def _startup_cmd_bytes(cmd: str) -> tuple[bytes, int]:
    """把 ``.cmd`` 的内容编成字节，返回 ``(字节, 用到的代码页)``。

    先按本机代码页编（那就是 cmd 的默认口径），编不动（英文系统的 cp437 + 中文路径）
    就退回 cp936 并让文件里的 ``chcp`` 一起改；再不行就把话说明白，别写出一个
    cmd 读不懂的文件。
    """
    for cp in (_cmd_cp(), 936, 65001):   # 第一项就是 _cmd_encoding() 对应的代码页
        body = (
            "@echo off\r\n"
            f"chcp {cp} >nul\r\n"
            "rem Generated by translate-ime --autostart.\r\n"
            "rem Delete this file to disable autostart.\r\n"
            f"start \"\" {cmd}\r\n"
        )
        try:
            return body.encode(f"cp{cp}"), cp
        except (UnicodeEncodeError, LookupError):
            continue
    raise ValueError("开机自启的命令行里含有本机任何代码页都编不出来的字符")


def _startup_folder_autostart(enable: bool, cmd: str | None) -> tuple[bool, str]:
    path = startup_script_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        if enable:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 注释一律用 ASCII 英文（cmd.exe 按代码页读这个文件，汉字注释只是无谓的风险）；
            # 真正的命令行按「本机代码页」写，并在文件里 chcp 成同一个代码页 ——
            # 只写不对齐的代码页，中文路径会被 cmd 读成乱码，`start` 就找不到 exe。
            data, _cp = _startup_cmd_bytes(cmd or "")
            # 先写临时文件再换名：中途失败不会在启动文件夹里留一个残缺的 .cmd，
            # 否则 autostart_status() 会看到一个「存在但没用」的文件。
            tmp.write_bytes(data)
            os.replace(tmp, path)
        else:
            if path.exists():
                path.unlink()
            _drop_tmp(tmp)          # 顺手清掉上一次失败留下的半成品
        return True, ""
    except OSError as exc:
        _drop_tmp(tmp)
        return False, _explain(exc)
    except (ValueError, UnicodeError) as exc:
        # UnicodeEncodeError 不是 OSError，早先它就这么一路抛到 main.py，
        # 用户看到的是一个吓人的 traceback，而不是「自启没装上，原因是……」
        _drop_tmp(tmp)
        return False, f"写 {path.name} 失败：{type(exc).__name__}: {exc}"


def _startup_script_ok() -> bool:
    """启动文件夹里的 ``.cmd`` 算不算「真的会自启」。

    只看 ``exists()`` 会说谎：早先写盘时用 ascii 编码写中文会抛
    ``UnicodeEncodeError``，而那时文件**已经被建出来了（0 字节）**，
    于是状态一直报 ``startup-folder`` —— 用户以为自启开着，开机却什么都没发生。
    所以这里要求文件非空。
    """
    p = startup_script_path()
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def autostart_status() -> str:
    """现在是什么方式在自启：``registry`` / ``startup-folder`` / ``none``。"""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
            value, _ = winreg.QueryValueEx(k, AUTOSTART_NAME)
            if value:
                return "registry"
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return "startup-folder" if _startup_script_ok() else "none"


_QUOTED_RE = re.compile(r'"([^"]+)"')


def _autostart_tokens() -> tuple[str, list[str]]:
    """把自启项里记下的命令行读出来，拆成参数列表（注册表优先，其次启动文件夹）。

    第 59 轮：``autostart_status()`` 只看「注册表里有没有一个非空值 / .cmd 是不是非空」，
    可那个值里写的可能是**别处**的 pythonw，或者早就被删掉的路径（换了目录、重建了
    .venv、装了另一个副本）。这时状态照样报「已开启」，用户以为开机没问题，其实开机
    什么都没发生。要回答「它真的会启动吗」，得把命令拆开、逐个路径去问。
    """
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
            value, _ = winreg.QueryValueEx(k, AUTOSTART_NAME)
            text = str(value or "")
            if text:
                tokens = _QUOTED_RE.findall(text) or text.split()
                return "registry", tokens
    except OSError:
        pass

    path = startup_script_path()
    if not _startup_script_ok():
        return "none", []
    try:
        data = path.read_bytes()
    except OSError:
        return "startup-folder", []
    # .cmd 里写着 `chcp N`，N 就是这份文件按哪个代码页存的 —— 先按它解，解不开再猜。
    # 只认能解出整份文本的编码：latin-1 兜底（它永不失败），免得中文路径被解成乱码、
    # 反被报成「指向另一个位置」。
    codecs: list[str] = []
    m = re.search(rb"chcp\s+(\d+)", data, re.IGNORECASE)
    if m:
        codecs.append(f"cp{m.group(1).decode('ascii')}")
    codecs += [f"cp{_cmd_cp()}", "utf-8", "cp936", "latin-1"]
    text = ""
    for codec in codecs:
        try:
            text = data.decode(codec)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    tokens = _QUOTED_RE.findall(text)
    return "startup-folder", tokens


def autostart_problem(root_dir: Path) -> str:
    """自启项存在，但那条命令**现在还能不能真的跑起来**。

    返回一句人话给用户看；没问题返回空字符串。检查两件事：

    * 命令里的 ``pythonw.exe`` 和 ``main.py`` 还在不在（换目录、重建 venv、
      删掉旧副本都会留下一条指向虚空的注册表值）；
    * 它指向的是不是**当前这个目录**里的程序（用户装了两份时，开机启动的
      可能是另一份，改这边的配置当然没反应）。
    """
    mode, tokens = _autostart_tokens()
    if mode == "none" or not tokens:
        return ""
    exe = tokens[0]
    if not exe:
        return ""
    if not Path(exe).exists():
        return (f"记下的 {Path(exe).name} 已经不在了（{exe}）—— "
                "开机时它启动不起来")
    if getattr(sys, "frozen", False):
        # 打包版的自启项就是「exe + 参数」，没有 main.py 参与。只要那个 exe 还
        # 在，并且就是当前这一份，就算能跑（换个位置摆放的副本会在这里被认出来）。
        try:
            same = (Path(exe).resolve() == Path(sys.executable).resolve())
        except OSError:                       # pragma: no cover
            same = False
        if not same:
            return (f"记的是另一个位置的程序（{Path(exe).resolve()}）—— "
                    "开机启动的会是那边那份，不是当前这个目录里的这份")
        return ""
    script = next((t for t in tokens[1:] if t.lower().endswith(".py")), "")
    if not script:
        return "记下的命令行里找不到 main.py —— 开机时它什么也启动不了"
    try:
        got = Path(script).resolve()
    except OSError:                          # pragma: no cover - 只在路径怪到读不了时
        got = Path(script)
    want = (Path(root_dir) / "main.py").resolve()
    if got != want:
        return (f"记的是另一个位置的程序（{got}）—— "
                "开机启动的会是那边那份，不是当前这个目录里的这份")
    return ""


def autostart_enabled(root_dir: Path) -> bool | None:  # 兼容旧调用
    return autostart_status() != "none"


def set_autostart(enable: bool, root_dir: Path,
                  argv: list[str] | None = None) -> tuple[bool, str]:
    """开机自动常驻 / 取消。

    先试注册表 ``HKCU\\...\\Run``（首选：不落文件，系统的「启动应用」列表里也看得见）；
    写不进去就退回往「启动」文件夹里放一个 ``.cmd``。两条路都记着失败原因，
    失败时把**真实原因**返回给调用方 —— 只报一句「失败」等于让用户去猜。

    ``argv`` 是用户本次敲的命令行：``--config`` / ``--log`` 会跟着写进自启项，
    否则开机起来的实例会悄悄换回默认配置（见 :func:`autostart_extra`）。
    """
    root = Path(root_dir)

    if not enable:
        # 「空白的 .cmd 残留」也要顺手清掉：状态会说 none，但文件还躺在启动文件夹里，
        # 留着它只会让下一个人以为自启开着（见 _startup_script_ok 的说明）。
        leftover = startup_script_path().exists()
        if autostart_status() == "none" and not leftover:
            return True, "本来就没有开启"
        problems = []
        ok, why = _registry_autostart(False, None)
        if not ok:
            problems.append(f"注册表：{why}")
        ok2, why2 = _startup_folder_autostart(False, None)
        if not ok2:
            problems.append(f"启动文件夹：{why2}")
        # 只要真的没了就算成功，不管哪条路报过错
        if autostart_status() == "none":
            return True, "已取消"
        return False, "；".join(problems) or "取消后仍然检测到自启项"

    cmd = autostart_command(root, argv)
    if not cmd:
        return False, "找不到 pythonw.exe，拼不出开机自启的命令行"

    ok, why_reg = _registry_autostart(True, cmd)
    if ok:
        return True, f"注册表 HKCU\\{AUTOSTART_KEY}\\{AUTOSTART_NAME}"

    ok2, why_dir = _startup_folder_autostart(True, cmd)
    if ok2:
        return True, f"启动文件夹 {startup_script_path()}"

    return False, f"注册表：{why_reg}；启动文件夹：{why_dir}"


# ---------------------------------------------------------------------- #
# 运行中的那个实例：状态公布 + 远程下命令
#
# 为什么要有这套东西：托盘图标可能装不上（低完整性进程会被 UIPI 拦掉，
# 见 t_ime/tray.py 的说明），而它本来是唯一能「退出 / 挂起 / 换语言」的入口。
# 所以常驻实例把自己每隔一会儿写进一个状态文件，并把命令文件当成收件箱；
# 第二个进程（用户又双击了一次 run.bat）就能读到真实状态、并把命令投进去。
# 文件很小、只在状态变化时写，代价可以忽略。
# ---------------------------------------------------------------------- #
STATUS_NAME = "translate-ime.status.json"
CTL_NAME = "translate-ime.ctl"


def status_path(root_dir: Path) -> Path:
    return Path(root_dir) / STATUS_NAME


def ctl_path(root_dir: Path) -> Path:
    return Path(root_dir) / CTL_NAME


def write_status(root_dir: Path, data: dict) -> bool:
    """把当前状态写出去，供第二个进程读。

    先写临时文件再替换：否则第二个进程可能正好读到写了一半的 JSON。
    """
    import json

    path = status_path(root_dir)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def read_status(root_dir: Path) -> dict | None:
    import json

    try:
        # utf-8-sig：状态文件是我们自己写的（没有 BOM），但用户可能用记事本
        # 或 PowerShell 打开过并顺手保存 —— 那些工具有时会塞一个 BOM 进去。
        raw = status_path(root_dir).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def clear_status(root_dir: Path) -> None:
    for p in (status_path(root_dir), ctl_path(root_dir)):
        try:
            p.unlink()
        except OSError:
            pass


def status_age(root_dir: Path) -> float | None:
    """状态文件距今多少秒没更新了。读不到（不存在 / 没有权限）返回 None。

    单独有这个函数是因为「超过 10 秒没更新」这句话本身没告诉用户任何东西：
    10.2 秒和 10 分钟是完全不同的两件事，而 mtime 之外的判据（进程还在不在）
    体检那边已经单独查过了（``W.process_name(pid)``）。
    """
    try:
        return max(0.0, time.time() - status_path(root_dir).stat().st_mtime)
    except OSError:
        return None


def status_is_stale(root_dir: Path, max_age: float = STATUS_STALE_SECONDS) -> bool:
    """状态文件太旧 = 那个实例多半已经死了（被强杀时来不及清理）。

    ``max_age`` 的默认值是 ``STATUS_STALE_SECONDS``（两个心跳），不是随便一个
    10 秒：常驻实例每 ``STATUS_HEARTBEAT_SECONDS`` 秒至少写一次状态文件，
    所以「两个心跳都没来」才说明它真的不转了 —— 空闲的实例照样是活的。
    """
    age = status_age(root_dir)
    return True if age is None else age > max_age


def send_command(root_dir: Path, cmd: str) -> bool:
    """把一条命令投进收件箱。追加写，所以连点两下不会互相覆盖。"""
    try:
        with open(ctl_path(root_dir), "a", encoding="utf-8") as fh:
            fh.write(cmd.strip() + "\n")
        return True
    except OSError:
        return False


def take_commands(root_dir: Path) -> list[str]:
    """取走收件箱里的所有命令（取完即清空）。

    用 ``utf-8-sig`` 读：我们自己写的时候不带 BOM，但用户完全可能用记事本或
    ``Out-File -Encoding utf8`` 手工往里写一条命令 —— 那两种都会在开头塞一个
    ``\\ufeff``，于是命令名变成 ``'\\ufeffquit'``，看起来就是「命令没反应」。
    第一行那个隐形字符不值得让人查半天。

    第 64 轮修的一个竞态：以前是「读完再删文件」。``send_command()`` 用追加写，
    它 ``open()`` 之后文件就已经存在、但内容还没落盘；这时候实例刚好来取，就会
    读到空字符串、把文件删掉，写入方随后的内容写进一个**已经被删掉的文件**里 ——
    那条命令（可能是 ``quit``）就凭空消失了。真机表现是「点了退出，输入法还在」，
    全量回归里偶发过一次 ``test_quote_live`` 的「quit 之后实例真的走了」失败。

    现在改成**先用 ``os.replace`` 把文件原子地改名抢走，再读改名后的那份**：
    Windows 上写入方还开着句柄时改名会失败（共享冲突），这一拍就什么都不做，
    下一拍再来 —— 命令一定还在原处，不会丢。
    """
    path = ctl_path(root_dir)
    if not path.exists():
        return []
    claimed = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        os.replace(path, claimed)
    except OSError:
        # 写入方还开着句柄（或者别人刚好抢先取走），下一拍再来，命令不丢
        return []
    try:
        raw = claimed.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        raw = ""
    finally:
        try:
            claimed.unlink()
        except OSError:
            pass
    return [ln.strip().lstrip("\ufeff") for ln in raw.splitlines() if ln.strip()]


def discard_commands(root_dir: Path) -> list[str]:
    """丢掉收件箱里残留的命令，并把丢掉的内容返回（崩溃恢复用）。

    和 :func:`take_commands` 的机制一样（读出来 + 删文件），区别只在**谁来调**、
    **语义是什么**：``take_commands()`` 是给*正在跑的实例*取命令的，而这个函数是给
    *刚启动的实例*用的。

    为什么必须有这一步：正常退出时 :func:`clear_status` 会把状态文件和收件箱一起清掉。
    但上一个实例如果是被**强杀**（``taskkill /F``、崩溃、注销、断电）而不是正常退出，
    ``clear_status()`` 根本没机会跑，收件箱里就可能还留着一条 ``quit``。新实例如果
    照单全收，第一条命令就让它当场退出 —— 用户看到的现象是「双击了 run.bat，可是
    输入法压根没起来」（没有报错、没有窗口、日志里只有一行 quit）。

    命令是发给**上一个进程**的，属于上一个生命周期，所以启动时一律丢掉，并且把内容
    返回给调用方写进日志（让这件事有据可查，而不是悄悄消失）。
    """
    return take_commands(root_dir)

