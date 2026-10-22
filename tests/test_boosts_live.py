# -*- coding: utf-8 -*-
"""自学习权重的**真机**验证：真进程、真键盘注入、真被强杀。

``tests/test_boosts.py`` 验的是「文件那一层对不对」（快、确定）；这个文件验的是
**用户实际会遇到的顺序**：

1. 后台常驻的实例（``pythonw`` + ``--detached``，跟用户双击 run.bat 一样）；
2. 用真键盘注入打字（``extra_info=0``，钩子眼里就是真按键）→ 数字键改选 → 上屏进另一个窗口；
3. **不退出**，等心跳把 ``user_boosts.json`` 写下去（这就是第 27 轮修的那条路：
   以前只有 ``shutdown()`` 写盘）；
4. ``taskkill /F`` 直接强杀 —— 模拟崩溃 / 注销 / 断电，文件必须还在；
5. 重新起一个实例，再打同一个拼音 —— 那个词应该已经顶到首位（README 的承诺）。

跑不了就跳过（有人正在用这个输入法时不抢）：
有人在跑 → 全部检查按「跳过」记 ok，退出码 0。

直接跑：``python tests/test_boosts_live.py``
"""
import ctypes
import json
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime import daemon  # noqa: E402
from t_ime import winapi as W  # noqa: E402
from t_ime.config import load as load_config  # noqa: E402
from t_ime.console import fix_console_encoding  # noqa: E402
from t_ime.core import BOOST_SAVE_INTERVAL  # noqa: E402

fix_console_encoding()

PASS = 0
FAIL = 0
HERE = Path(__file__).resolve().parent
TITLE = "T-IME-CARET"
OUT = HERE / "_boosts_target_out.json"
CMD = HERE / "_boosts_target_cmd.txt"

u32 = ctypes.windll.user32
u32.FindWindowW.restype = ctypes.c_void_p
u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
u32.GetForegroundWindow.restype = ctypes.c_void_p
u32.SetForegroundWindow.argtypes = [wintypes.HWND]
u32.SetForegroundWindow.restype = wintypes.BOOL

LETTERS = {}          # 打字用：字母 → 虚拟键码（0x41..0x5A）
PINYIN = "shangchuan"
WORD = "上传"
WORD_IDX = 2          # 数字键 3（0x31 + 2）


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok ] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  <-- {extra!r}")


def skip_all(reason: str, n: int):
    """不能跑真机时，用同样多的项目记「跳过」——总数不随环境变。"""
    global PASS
    for i in range(n):
        PASS += 1
        print(f"  [ok ] （跳过 {i + 1}/{n}：{reason}）")


def read_dump():
    for _ in range(6):
        try:
            return json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            time.sleep(0.05)
    return {"insert": "<读不到>", "text": "<读不到>", "cmd": None}


def wait_text(want: str, timeout: float = 4.0):
    end = time.time() + timeout
    while time.time() < end:
        if read_dump().get("text") == want:
            return True
        time.sleep(0.05)
    return False


_cmd_seq = 0


def clear_target():
    """清空靶子，并且**等命令回显** —— 只等文本为空会撞上「还没落地的旧状态」。"""
    global _cmd_seq
    _cmd_seq += 1
    want = f"clear {_cmd_seq}"
    CMD.write_text(want, encoding="utf-8")
    end = time.time() + 3.0
    while time.time() < end:
        d = read_dump()
        if d.get("cmd") == want and d.get("text") == "" and d.get("insert") == "1.0":
            return True
        time.sleep(0.05)
    return False


def key_down(vk: int):
    """注入一个**真的**按键（不带我们的标记，钩子会当成别的程序转发的真键）。"""
    scan = W.user32.MapVirtualKeyW(vk, W.MAPVK_VK_TO_VSC)
    W._send([W._kb_input(vk, scan, 0, 0)])


def key_up(vk: int):
    scan = W.user32.MapVirtualKeyW(vk, W.MAPVK_VK_TO_VSC)
    W._send([W._kb_input(vk, scan, W.KEYEVENTF_KEYUP, 0)])


def tap(vk: int, gap: float = 0.05):
    key_down(vk)
    time.sleep(gap)
    key_up(vk)


def type_text(s: str, gap: float = 0.12):
    for ch in s:
        tap(ord(ch.upper()), gap)
        time.sleep(gap)


