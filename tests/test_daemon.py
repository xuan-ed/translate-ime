# -*- coding: utf-8 -*-
"""后台常驻层的回归测试：日志、脱离控制台、单实例、开机自启。

这里的重点是**不依赖环境特权**：注册表/启动文件夹写不进去时，
要断言「失败原因说得清楚」，而不是笼统的一句「失败」。
用户在被降权的环境里跑时，这条信息就是他唯一的线索。

直接跑：``python tests/test_daemon.py``
不会真的开启开机自启 —— 只做只读探测和失败路径。
"""
import contextlib
import io
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

from t_ime import daemon  # noqa: E402
from t_ime import winapi as W  # noqa: E402

PASS = 0
FAIL = 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"[PASS] {name}" + (f"  —— {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"[FAIL] {name}" + (f"  —— {extra}" if extra else ""))


print("=" * 72)
print("  1. 日志路径解析")
print("=" * 72)

p = daemon.resolve_log_path(ROOT)
check("默认路径落在项目根目录下，文件名是 translate-ime.log",
      p == ROOT / "translate-ime.log", str(p))

p = daemon.resolve_log_path(ROOT, "custom.log")
check("命令行相对路径相对项目根解析", p == ROOT / "custom.log", str(p))

abs_target = Path(tempfile.gettempdir()) / "abs-ime.log"
p = daemon.resolve_log_path(ROOT, str(abs_target))
check("命令行绝对路径原样使用", p == abs_target, str(p))

cfg = {"output": {"log_file": "from-config.log"}}
p = daemon.resolve_log_path(ROOT, None, cfg)
check("配置里的 log_file 生效", p == ROOT / "from-config.log", str(p))

p = daemon.resolve_log_path(ROOT, "cli.log", cfg)
check("命令行优先级高于配置", p == ROOT / "cli.log", str(p))

cfg_empty = {"output": {"log_file": ""}}
p = daemon.resolve_log_path(ROOT, None, cfg_empty)
check("配置为空串时回退到默认名", p == ROOT / daemon.DEFAULT_LOG_NAME, str(p))

# 第 67 轮：--log 指到目录时，以前会把目录本身当成日志文件打开 —— 子进程在
# _LogFile.__init__ 抛 PermissionError，启动器却怪「依赖没装」。
tmp_logdir = ROOT / ".tmp" / "daemon-logdir"
tmp_logdir.mkdir(parents=True, exist_ok=True)
p = daemon.resolve_log_path(ROOT, str(tmp_logdir))
check("--log 指向已存在的目录时，日志落在里面的默认文件名",
      p == tmp_logdir / daemon.DEFAULT_LOG_NAME, str(p))
p = daemon.resolve_log_path(ROOT, str(tmp_logdir) + os.sep)
check("末尾带分隔符也按目录处理（哪怕目录还不存在）",
      p == tmp_logdir / daemon.DEFAULT_LOG_NAME, str(p))
p = daemon.resolve_log_path(ROOT, None, {"output": {"log_file": str(tmp_logdir)}})
check("output.log_file 指向目录时同样补默认文件名",
      p == tmp_logdir / daemon.DEFAULT_LOG_NAME, str(p))
tmp_logdir.rmdir()
src_daemon = (ROOT / "t_ime" / "daemon.py").read_text(encoding="utf-8")
check("后台成功提示跟着 --log 走，不再写死「项目目录下的 translate-ime.log」",
      "日志见项目目录下的 translate-ime.log" not in src_daemon)
check("失败提示不再只怪依赖，也指出 --log 写不进去",
      "--log / output.log_file 指的位置写不进去" in src_daemon)

print()
print("=" * 72)
print("  2. 日志文件对象：时间戳、换行、reconfigure")
print("=" * 72)

tmpdir = ROOT / ".tmp"
tmpdir.mkdir(exist_ok=True)
log_path = tmpdir / "_test_daemon.log"
if log_path.exists():
    log_path.unlink()

log = daemon._LogFile(log_path)
log.write("第一行\n")
log.write("跨两行\n第二半\n")
log.write("没有换行结尾")
log.flush()
log.close()

text = log_path.read_text(encoding="utf-8")
lines = text.split("\n")
check("每一行都盖了 [YYYY-MM-DD HH:MM:SS] 时间戳",
      all(l.startswith("[") and l[20:22] == "] " for l in lines if l),
      lines[0][:28] if lines else "")
check("换行被正确拆行（4 行）", len([l for l in lines if l]) == 4,
      f"{len([l for l in lines if l])} 行")
check("行内容没被吃掉", "第一行" in text and "跨两行" in text and "第二半" in text)
check("末尾不补换行", not text.endswith("\n"))

log2 = daemon._LogFile(log_path)
check("reconfigure 可被无脑调用（t_ime.console 会调它）",
      log2.reconfigure(encoding="utf-8") is None)
check("isatty() 为 False", log2.isatty() is False)
check("encoding 是 utf-8", log2.encoding == "utf-8")
log2.close()

# 追加而不是覆盖
log3 = daemon._LogFile(log_path)
log3.write("追加的一行\n")
log3.close()
check("重复打开是追加模式", "追加的一行" in log_path.read_text(encoding="utf-8"))

print()
print("=" * 72)
print("  3. 日志轮转")
print("=" * 72)

rot = tmpdir / "_test_rotate.log"
bak = rot.with_suffix(rot.suffix + ".1")
for f in (rot, bak):
    if f.exists():
        f.unlink()

rot.write_bytes(b"x" * (daemon.LOG_MAX_BYTES + 10))
daemon._rotate(rot)
check("超过上限的文件被改名成 .1", bak.exists() and not rot.exists(),
      f"原文件 {rot.exists()} / 备份 {bak.exists()}")

rot.write_bytes(b"y" * 100)
daemon._rotate(rot)
check("没超上限就不轮转", rot.exists() and rot.stat().st_size == 100)

rot.write_bytes(b"z" * (daemon.LOG_MAX_BYTES + 10))
daemon._rotate(rot)
check("再次轮转时旧的 .1 被替换掉", bak.exists() and bak.stat().st_size > daemon.LOG_MAX_BYTES)
for f in (rot, bak):
    if f.exists():
        f.unlink()

print()
print("=" * 72)
print("  4. setup_file_logging")
print("=" * 72)

swap_path = tmpdir / "_test_swap.log"
if swap_path.exists():
    swap_path.unlink()
saved_out, saved_err = sys.stdout, sys.stderr
try:
    handle = daemon.setup_file_logging(swap_path, verbose=True)
    print("这条应该同时出现在屏幕和文件里")
    sys.stdout.flush()
    sys.stdout = saved_out
    sys.stderr = saved_err
    body = swap_path.read_text(encoding="utf-8")
    check("stdout 被换成了 _LogFile", isinstance(handle, daemon._LogFile))
    check("启动横幅写进了日志", "translate-ime 启动 pid=" in body)
    check("重定向后的 print 也进了日志", "这条应该同时出现在屏幕和文件里" in body)
    handle.close()
except Exception as exc:  # pragma: no cover
    sys.stdout, sys.stderr = saved_out, saved_err
    check("setup_file_logging 不抛异常", False, repr(exc))
finally:
    sys.stdout, sys.stderr = saved_out, saved_err
    # 收尾：把真实的 stdout 还原（`fix_console_encoding` 已经调过，不用重来）

keep_path = tmpdir / "_test_keep.log"
if keep_path.exists():
    keep_path.unlink()
saved_out, saved_err = sys.stdout, sys.stderr
try:
    h2 = daemon.setup_file_logging(keep_path, keep_console=True)
    check("keep_console=True 时 stdout 是 Tee（控制台 + 文件）",
          type(sys.stdout).__name__ == "_Tee", type(sys.stdout).__name__)
    print("tee 测试行")
    sys.stdout.flush()
    body2 = keep_path.read_text(encoding="utf-8")
    check("Tee 把内容写进了日志文件", "tee 测试行" in body2)
    h2.close()
except Exception as exc:  # pragma: no cover
    check("keep_console 路径不抛异常", False, repr(exc))
finally:
    sys.stdout, sys.stderr = saved_out, saved_err

print()
print("=" * 72)
print("  5. 脱离控制台 / 开机自启的命令行")
print("=" * 72)

pyw = daemon.pythonw_path()
check("找得到 pythonw.exe（没有它就脱离不了控制台）", pyw is not None and pyw.exists(),
      str(pyw))

cmd = daemon.background_command(ROOT, ["--background", "--verbose"])
check("background_command 用 pythonw 起头", cmd is not None and "pythonw" in cmd[0].lower(),
      cmd[0] if cmd else "None")
check("background_command 带上 main.py", cmd is not None and cmd[1].endswith("main.py"))
check("原有的 --background/--detached 被剔重，只留一组",
      cmd is not None and cmd.count("--background") == 1 and cmd.count("--detached") == 1,
      " ".join(cmd[2:]) if cmd else "")
check("其它参数原样透传", cmd is not None and "--verbose" in cmd)

auto = daemon.autostart_command(ROOT)
check("autostart_command 每个参数都加引号",
      auto is not None and auto.startswith('"') and auto.count('"') >= 6, auto)
check("autostart_command 里没有残留的 --detached 重复",
      auto is not None and auto.count("--detached") == 1)

print()
print("=" * 72)
print("  6. 启动文件夹与自启状态探测")
print("=" * 72)

sd = daemon.startup_dir()
check("启动文件夹路径以 Startup 结尾", sd.name == "Startup", str(sd))
check("启动文件夹路径是绝对路径", sd.is_absolute())
check("脚本名是 translate-ime.cmd", daemon.startup_script_path().name == "translate-ime.cmd")

state = daemon.autostart_status()
check("autostart_status 返回三种已知取值之一",
      state in ("registry", "startup-folder", "none"), state)
check("autostart_enabled 与 status 一致",
      daemon.autostart_enabled(ROOT) == (state != "none"), state)

print()
print("=" * 72)
print("  7. 失败原因必须说人话（这是被降权环境下唯一的线索）")
print("=" * 72)

e5 = OSError(5, "Access is denied")
e5.winerror = 5
msg5 = daemon._explain(e5)
check("WinError 5 说清是权限/完整性级别问题",
      "完整性级别" in msg5 and "拒绝访问" in msg5, msg5[:40] + "…")

e13 = PermissionError(13, "Permission denied")
msg13 = daemon._explain(e13)
check("errno 13 (EACCES) 走同一条解释", "完整性级别" in msg13, msg13[:40] + "…")

e2 = OSError(2, "not found")
e2.winerror = 2
check("WinError 2 报路径不存在", "不存在" in daemon._explain(e2))

e999 = OSError(999, "weird")
e999.winerror = 999
check("未知错误码也带上异常类型和原文",
      "OSError" in daemon._explain(e999) and "weird" in daemon._explain(e999))

# 只读探测：此刻没开自启的话，取消应当被识别成「本来就没了」，而不是报错
if state == "none":
    ok, detail = daemon.set_autostart(False, ROOT)
    check("没开自启时取消 == 幂等成功", ok and "本来" in detail, detail)
else:
    print(f"[SKIP] 当前环境已经有自启项（{state}），不动它")

# 开启：本会话被降权，应当失败**且把两条路的真实原因都带上**
# （在有权限的环境里会成功，那时不写注册表就没法测，所以两种结果都算通过）
ok, detail = daemon.set_autostart(True, ROOT) if os.environ.get("T_IME_TEST_AUTOSTART") else (None, "")
if ok is None:
    print("[SKIP] 未设置 T_IME_TEST_AUTOSTART，跳过「开启自启」的实写测试")
else:
    check("开启自启：要么成功，要么失败原因具体可读",
          ok or ("注册表" in detail or "pythonw" in detail), f"{ok} / {detail[:60]}")
    if ok:
        daemon.set_autostart(False, ROOT)

print()
print("=" * 72)
print("  8. 单实例互斥体")
print("=" * 72)

tok1 = W.single_instance("translate-ime-selftest")
check("第一次取互斥体返回 True", tok1 is True)
check("同一进程内重复索取不再拦自己（幂等）",
      W.single_instance("translate-ime-selftest") is True)

# 真正的单实例语义必须跨进程验证：另起一个进程应该拿不到同一个锁
import subprocess  # noqa: E402

child = (
    "import sys; sys.path.insert(0, r'{root}');"
    "from t_ime import winapi as W;"
    "print('SECOND=' + str(W.single_instance('translate-ime-selftest')))"
).format(root=ROOT)
out = subprocess.run([sys.executable, "-c", child],
                     capture_output=True, text=True, errors="replace")
check("另一个进程拿不到同名互斥体（这就是单实例的实现）",
      "SECOND=False" in out.stdout,
      out.stdout.strip() or (out.stderr or "").strip()[-90:])

tok3 = W.single_instance("translate-ime-selftest-2")
check("不同名字互不干扰", tok3 is True)

# instance_exists 是「只看不占」，和 single_instance 的语义必须能分开 ——
# 启动器就是靠这个才敢在派生子进程之前先探一下有没有实例在跑。
check("instance_exists() 探测到别人占着的锁",
      W.instance_exists("translate-ime-selftest") is True)
check("instance_exists() 对没人用的名字返回 False",
      W.instance_exists("translate-ime-nobody-holds-this") is False)
check("instance_exists() 会立刻把句柄还回去（连探 50 次不留痕）",
      all(W.instance_exists("translate-ime-probe-%d" % i) is False
          for i in range(50)))

print()
print("=" * 72)
print("  启动时丢掉上一个实例残留的命令（崩溃恢复）")
print("=" * 72)
print("上一个实例被 taskkill / 崩溃 / 断电干掉时，收件箱里的 quit 会留下来；")
print("新实例如果照常取命令，就会当场自杀 —— 表现是「双击了但没起来」。")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    ctl = daemon.ctl_path(tmp)

    check("没有收件箱时丢掉操作也不报错", daemon.discard_commands(tmp) == [])
    check("丢掉不存在的收件箱不会凭空造出一个文件", not ctl.exists())

    daemon.send_command(tmp, "toggle_enabled")
    daemon.send_command(tmp, "quit")
    check("收件箱里确实躺着两条残留命令", ctl.exists())
    stale = daemon.discard_commands(tmp)
    check("discard_commands 把残留命令原样交出来（好写进日志）",
          stale == ["toggle_enabled", "quit"], str(stale))
    check("丢完之后收件箱文件消失了", not ctl.exists())
    check("再丢一次是空的（幂等）", daemon.discard_commands(tmp) == [])

    # 用户完全可能用记事本 / PowerShell 手写这个文件，那会带上 BOM
    ctl.write_bytes(b"\xef\xbb\xbfquit\n")
    check("带 BOM 的手写收件箱也能认出来", daemon.discard_commands(tmp) == ["quit"])
    check("带 BOM 的那份同样被删掉了", not ctl.exists())

print()
print("=" * 72)
print("  10. 后台重拉：--config / --log 必须钉成绝对路径")
print("=" * 72)
print("子进程的 cwd 固定是项目根，用户却可能从别处敲命令。相对路径在父子两边")
print("解析成不同文件时，实例会**悄悄用默认配置**，而父进程刚报过「配置来自 …」。")

import main as _main  # noqa: E402

check("cli_value 认 --config=PATH", daemon.cli_value(["--config=a.json"], "--config") == "a.json")
check("cli_value 认 --config PATH", daemon.cli_value(["--config", "a.json"], "--config") == "a.json")
check("cli_value 没这个选项时返回 None", daemon.cli_value(["--check"], "--config") is None)
check("_log_arg 认 --log=PATH", daemon._log_arg(["--log=x.log"]) == "x.log")
check("_log_arg 认 --log PATH", daemon._log_arg(["--log", "y.log"]) == "y.log")

with tempfile.TemporaryDirectory() as td:
    alt = Path(td) / "alt.json"
    alt.write_text('{"hotkeys": {"quit": "ctrl+alt+f9"}}', encoding="utf-8")
    got = daemon.quit_hotkey_text(ROOT, [f"--config={alt}"])
    check("退出热键提示也认 --config=PATH（与实例读同一份配置）",
          got == "Ctrl+Alt+F9", got)

    # 第 77 轮：hotkeys.quit = "q" 这种没有修饰键的打字键，核心会拒绝注册 ——
    # 启动提示不能再报「按 Q 退出」，得说清楚为什么没这个键。
    bad = Path(td) / "bad.json"
    bad.write_text('{"hotkeys": {"quit": "q"}}', encoding="utf-8")
    check("裸字母的退出热键不再被当成可用键播报",
          daemon.quit_hotkey_text(ROOT, [f"--config={bad}"]) == "",
          daemon.quit_hotkey_text(ROOT, [f"--config={bad}"]))
    _why = daemon.quit_hotkey_problem(ROOT, [f"--config={bad}"])
    check("并且能说出原因（没有修饰键）", "没有修饰键" in _why, _why)
    check("正常热键没有被误判成问题",
          daemon.quit_hotkey_problem(ROOT, [f"--config={alt}"]) == "",
          daemon.quit_hotkey_problem(ROOT, [f"--config={alt}"]))
    _src = (ROOT / "t_ime" / "daemon.py").read_text(encoding="utf-8")
    check("启动失败/成功提示里会用这句原因（不是又写死一个键）",
          "hotkeys.quit 配的写法不能用" in _src, "")

    # 第 84 轮：shift+q —— Shift 单独出现就是「打大写字母」，核心同样拒绝注册；
    # 而 ctrl+q+w 这种一个串里两个主键的写法，以前只注册最后一个，播报却照抄原文。
    shifty = Path(td) / "shifty.json"
    shifty.write_text('{"hotkeys": {"quit": "shift+q"}}', encoding="utf-8")
    check("shift+q 不再被当成可用键播报",
          daemon.quit_hotkey_text(ROOT, [f"--config={shifty}"]) == "",
          daemon.quit_hotkey_text(ROOT, [f"--config={shifty}"]))
    _why = daemon.quit_hotkey_problem(ROOT, [f"--config={shifty}"])
    check("并且说出「只有 Shift 一个修饰键」", "只有 Shift" in _why, _why)

    twokey = Path(td) / "twokey.json"
    twokey.write_text('{"hotkeys": {"quit": "ctrl+q+w"}}', encoding="utf-8")
    check("一个串里两个主键时不再播报任何键",
          daemon.quit_hotkey_text(ROOT, [f"--config={twokey}"]) == "",
          daemon.quit_hotkey_text(ROOT, [f"--config={twokey}"]))
    _why = daemon.quit_hotkey_problem(ROOT, [f"--config={twokey}"])
    check("原因说成「解析不了」而不是静默", "解析不了" in _why, _why)

pinned = _main._pin_cli_paths(
    ["--background", "--config", "rel.json", "--log=rel.log", "--verbose"],
    Path("sub") / "rel.json", Path("sub") / "rel.log")
check("--config 的值被换成绝对路径",
      pinned[pinned.index("--config") + 1].endswith("rel.json")
      and Path(pinned[pinned.index("--config") + 1]).is_absolute(), str(pinned))
check("--config 的相对父目录展开成当前 cwd",
      Path(pinned[pinned.index("--config") + 1]).parent == (Path.cwd() / "sub").resolve(),
      pinned[pinned.index("--config") + 1])
check("--log= 的形式也被改写成绝对路径",
      [t for t in pinned if t.startswith("--log=")][0][len("--log="):] ==
      str((Path.cwd() / "sub" / "rel.log").resolve()), str(pinned))
check("其余参数原样保留", "--verbose" in pinned and "--background" in pinned, str(pinned))
check("命令行里没写 --config 就不硬塞",
      _main._pin_cli_paths(["--background"], ROOT / "config.json", ROOT / "x.log")
      == ["--background"])

# 端到端：打桩 spawn_background，确认 main() 交出去的就是绝对路径。
# 不打桩会真的拉起一个 pythonw 实例，测试就成了「跑一次输入法」。
import t_ime.daemon as _d_mod  # noqa: E402

seen: list[tuple[list[str], Path | None]] = []
_orig_spawn = _d_mod.spawn_background


def _fake_spawn(root_dir, argv, log_path=None):  # noqa: ANN001
    seen.append((list(argv), log_path))
    return 0


_d_mod.spawn_background = _fake_spawn
# 测试机上也完全可能正跑着用户自己的实例：那会让 main() 走「第二次启动 → 控制面板」
# 而不是后台分支，甚至停在交互菜单上等输入。显式把「已经有实例」按下去。
_orig_exists = W.instance_exists
W.instance_exists = lambda *a, **k: False
try:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "alt.json").write_text('{"hotkeys": {"quit": "ctrl+alt+f9"}}', encoding="utf-8")
        (tmp / "cfglog.json").write_text('{"output": {"log_file": "fromcfg.log"}}',
                                         encoding="utf-8")
        old = Path.cwd()
        os.chdir(tmp)
        try:
            rc_ = _main.main(["--background", "--config", "alt.json"])
            rc_log = _main.main(["--background", "--config", "cfglog.json"])
            rc_cli = _main.main(["--background", "--config", "cfglog.json",
                                 "--log", "cli.log"])
        finally:
            os.chdir(old)
    check("打桩下 main(--background) 退出 0", rc_ == 0, f"rc={rc_}")
    got_argv = seen[0][0] if seen else []
    got_cfg = Path(got_argv[got_argv.index("--config") + 1]) if got_argv else Path("")
    check("交给 spawn 的 argv 里 --config 已是绝对路径",
          bool(got_argv) and got_cfg.is_absolute() and got_cfg == (tmp / "alt.json").resolve(),
          str(seen))
    # 第 70 轮：父进程算出的日志路径必须一路传下去，而不是让 spawn 自己重算。
    # 配置里的相对路径是相对**项目根**解析的（不是相对配置文件所在目录）。
    check("配置里的 output.log_file 被交给 spawn（不是 translate-ime.log）",
          rc_log == 0 and seen[1][1] == ROOT / "fromcfg.log", str(seen[1][1]))
    check("命令行 --log 优先级最高，交过去的也是它",
          rc_cli == 0 and seen[2][1] == ROOT / "cli.log", str(seen[2][1]))
    check("没配 log_file、没带 --log 时默认项目根的 translate-ime.log",
          seen[0][1] == ROOT / "translate-ime.log", str(seen[0][1]))
