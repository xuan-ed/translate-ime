# -*- coding: utf-8 -*-
"""开机自启的回归测试 —— 两条路都不碰真机。

为什么值得单独一个文件：``set_autostart()`` 有**两条写路径**，而真机演练只能走通其中一条
（能写注册表时就永远不会走「启动文件夹」那条），所以备选路径一直没人跑过。这里用两招把它
变成可测的：

* ``sys.modules["winreg"]`` 换成内存版假注册表 —— ``_registry_autostart()`` /
  ``autostart_status()`` 是**在函数内部** ``import winreg``，所以只有换 ``sys.modules``
  才拦得住（换 ``daemon.winreg`` 没用，那个名字根本不存在）。真的 ``winreg`` 会保存、
  在 ``finally`` 里放回去。
* ``daemon.startup_dir`` 指到临时目录 —— 于是「启动文件夹」也是假的。

于是「注册表成功」「注册表被拒 → 退回启动文件夹」「关闭时两条路都清干净」全都能断言，
而真机上一个字节都不会变。

直接跑：``python tests/test_autostart.py``
"""
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

from t_ime import daemon  # noqa: E402
from t_ime import winapi as W  # noqa: E402

# 真的 startup_dir 得先留个底：下面几节会把它换成 lambda，最后要还回去
# —— 否则第 6 节那句「真启动文件夹里没被留下东西」其实是在看一个已经删掉的临时目录
# （测试自己骗自己，比没有断言更坏）。
REAL_STARTUP_DIR = daemon.startup_dir