def launch_instance(cfg_path: Path, log_path: Path):
    cmd = daemon.background_command(PROJ, ["--config", str(cfg_path), "--log", str(log_path)])
    if cmd is None:
        return None
    flags = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(cmd, cwd=str(PROJ), close_fds=True, creationflags=flags,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def wait_instance(log_path: str, timeout: float = 30.0):
    """等**我们起的那个**实例报到：状态文件里的 log 路径能对上，且钩子装上了。"""
    end = time.time() + timeout
    while time.time() < end:
        st = daemon.read_status(PROJ) or {}
        if str(st.get("log") or "") == log_path and st.get("hook"):
            return st
        time.sleep(0.2)
    return None


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True, errors="replace").stdout
    return str(pid) in out


def kill_pid(pid: int):
    """真强杀（``taskkill /F``）—— 模拟崩溃 / 注销 / 断电，不是干净退出。"""
    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                   capture_output=True, text=True, errors="replace")
    end = time.time() + 8.0
    while time.time() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return False


print("=" * 74)
print("自学习权重 · 真机验证（真实例 + 真注入 + 真强杀）")
print("=" * 74)

if W.instance_exists():
    print()
    print("！检测到已经有 translate-ime 在跑 —— 不抢用户正在用的那个实例，本次全部跳过。")
    print("  （想真跑一遍：先退出输入法，再运行本文件。）")
    skip_all("已有实例在跑", 21)