finally:
    W.instance_exists = _orig_exists
    _d_mod.spawn_background = _orig_spawn

# 第 93 轮：spawn 说「没就绪」时，main.py 不许再说「已在后台启动」并返回 0。
print()
_buf93 = io.StringIO()
_err93 = io.StringIO()
_orig_spawn93 = _d_mod.spawn_background
_orig_exists93 = W.instance_exists
_d_mod.spawn_background = lambda root, argv, log_path=None: _d_mod.SPAWN_NOT_READY
W.instance_exists = lambda *a, **k: False
try:
    with contextlib.redirect_stdout(_buf93), contextlib.redirect_stderr(_err93):
        rc_93 = _main.main(["--background"])
finally:
    _d_mod.spawn_background = _orig_spawn93
    W.instance_exists = _orig_exists93
_said93 = _buf93.getvalue() + _err93.getvalue()
check("没就绪时 main(--background) 返回 4（不是 0）", rc_93 == _d_mod.SPAWN_NOT_READY,
      f"rc={rc_93}")
check("没就绪时 main 不许说「已在后台启动」",
      "已在后台启动" not in _said93, _said93[-300:])
check("没就绪时 main 要说清「这段时间敲的字不会被接管」",
      "还没就绪" in _said93 and "不会被接管" in _said93, _said93[-300:])