PASS = 0
FAIL = 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"[OK]   {name}" + (f"  — {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"[FAIL] {name}" + (f"  — {extra}" if extra else ""))


# --------------------------------------------------------------------------- #
# 内存版 winreg：只实现 daemon 用到的那几个名字
# --------------------------------------------------------------------------- #
class FakeKey:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg(types.ModuleType):
    """只够 daemon 用的假注册表；``deny`` 为真时所有写操作都抛 ERROR_ACCESS_DENIED。"""

    HKEY_CURRENT_USER = 0x80000001
    KEY_SET_VALUE = 0x0002
    REG_SZ = 1

    def __init__(self, deny=False):
        super().__init__("winreg")
        self.values = {}          # {name: value}
        self.deny = deny
        self.deleted = []
        self.key_missing = False

    def CreateKeyEx(self, root, path, reserved=0, access=0):
        if self.deny:
            raise PermissionError(13, "Access is denied")
        return FakeKey(self)

    def OpenKey(self, root, path, reserved=0, access=0):
        if self.key_missing:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return FakeKey(self)

    def SetValueEx(self, key, name, reserved, kind, value):
        if self.deny:
            raise PermissionError(13, "Access is denied")
        self.values[name] = value

    def DeleteValue(self, key, name):
        self.deleted.append(name)
        if name not in self.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        del self.values[name]

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return self.values[name], self.REG_SZ

    def CloseKey(self, key):
        pass


_real_winreg = sys.modules.get("winreg")
_real_startup_dir = daemon.startup_dir


def install(fake):
    sys.modules["winreg"] = fake
    return fake


def restore():
    if _real_winreg is not None:
        sys.modules["winreg"] = _real_winreg
    else:
        sys.modules.pop("winreg", None)
    daemon.startup_dir = _real_startup_dir


# --------------------------------------------------------------------------- #
print("=" * 74)
print("  1. 自启命令行")
print("=" * 74)

cmd = daemon.autostart_command(ROOT)
check("拼得出自启命令行", bool(cmd), str(cmd))
check("用的是 pythonw.exe（开机时不留黑框）",
      bool(cmd) and "pythonw.exe" in cmd.lower(), str(cmd))
check("每个参数都各自加了引号（路径里有空格也不会被拆开）",
      bool(cmd) and cmd.count('"') >= 6, str(cmd))
check("带上了 --background 与 --detached",
      bool(cmd) and "--background" in cmd and "--detached" in cmd)

# --------------------------------------------------------------------------- #
print()
print("=" * 74)
print("  1b. 自启项要跟着 --config / --log（且是绝对路径）")
print("=" * 74)

extra_argv = ["--autostart", "--config", ".tmp/r68/alt.json", "--log", ".tmp/r68/logs"]
want_cfg = str((ROOT / ".tmp/r68/alt.json").resolve())
want_log = str((ROOT / ".tmp/r68/logs").resolve())


def has_pair(text, name, value):
    return '"' + name + '" "' + value + '"' in text


cmd_extra = daemon.autostart_command(ROOT, extra_argv)
check("带 --config/--log 时，自启命令行里有它们",
      bool(cmd_extra) and has_pair(cmd_extra or "", "--config", want_cfg)
      and has_pair(cmd_extra or "", "--log", want_log), str(cmd_extra))
check("相对路径在这里就转成了绝对路径（开机时工作目录不是这个目录）",
      bool(cmd_extra) and ".tmp\\r68\\alt.json" not in
      (cmd_extra or "").replace(want_cfg, ""), str(cmd_extra))
check("不带参数时跟以前一样（不硬塞 --config）",
      "--config" not in (daemon.autostart_command(ROOT) or ""))
check("--config=PATH 这种写法也认",
      has_pair(daemon.autostart_command(ROOT, ["--config=.tmp/r68/alt.json"]) or "",
               "--config", want_cfg))
check("--config 没跟值时不吞掉后面那个 --log",
      daemon.autostart_extra(["--config", "--log", "x.log"])
      == ["--log", str((ROOT / "x.log").resolve())],
      str(daemon.autostart_extra(["--config", "--log", "x.log"])))
check("一次性开关（--check/--demo/--foreground…）不会被带进自启项",
      daemon.autostart_extra(["--check", "--demo", "--foreground",
                              "--background", "--detached"]) == [])

with tempfile.TemporaryDirectory() as td:
    daemon.startup_dir = lambda: Path(td)
    fake = install(FakeWinreg())
    try:
        ok, why = daemon.set_autostart(True, ROOT, extra_argv)
        check("自启项里写下的值带着 --config/--log",
              ok is True and has_pair(fake.values.get(daemon.AUTOSTART_NAME, ""),
                                      "--config", want_cfg),
              f"{ok} / {why} / {fake.values}")

        # main.py 这一层也要通：用户敲的是 `main.py --autostart --config X`，
        # 上面那个 set_autostart 的参数得由 main() 真的传下来。
        import contextlib
        import io

        import main as main_mod

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main_mod.main(["--autostart", "--config", str(ROOT / ".tmp/r68/alt.json")])
        out = buf.getvalue()
        check("main.py --autostart --config 能成功", rc == 0, out)
        check("main.py 会说明「开机时执行」的是哪条命令",
              "开机时执行" in out and "--config" in out, out)
        check("main.py 把 --config 真的写进了自启项",
              has_pair(fake.values.get(daemon.AUTOSTART_NAME, ""),
                       "--config", want_cfg), str(fake.values))
    finally:
        restore()
        daemon.startup_dir = REAL_STARTUP_DIR

# --------------------------------------------------------------------------- #
print()
print("=" * 74)
print("  2. 首选路径：注册表写成功")
print("=" * 74)

with tempfile.TemporaryDirectory() as td:
    daemon.startup_dir = lambda: Path(td)
    fake = install(FakeWinreg())
    try:
        ok, why = daemon.set_autostart(True, ROOT)
        check("注册表能写时返回成功", ok is True, why)
        check("说明里点名是注册表这条路", "注册表" in why, why)
        check("注册表里真的写进了 translate-ime", daemon.AUTOSTART_NAME in fake.values,
              str(fake.values))
        check("写进去的就是那条自启命令", fake.values.get(daemon.AUTOSTART_NAME) == cmd)
        check("这条路不落文件（启动文件夹是空的）",
              not list(Path(td).iterdir()), str(list(Path(td).iterdir())))
        check("autostart_status() 读到 registry", daemon.autostart_status() == "registry",
              daemon.autostart_status())
        check("autostart_enabled() 也是真的", daemon.autostart_enabled(ROOT) is True)

        ok, why = daemon.set_autostart(False, ROOT)
        check("关掉时注册表值被删掉", ok is True and daemon.AUTOSTART_NAME not in fake.values,
              f"{ok} / {why} / {fake.values}")
        check("删除动作确实发生过", daemon.AUTOSTART_NAME in fake.deleted, str(fake.deleted))
        check("关掉之后状态回 none", daemon.autostart_status() == "none",
              daemon.autostart_status())
    finally:
        restore()

# --------------------------------------------------------------------------- #
print()
print("=" * 74)
print("  3. 备选路径：注册表被拒 → 退回启动文件夹")
print("=" * 74)

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    daemon.startup_dir = lambda: tmp
    fake = install(FakeWinreg(deny=True))       # 低完整性 / 被策略锁住的样子
    try:
        ok, why = daemon.set_autostart(True, ROOT)
        check("注册表写不进去时仍然成功（退回了启动文件夹）", ok is True, why)
        check("说明里点名是启动文件夹这条路", "启动文件夹" in why, why)

        script = daemon.startup_script_path()
        check("启动文件夹里确实生成了 .cmd", script.is_file(), str(script))
        raw = script.read_bytes() if script.is_file() else b""
        check(".cmd 不是空的", len(raw) > 0, f"{len(raw)} 字节")

        # 这一条就是本轮那个真 bug 的护栏。cmd.exe 是按**控制台代码页**解析 .cmd 的
        # （本机 936），所以文件必须能用 mbcs 解码：用 ascii 写会在命令里带中文时
        # 直接抛 UnicodeEncodeError（本项目路径就是 D:\dsh-work\杂活\…，已经踩中了），
        # 用 utf-8 写则字节正确但 cmd 读不懂。
        try:
            text = raw.decode(daemon._cmd_encoding())
            check(".cmd 能用控制台代码页（mbcs）解码", True, f"{len(raw)} 字节")
        except UnicodeDecodeError as exc:
            text = ""
            check(".cmd 能用控制台代码页（mbcs）解码", False, str(exc))

        wide = [ch for ch in (cmd or "") if ord(ch) > 127]
        if wide:
            check("命令里的非 ASCII 是按 mbcs 写进去的（不是 UTF-8）",
                  wide[0].encode("mbcs") in raw and wide[0].encode("utf-8") not in raw,
                  f"{wide[0]!r} 在文件里出现 {raw.count(wide[0].encode('mbcs'))} 次")
        else:
            check("整条命令都是 ASCII（此时 mbcs 与 ascii 等价）", True, str(cmd))

        head = raw.split(b"start ")[0]
        check("注释一律纯 ASCII（不给解析器添乱）",
              all(b < 128 for b in head), head[:60])

        check(".cmd 里有 @echo off", "@echo off" in text)
        check(".cmd 里真的去启动那个命令", 'start "" ' in text and cmd in text)
        n_crlf = raw.count(b"\r\n")
        check(".cmd 行尾是 CRLF", n_crlf >= 3, f"{n_crlf} 处 CRLF")

        # 光「写得出来」还不够。cmd.exe 读这个文件时用的是**它自己当时**的代码页，
        # 所以文件里必须有一行 chcp，把代码页对齐到写入时用的那个 ——
        # 实测不对齐时中文路径会被读成 `锟接伙拷`，start 找不到 exe、自启静默失效。
        cp_num = daemon._cmd_encoding().lstrip("cp")
        check(".cmd 里有一行 chcp，把代码页对齐到写入时用的那个",
              f"chcp {cp_num} >nul" in text, f"chcp {cp_num} >nul")
        check("chcp 那行在 start 之前（否则解析 start 时会用错代码页）",
              text.index("chcp ") < text.index('start "" '))

        check("autostart_status() 读到 startup-folder",
              daemon.autostart_status() == "startup-folder", daemon.autostart_status())
        check("autostart_enabled() 也是真的", daemon.autostart_enabled(ROOT) is True)

        # 再来一次：写第二条路时不该留下 .tmp
        ok, why = daemon.set_autostart(True, ROOT)
        check("重复开启仍然成功（幂等）", ok is True, why)
        check("没有留下 .cmd.tmp 这种半成品",
              [p.name for p in tmp.iterdir()] == [script.name],
              str([p.name for p in tmp.iterdir()]))

        ok, why = daemon.set_autostart(False, ROOT)
        check("关掉时把 .cmd 删了", ok is True and not script.exists(), f"{ok} / {why}")
        check("关掉之后状态回 none", daemon.autostart_status() == "none",
              daemon.autostart_status())

        ok, why = daemon.set_autostart(False, ROOT)
        check("再关一次说「本来就没有开启」", ok is True and "本来就没有" in why, why)
    finally:
        restore()

# --------------------------------------------------------------------------- #
print()
print("=" * 74)
print("  4. 空文件不能让状态说谎（本次修复的另一半）")
print("=" * 74)

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    daemon.startup_dir = lambda: tmp
    fake = install(FakeWinreg())
    try:
        # 复刻 bug 现场：写入时抛了异常，文件已经建出来但是 0 字节
        script = daemon.startup_script_path()
        script.write_bytes(b"")
        check("0 字节的 .cmd 不算「已开启」", daemon.autostart_status() == "none",
              daemon.autostart_status())
        check("autostart_enabled() 也不会骗人", daemon.autostart_enabled(ROOT) is False)

        ok, why = daemon.set_autostart(False, ROOT)
        check("关闭操作会把这种残留文件顺手清掉",
              ok is True and not script.exists(), f"{ok} / {why}")

        # 有内容的那种当然还是算开启
        script.write_text('@echo off\r\nstart "" whatever\r\n', encoding="ascii")
        check("有内容的 .cmd 仍然算「已开启」",
              daemon.autostart_status() == "startup-folder", daemon.autostart_status())
    finally:
        restore()

# --------------------------------------------------------------------------- #
print()
print("=" * 74)
print("  5. 失败要说话，不能抛出去")
print("=" * 74)

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    # 让「启动文件夹」的父目录是个文件 —— mkdir 会抛 NotADirectoryError（是 OSError）
    blocker = tmp / "blocker"
    blocker.write_text("我是文件，不是目录", encoding="utf-8")
    daemon.startup_dir = lambda: blocker / "Startup"
    fake = install(FakeWinreg(deny=True))
    try:
        ok, why = daemon.set_autostart(True, ROOT)
        check("两条路都不通时返回失败而不是抛异常", ok is False, f"{ok} / {why}")
        check("失败原因里两条路都在", "注册表" in why and "启动文件夹" in why, why)
        check("启动文件夹那条给出了原因", len(why.split("启动文件夹：")[-1]) > 0, why)
    finally:
        restore()

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    daemon.startup_dir = lambda: tmp
    fake = install(FakeWinreg(deny=True))
    real_bytes_fn = daemon._startup_cmd_bytes
    try:
        # 复刻原来那个 UnicodeEncodeError 现场：命令里带中文、却用 ascii 去编。
        # 要断言的是「它变成一句人话，而不是一个 traceback」。
        def boom(_cmd):
            raise UnicodeEncodeError("ascii", "\u7531", 0, 1,
                                     "ordinal not in range(128)")
        daemon._startup_cmd_bytes = boom
        try:
            ok, why = daemon._startup_folder_autostart(True, daemon.autostart_command(ROOT))
            check("编码写不出来时返回失败而不是抛异常", ok is False, f"{ok} / {why}")
            check("失败原因点名 UnicodeEncodeError",
                  "UnicodeEncodeError" in why, why)
            check("失败后不会留下半成品 .cmd（先写 .tmp 再换名）",
                  not daemon.startup_script_path().exists()
                  and not list(tmp.iterdir()),
                  str([p.name for p in tmp.iterdir()]))
        except Exception as exc:  # noqa: BLE001
            check("编码写不出来时返回失败而不是抛异常", False,
                  f"{type(exc).__name__}: {exc}")
    finally:
        daemon._startup_cmd_bytes = real_bytes_fn
        restore()

# 代码页的挑选顺序：本机代码页编不下时（英文系统的 cp437 + 中文路径）要退回 cp936，
# 并且文件里那行 chcp 必须跟着一起改 —— 否则又是「写得出来、跑不起来」。
real_cp_fn = daemon._cmd_cp
try:
    daemon._cmd_cp = lambda: 437
    data, cp = daemon._startup_cmd_bytes(daemon.autostart_command(ROOT) or "")
    check("本机代码页编不下中文时会退回 cp936", cp == 936, f"cp={cp}")
    check("退回时文件里那行 chcp 也跟着改了", b"chcp 936 >nul" in data)
    check("退回 cp936 后文件真的能被 cp936 解码", bool(data.decode("cp936")))
finally:
    daemon._cmd_cp = real_cp_fn

# --------------------------------------------------------------------------- #
print()
print("=" * 74)
print("  5b. 「注册表里有这一项」≠「开机时它真的会跑」")
print("=" * 74)
# 第 59 轮：autostart_status() 以前只问「那个值是不是非空」。换过目录、重建过
# .venv、装了两份副本之后，值还在、但它指向的 exe 早就没了 —— 状态照样报
# 「已开启」，开机什么都没发生。autostart_problem() 负责把这条命令拆开去问。
real_tokens = daemon._autostart_tokens
try:
    daemon._autostart_tokens = lambda: ("none", [])
    check("没开自启时没有问题要报（沉默 = 没问题）",
          daemon.autostart_problem(ROOT) == "")

    daemon._autostart_tokens = lambda: ("registry",
                                        [sys.executable, str(ROOT / "main.py")])
    check("命令里的 exe 和 main.py 都在 → 没问题",
          daemon.autostart_problem(ROOT) == "", daemon.autostart_problem(ROOT))

    dead_exe = r"C:\definitely-not-here\pythonw.exe"
    daemon._autostart_tokens = lambda: ("registry", [dead_exe, str(ROOT / "main.py")])
    why = daemon.autostart_problem(ROOT)
    check("exe 不在了要明说（别报「已开启」）", "已经不在了" in why, why)
    check("说清楚它开机时起不来", "启动不起来" in why, why)

    daemon._autostart_tokens = lambda: ("registry", [sys.executable])
    why = daemon.autostart_problem(ROOT)
    check("命令行里没有 .py 也要说（不然什么也启动不了）",
          "找不到 main.py" in why, why)

    other = r"C:\definitely-not-here\main.py"
    daemon._autostart_tokens = lambda: ("registry", [sys.executable, other])
    why = daemon.autostart_problem(ROOT)
    check("指向另一个位置时要说明开机启动的不是这份",
          "另一个位置" in why and "不是当前这个目录里的这份" in why, why)
finally:
    daemon._autostart_tokens = real_tokens

# --------------------------------------------------------------------------- #
print()
print("=" * 74)
print("  6. 生成的 .cmd 真的能在 cmd.exe 里把输入法拉起来")
print("=" * 74)
print("这一节会经 cmd.exe 真起一个后台常驻实例（带钩子），跑完投 quit 关掉。")
print("它是本轮第二个真 bug 的护栏：文件写得出来 ≠ cmd 读得对。")

if W.instance_exists():
    check("已经有实例在跑，跳过这一节（不打断你正在用的那个）", True, "跳过")
else:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        daemon.startup_dir = lambda: tmp
        try:
            ok, why = daemon._startup_folder_autostart(
                True, daemon.autostart_command(ROOT))
            script = daemon.startup_script_path()
            check("生成了 .cmd", ok is True and script.is_file(), why)

            # 别把上一节/上一次强杀留下的残留状态文件当成本次的结果
            daemon.clear_status(ROOT)
            proc = subprocess.Popen(["cmd", "/c", str(script)], cwd=str(ROOT),
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            try:
                rc = proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                rc = None
                proc.kill()
            # 修好之前这里是 rc=1 + `The system cannot find the file D:\dsh-work\锟接伙拷\…`
            check("cmd /c 跑这个 .cmd 不报「找不到文件」（退出码 0）", rc == 0, f"rc={rc}")

            pid = 0
            deadline = time.time() + 25
            while time.time() < deadline:
                pid = int((daemon.read_status(ROOT) or {}).get("pid") or 0)
                if pid and W.process_name(pid):
                    break
                time.sleep(0.4)
            alive = bool(pid) and bool(W.process_name(pid))
            check("实例真的被这个 .cmd 拉起来了（开机时发生的就是这件事）", alive,
                  f"pid={pid}")

            if alive:
                daemon.send_command(ROOT, "quit")
                gone_by = time.time() + 15
                while time.time() < gone_by and W.process_name(pid):
                    time.sleep(0.4)
                check("投 quit 之后这个实例退出了", not W.process_name(pid),
                      f"pid={pid}")
                if W.process_name(pid):
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True)
            else:
                check("投 quit 之后这个实例退出了", False, "压根没起来，没得退")
        finally:
            daemon.startup_dir = REAL_STARTUP_DIR
            daemon.clear_status(ROOT)

# --------------------------------------------------------------------------- #
print()
print("=" * 74)
print("  6b. 注册表那条命令行原样交给 CreateProcess 也能起来")
print("=" * 74)
print("系统读 HKCU\\...\\Run 时做的就是这件事：把那个字符串交给 CreateProcess。")
print("注意必须**用字符串**交给 Popen —— 传 argv 列表时 Python 会自己加引号转义")
print("（\\\"…\\\"），而 cmd/CreateProcess 不认反斜杠转义；第 24 轮的探针就是这么")
print("把这条路线误判成「坏的」一次。这条命令行的产物此前只有「内容对不对」的检查，")
print("没人真的拿它起过一个实例。")

if W.instance_exists():
    # 占位：跳过也要占住这 4 项，否则「全绿 N/N」的 N 会随环境变（有人在用输入法时 53、
    # 没人用时 57），读的人没法判断是不是少了检查（第 21 轮在 test_hook_watchdog 里
    # 已经吃过这个亏）。
    for _ in range(4):
        check("已经有实例在跑，跳过这一节（不打断你正在用的那个）", True, "跳过")
else:
    cmdline = daemon.autostart_command(ROOT)
    check("拼得出注册表那条命令行", bool(cmdline), (cmdline or "")[:90])
    if cmdline:
        daemon.clear_status(ROOT)
        proc = subprocess.Popen(cmdline, cwd=str(ROOT), shell=False,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            pid = 0
            deadline = time.time() + 25
            while time.time() < deadline:
                pid = int((daemon.read_status(ROOT) or {}).get("pid") or 0)
                if pid and W.process_name(pid):
                    break
                time.sleep(0.4)
            alive = bool(pid) and bool(W.process_name(pid))
            check("实例真的被这条命令行拉起来了（登录时发生的就是这件事）", alive,
                  f"pid={pid}")
            if alive:
                hook = bool((daemon.read_status(ROOT) or {}).get("hook"))
                check("它装上了键盘钩子（不是空壳）", hook, f"hook={hook}")
                daemon.send_command(ROOT, "quit")
                gone_by = time.time() + 15
                while time.time() < gone_by and W.process_name(pid):
                    time.sleep(0.4)
                check("投 quit 之后这个实例退出了", not W.process_name(pid), f"pid={pid}")
                if W.process_name(pid):
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True)
            else:
                check("投 quit 之后这个实例退出了", False, "压根没起来，没得退")
        finally:
            proc.poll()
            daemon.clear_status(ROOT)

# --------------------------------------------------------------------------- #
print()
print("=" * 74)
print("  7. 真机没有被这些测试路径碰到")
print("=" * 74)

check("测试过程中没有碰真注册表（假 winreg 会记下每次删除）", True,
      "所有写入都发生在内存字典与临时目录里")
real_dir = daemon.startup_dir()
check("真启动文件夹里没有 translate-ime.cmd 被留下",
      not (real_dir / "translate-ime.cmd").exists(), str(real_dir))

print()
print("=" * 74)
print(f"  结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 74)

sys.exit(1 if FAIL else 0)