else:
    TMP = Path(tempfile.mkdtemp(prefix="t-ime-boostlive-"))
    CFG_PATH = TMP / "config.json"
    BOOST = TMP / "user_boosts.json"
    LOG = TMP / "live.log"
    target = None
    root = None
    inst = None
    inst2 = None
    pid1 = 0
    pid2 = 0
    try:
        cfg, _ = load_config()
        cfg["output"] = dict(cfg.get("output") or {}, boost_file=str(BOOST))
        CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        check("临时配置写好了（把自学习文件指向临时目录）", CFG_PATH.exists(), str(CFG_PATH))

        # ---- 靶子窗口：另一个进程，用来接住「上屏」 ----------------------------
        OUT.unlink(missing_ok=True)
        CMD.unlink(missing_ok=True)
        target = subprocess.Popen(
            [sys.executable, str(HERE / "caret_target.py"), str(OUT), str(CMD)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        hwnd = 0
        end = time.time() + 12.0
        while time.time() < end and not hwnd:
            hwnd = u32.FindWindowW(None, TITLE) or 0
            time.sleep(0.2)
        check("靶子窗口起来了（另一个进程的 Tk 文本框）", bool(hwnd), hwnd)
        if not hwnd:
            raise SystemExit(1)

        # 抢前台：**本进程必须自己也有个窗口**，否则 Windows 的前台锁一定拒绝
        # （实测没有窗口时 W.set_foreground() 连试 6 次全 False；先建一个 1x1 的
        # Tk 窗口，同一句调用第一次就 True）。别直接裸调 SetForegroundWindow ——
        # 它不报错、只是不生效，后面所有注入都会打进别的窗口。
        root = tk.Tk()
        root.geometry("1x1+0+0")
        root.update()
        ok_fg = False
        for _ in range(8):
            if W.set_foreground(hwnd) and W.same_window(W.foreground_hwnd(), hwnd):
                ok_fg = True
                break
            time.sleep(0.4)
        check("靶子拿到了前台（等一下再注入）", ok_fg,
              (W.foreground_hwnd(), hwnd))

        # 靶子刚起来那会儿还不收键（第 25 轮实测 0.4~1.7 秒），先握个手再演。
        clear_target()
        ready = False
        for _ in range(20):
            W.send_text(".")
            if wait_text(".", 0.4):
                ready = True
                break
        check("靶子已经准备好收键（握手：注入一个字符真的落地）", ready, read_dump())
        clear_target()

        # ---- 1. 后台常驻的真实例 ---------------------------------------------
        # 注意：不能拿 Popen().pid 当实例 pid —— venv 里的 pythonw.exe 只是个
        # 转发器，真正的解释器是它另起的子进程。用状态文件里的 log 路径认领。
        inst = launch_instance(CFG_PATH, LOG)
        check("实例起来了（pythonw + --detached，跟 run.bat 一样）", inst is not None)
        st = wait_instance(str(LOG)) if inst else None
        check("状态文件报到：钩子装上了、日志就是我们的临时目录",
              st is not None, daemon.read_status(PROJ))
        pid1 = int((st or {}).get("pid") or 0)
        check("认领到的实例 pid 是个活进程", pid1 > 0 and pid_alive(pid1), (pid1,))
        check("此刻还没有自学习文件（一场新的学习）", not BOOST.exists(), str(BOOST))

        # ---- 2. 真键盘打字 + 数字键改选 --------------------------------------
        clear_target()
        type_text(PINYIN)
        time.sleep(0.5)
        check("打拼音时字母被钩子吃掉了（靶子里没多出字母）",
              read_dump().get("text") == "", read_dump())
        tap(0x31 + WORD_IDX)          # 数字键 3 = 第 3 个候选『上传』
        ok = wait_text(WORD, 4.0)
        check(f"数字键改选后『{WORD}』上屏进了别的窗口", ok, read_dump())

        # ---- 3. 不退出，等心跳落盘（本轮修的就是这个）------------------------
        print(f"        等心跳落盘（节流 {BOOST_SAVE_INTERVAL:g} 秒）…")
        end = time.time() + BOOST_SAVE_INTERVAL + 8.0
        while time.time() < end and not BOOST.exists():
            time.sleep(0.2)
        check("★ 实例没退出，权重也已经写进 user_boosts.json", BOOST.exists(), str(BOOST))
        saved = json.loads(BOOST.read_text(encoding="utf-8")) if BOOST.exists() else {}
        check(f"文件里记着的就是刚才改选的词（{WORD}）", saved.get(WORD) == 1.0, saved)
        st2 = daemon.read_status(PROJ) or {}
        check("状态文件里报了记住几个词", int(st2.get("boosts") or 0) >= 1, st2.get("boosts"))

        # ---- 4. 强杀：不干净地死掉，学习成果也必须留下 ------------------------
        dead = kill_pid(pid1)
        check("taskkill /F 之后进程真的没了", dead, pid1)
        inst = None
        check("强杀之后文件还在", BOOST.exists(), str(BOOST))
        after = json.loads(BOOST.read_text(encoding="utf-8")) if BOOST.exists() else {}
        check("强杀之后那个词还在（以前这一场就白学了）", after.get(WORD) == 1.0, after)

        # ---- 5. 重启一个实例：那个词应该已经顶到首位 --------------------------
        time.sleep(0.5)
        LOG2 = TMP / "live2.log"
        inst2 = launch_instance(CFG_PATH, LOG2)
        st3 = wait_instance(str(LOG2)) if inst2 else None
        check("第二个实例起来了（同一份自学习文件）", st3 is not None, daemon.read_status(PROJ))
        pid2 = int((st3 or {}).get("pid") or 0)
        clear_target()
        type_text(PINYIN)
        time.sleep(0.5)
        check("重启后打同样的拼音，字母照样被吃掉（钩子在工作）",
              read_dump().get("text") == "", read_dump())
        tap(0x20)                     # 空格 = 上屏首选项
        ok = wait_text(WORD, 4.0)
        check(f"★ 重启后空格上屏的就是它（{WORD} 已经顶到首位）", ok, read_dump())
        check("文档里那句承诺是真的", read_dump().get("text") == WORD, read_dump())
    finally:
        for pid in (pid1, pid2):
            if pid:
                kill_pid(pid)
        if target is not None:
            target.kill()
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        time.sleep(0.4)
        # 我们自己起的实例留下的状态/命令文件要清掉（用户本来没在跑），
        # 不然下次双击 run.bat 会看到一个「已经卡死」的旧状态。
        if not W.instance_exists():
            for p in (daemon.status_path(PROJ), daemon.ctl_path(PROJ)):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        for p in (OUT, CMD):
            p.unlink(missing_ok=True)
        shutil.rmtree(TMP, ignore_errors=True)

    print()
    check("收工：没有残留的实例", not W.instance_exists(), daemon.read_status(PROJ))

print()
print("=" * 74)
print(f"{PASS} 通过 / {FAIL} 失败")
print("=" * 74)
sys.exit(1 if FAIL else 0)