# 第 106 轮：同时点两下 run.bat（或自启与手动双击撞车）时，抢不到互斥体的那个
# 子进程按单实例约定退出 3。父进程以前只会翻译成「退出码 3 → 后台启动失败」，
# run.bat 跟着 pause —— 可输入法其实好好地常驻着。
print()
with tempfile.TemporaryDirectory() as td:
    race_root = Path(td)
    race_log = race_root / "race.log"
    _orig_cmd106 = _d_mod.background_command
    _orig_running106 = _d_mod.is_running
    _orig_status106 = _d_mod.read_status
    _d_mod.background_command = lambda root, a: [sys.executable, "-c",
                                                 "import sys; sys.exit(3)"]
    _d_mod.is_running = lambda *a, **k: True
    _d_mod.read_status = lambda root: {"pid": 4242}
    buf106 = io.StringIO()
    err106 = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf106), contextlib.redirect_stderr(err106):
            rc_race = _d_mod.spawn_background(race_root, [], log_path=race_log)
    finally:
        _d_mod.background_command = _orig_cmd106
        _d_mod.is_running = _orig_running106
        _d_mod.read_status = _orig_status106
    said_race = buf106.getvalue() + err106.getvalue()
    check("子进程退出 3 且有实例在跑 → spawn 返回 SPAWN_ALREADY_RUNNING，不是失败",
          rc_race == _d_mod.SPAWN_ALREADY_RUNNING, f"rc={rc_race}")
    check("这时要说「已经有一个在跑了」并带 pid",
          "已经有一个 translate-ime 在跑了" in said_race and "4242" in said_race,
          said_race[-300:])
    check("不许把它说成「启动后立刻退出」或「后台启动失败」",
          "启动后立刻退出" not in said_race and "启动失败" not in said_race,
          said_race[-300:])
    check("也不许把「依赖没装 / 索引建不出来」搬出来误导",
          "依赖没装" not in said_race and "连日志都没写出来" not in said_race,
          said_race[-300:])

    # 同样的退出码 3，但系统里其实没有实例（不是「已经在跑」那种原因）：
    # 那仍然是真失败，不许被这条新分支吞掉。
    buf_fail = io.StringIO()
    err_fail = io.StringIO()
    _d_mod.background_command = lambda root, a: [sys.executable, "-c",
                                                 "import sys; sys.exit(3)"]
    _d_mod.is_running = lambda *a, **k: False
    try:
        with contextlib.redirect_stdout(buf_fail), contextlib.redirect_stderr(err_fail):
            rc_fail = _d_mod.spawn_background(race_root, [], log_path=race_log)
    finally:
        _d_mod.background_command = _orig_cmd106
        _d_mod.is_running = _orig_running106
    said_fail = buf_fail.getvalue() + err_fail.getvalue()
    check("没有实例时退出码 3 仍按真失败报（返回 1）", rc_fail == 1, f"rc={rc_fail}")
    check("真失败仍然说「启动后立刻退出（退出码 3）」",
          "启动后立刻退出（退出码 3）" in said_fail, said_fail[-300:])

# main.py 那一层：spawn 说「已经在跑」时，不许再宣布「已在后台启动」。
print()
_buf106 = io.StringIO()
_err106 = io.StringIO()
_orig_spawn106 = _d_mod.spawn_background
_orig_exists106 = W.instance_exists
_d_mod.spawn_background = lambda root, argv, log_path=None: _d_mod.SPAWN_ALREADY_RUNNING
W.instance_exists = lambda *a, **k: False
try:
    with contextlib.redirect_stdout(_buf106), contextlib.redirect_stderr(_err106):
        rc_main106 = _main.main(["--background"])
finally:
    _d_mod.spawn_background = _orig_spawn106
    W.instance_exists = _orig_exists106
_said106 = _buf106.getvalue() + _err106.getvalue()
check("已经有一份在跑时 main(--background) 返回 0（不让 run.bat pause）",
      rc_main106 == 0, f"rc={rc_main106}")
check("这时不许说「已在后台启动」（那不是这一次启动的）",
      "已在后台启动" not in _said106, _said106[-300:])
check("要告诉用户「再双击一次会弹控制面板」",
      "再双击一次 run.bat" in _said106, _said106[-300:])
check("也不许说「后台启动失败」", "后台启动失败" not in _said106, _said106[-300:])
_src106 = (ROOT / "main.py").read_text(encoding="utf-8")
check("main.py 确实在分派这个退出码（不是只靠注释）",
      "SPAWN_ALREADY_RUNNING" in _src106, "")

# 真调一次（子进程只是睡一下）：父进程报的「日志见 …」必须是传进去的那份。
print()
with tempfile.TemporaryDirectory() as td:
    tmp_root = Path(td)
    tmp_log = tmp_root / "custom.log"
    _orig_cmd = _d_mod.background_command
    _orig_ready = _d_mod.READY_TIMEOUT_SECONDS
    _d_mod.background_command = lambda root, a: [sys.executable, "-c",
                                                 "import time; time.sleep(0.25)"]
    # 这个桩子永远不会写状态文件，等就绪会白等 6 秒 —— 测试里缩短到 0.4 秒。
    _d_mod.READY_TIMEOUT_SECONDS = 0.4
    buf = io.StringIO()
    err_buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err_buf):
            spawn_rc = _d_mod.spawn_background(tmp_root, [], log_path=tmp_log)
    finally:
        _d_mod.background_command = _orig_cmd
        _d_mod.READY_TIMEOUT_SECONDS = _orig_ready
    said = buf.getvalue()
    said_all = said + err_buf.getvalue()
    check("健康子进程活着但没有状态文件 → spawn 返回 SPAWN_NOT_READY(4)，不是 0",
          spawn_rc == _d_mod.SPAWN_NOT_READY, f"rc={spawn_rc}")
    check("「日志见 …」用的是调用方传来的路径", str(tmp_log) in said, said[-200:])
    check("不再凭空报 translate-ime.log", "translate-ime.log" not in said, said[-200:])
    check("报路径不等于真去创建文件", not tmp_log.exists())
    # 第 88 轮：进程活着 ≠ 能打字。没等到 hook=True 就只许说「还没就绪」。
    # 第 93 轮：光说还不够 —— 得用退出码把「没就绪」交给上层，否则 main.py 会
    # 接着打印「已在后台启动」、run.bat 当作成功安静退出。
    check("子进程没报到就绪时，父进程如实说「还没就绪」而不是宣布可以打字",
          "还没就绪" in said_all and "已就绪" not in said_all, said_all[-300:])

# 第 88 轮：wait_ready() 只认「状态文件新鲜 + hook=True」。
print()
with tempfile.TemporaryDirectory() as td:
    ready_root = Path(td)
    t0 = time.monotonic()
    missing = _d_mod.wait_ready(ready_root, timeout=0.3)
    waited_missing = time.monotonic() - t0
    _d_mod.write_status(ready_root, {"hook": False, "pid": 1})
    t0 = time.monotonic()
    not_hook = _d_mod.wait_ready(ready_root, timeout=0.3)
    waited_nohook = time.monotonic() - t0
    _d_mod.write_status(ready_root, {"hook": True, "pid": 2})
    t0 = time.monotonic()
    st_ok = _d_mod.wait_ready(ready_root, timeout=1.0)
    waited_ok = time.monotonic() - t0
    old = time.time() - 60
    os.utime(_d_mod.status_path(ready_root), (old, old))
    stale = _d_mod.wait_ready(ready_root, timeout=0.3)

check("没有状态文件 → wait_ready 等满超时就返回 None",
      missing is None and waited_missing >= 0.25, f"{waited_missing:.2f}s")
check("状态文件报到 hook=False → 不算就绪",
      not_hook is None and waited_nohook >= 0.25, f"{waited_nohook:.2f}s")
check("状态文件报到 hook=True → 立刻返回那份状态",
      bool(st_ok) and st_ok.get("pid") == 2 and waited_ok < 0.2, f"{waited_ok:.2f}s")
check("状态文件太旧（超过 STATUS_STALE_SECONDS）→ 不算就绪", stale is None, str(stale))

# 第 88 轮：真就绪时父进程要明确说「现在可以打字了」；--no-hook 时不谎报。
print()
with tempfile.TemporaryDirectory() as td:
    ready_root = Path(td)
    _d_mod.write_status(ready_root, {"hook": True, "pid": os.getpid()})
    _orig_cmd = _d_mod.background_command
    _orig_ready = _d_mod.READY_TIMEOUT_SECONDS
    _d_mod.background_command = lambda root, a: [sys.executable, "-c",
                                                 "import time; time.sleep(0.25)"]
    _d_mod.READY_TIMEOUT_SECONDS = 0.4
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc_ok = _d_mod.spawn_background(ready_root, [], log_path=ready_root / "x.log")
        t0 = time.monotonic()
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            rc_nohook = _d_mod.spawn_background(ready_root, ["--no-hook"],
                                                log_path=ready_root / "x.log")
        waited_nohook = time.monotonic() - t0
    finally:
        _d_mod.background_command = _orig_cmd
        _d_mod.READY_TIMEOUT_SECONDS = _orig_ready
    said_ok = buf.getvalue()
    said_nohook = buf2.getvalue()

check("状态已就绪 → 父进程说「已就绪：钩子装上了，现在可以打字了」",
      rc_ok == 0 and "已就绪：钩子装上了，现在可以打字了" in said_ok, said_ok[-200:])
check("--no-hook 时不去等钩子（立刻返回，也不谎报就绪）",
      rc_nohook == 0 and waited_nohook < 1.0
      and "已就绪" not in said_nohook and "还没就绪" not in said_nohook,
      f"{waited_nohook:.2f}s {said_nohook[-160:]}")

print()
print("=" * 72)
print(f"  结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)

if log_path.exists():
    log_path.unlink()
for f in (swap_path, keep_path):
    if f.exists():
        f.unlink()

sys.exit(1 if FAIL else 0)
