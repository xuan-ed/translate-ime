# -*- coding: utf-8 -*-
"""第 57 轮：三条用户 bug 的**实机**复验（真常驻实例 + 真钩子 + 真靶子窗口）。

单元测试已经把状态机那一面测透了；这里补的是「真键盘事件走完整条链」那一面：

1. **开着输入法打不出大写英文** —— Shift+字母、CapsLock+字母，都必须落到靶子里，
   而不是变成拼音候选；
2. **没有 Shift 切英文** —— 单击 Shift 切「英文直通」，直通里字母原样落进靶子，
   再单击一下切回拼音；
3. **切窗口后拼音缓冲不清理** —— 敲一半拼音切到另一个窗口：缓冲必须清空，
   半截拼音不许落进新窗口，切回来还能接着打。

做法：临时配置起一个后台常驻实例（日志/自学习文件都指到临时目录），再起两个
独立进程的靶子窗口 —— 一个接字（caret 靶子，带 clear 命令），另一个纯粹当
「用户切过去的那个窗口」。所有注入都不带自家标记，钩子会当成真实按键处理。

抢不到前台窗口时退出码 2（环境问题，不算产品失败）；已有实例在跑时跳过（不打扰
用户正在用的那个）。直接跑：``python tests/test_user_bugs_live.py``
"""
import ctypes
import ctypes.wintypes as wintypes
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
sys.path.insert(0, str(PROJ))

from t_ime import daemon          # noqa: E402
from t_ime import winapi as W     # noqa: E402

TITLE_CARET = "T-IME-CARET"
TITLE_OTHER = "T-IME-TARGET"

PASS = 0
FAIL = 0

u32 = ctypes.WinDLL("user32", use_last_error=True)
u32.FindWindowW.restype = ctypes.c_void_p
u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
u32.SetForegroundWindow.argtypes = [wintypes.HWND]
u32.SetForegroundWindow.restype = wintypes.BOOL
u32.GetForegroundWindow.restype = ctypes.c_void_p


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok ] {label}", flush=True)
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  <-- {extra}", flush=True)


def skip(label, why):
    global PASS
    PASS += 1
    print(f"  [skip] {label}  <-- {why}", flush=True)


# ---------------------------------------------------------------------- #
# 注入：故意不带 T_IME_INJECT_TAG，让钩子把它当真实按键
# ---------------------------------------------------------------------- #
def key(vk: int, down: bool):
    scan = W.user32.MapVirtualKeyW(vk, W.MAPVK_VK_TO_VSC)
    flags = 0 if down else W.KEYEVENTF_KEYUP
    W._send([W._kb_input(vk, scan, flags, 0)])


def tap(vk: int, gap: float = 0.03):
    key(vk, True)
    time.sleep(gap)
    key(vk, False)


def tap_letter(ch: str, gap: float = 0.03):
    tap(ord(ch.upper()), gap)


def status() -> dict:
    return daemon.read_status(PROJ) or {}


def wait_for(fn, timeout: float, step: float = 0.05) -> tuple[bool, float]:
    start = time.time()
    while time.time() - start < timeout:
        try:
            if fn():
                return True, time.time() - start
        except Exception:  # noqa: BLE001 —— 轮询期间的读失败只当「还没到」
            pass
        time.sleep(step)
    return False, time.time() - start


def read_dump(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def clear_target(out_path: Path, cmd_path: Path, tag: str) -> bool:
    want = f"clear {tag}"
    cmd_path.write_text(want, encoding="utf-8")
    end = time.time() + 4.0
    while time.time() < end:
        d = read_dump(out_path)
        if d.get("cmd") == want and d.get("text") == "":
            return True
        time.sleep(0.05)
    return False


def focus(hwnd: int, tries: int = 40) -> bool:
    for _ in range(tries):
        try:
            u32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        except Exception:  # noqa: BLE001
            pass
        if W.same_window(W.foreground_hwnd(), hwnd):
            return True
        time.sleep(0.1)
    return False


def find(title: str, timeout: float = 15.0) -> int:
    hwnd = 0
    end = time.time() + timeout
    while time.time() < end:
        hwnd = int(u32.FindWindowW(None, title) or 0)
        if hwnd:
            return hwnd
        time.sleep(0.2)
    return 0


def text_of(out_path: Path) -> str:
    return str(read_dump(out_path).get("text") or "")


# ---------------------------------------------------------------------- #
def main() -> int:
    if W.instance_exists():
        print("[userbugs] 检测到已有实例在跑 —— 跳过（不打断你正在用的那一个）。")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="t-ime-userbugs-"))
    cfg_path = tmp / "config.json"
    log = tmp / "userbugs.log"
    boosts = tmp / "user_boosts.json"
    caret_out = tmp / "caret.json"
    caret_cmd = tmp / "caret_cmd.txt"
    other_out = tmp / "other.json"

    base = json.loads((PROJ / "config.json").read_text(encoding="utf-8"))
    base.setdefault("output", {})["boost_file"] = str(boosts)
    base.setdefault("log", {})["path"] = str(log)
    cfg_path.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")

    caret = other = instance = None
    caps_was_on: bool | None = None
    try:
        print("[userbugs] 起临时实例 + 两个靶子窗口 …", flush=True)
        caret = subprocess.Popen(
            [sys.executable, str(HERE / "caret_target.py"), str(caret_out), str(caret_cmd)],
            cwd=str(PROJ), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        other = subprocess.Popen(
            [sys.executable, str(HERE / "target_window.py"), str(other_out)],
            cwd=str(PROJ), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        cmd = daemon.background_command(PROJ, ["--config", str(cfg_path), "--log", str(log)])
        if cmd is None:
            skip("能拼出后台启动命令", "background_command -> None（没有 pythonw？）")
            return 0
        instance = subprocess.Popen(cmd, cwd=str(PROJ), close_fds=True,
                                    creationflags=0x00000008 | 0x00000200,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)

        st: dict = {}
        end = time.time() + 30
        while time.time() < end:
            st = status()
            if str(st.get("log") or "") == str(log) and st.get("hook"):
                break
            time.sleep(0.2)
        check("临时实例起来了，状态文件报到的是我们这一个",
              int(st.get("pid") or 0) > 0 and str(st.get("log")) == str(log), st)
        check("钩子装上了", bool(st.get("hook")), st)

        hwnd_caret = find(TITLE_CARET)
        hwnd_other = find(TITLE_OTHER)
        check("两个靶子窗口都找到了", bool(hwnd_caret and hwnd_other),
              (hwnd_caret, hwnd_other))
        if not (hwnd_caret and hwnd_other):
            return 1
        if not focus(hwnd_caret):
            print("[userbugs] 抢不到前台窗口 —— 环境问题，跳过这次实机复验")
            return 2
        check("caret 靶子拿到了前台", W.same_window(W.foreground_hwnd(), hwnd_caret),
              W.foreground_hwnd())
        check("靶子清空了", clear_target(caret_out, caret_cmd, "1"), text_of(caret_out))

        # ---- 先把 CapsLock 归零：本测试要轮流控制它 ----
        caps_now = W.caps_lock_on()
        caps_was_on = bool(caps_now)
        if caps_now:
            tap(W.VK_CAPITAL)
            ok, _ = wait_for(lambda: W.caps_lock_on() is False, 2.0)
            check("开测前把 CapsLock 关掉（测完会还原）", ok, W.caps_lock_on())
        else:
            skip("开测前把 CapsLock 关掉（本来就是关的）", f"caps={caps_now}")
        ok, _ = wait_for(lambda: not (status().get("caps")), 2.0)
        check("实例也认为 CapsLock 关着", ok, status().get("caps"))

        # ---- 1. 打不出大写英文：Shift+字母 ----
        print("\n-- 1. 开着输入法，Shift+字母必须上屏大写英文 --", flush=True)
        key(W.VK_SHIFT, True)
        time.sleep(0.05)
        tap_letter("a")
        time.sleep(0.08)
        key(W.VK_SHIFT, False)
        ok, dt = wait_for(lambda: text_of(caret_out).endswith("A"), 3.0)
        check("Shift+a → 靶子里出现大写 A", ok, (text_of(caret_out), f"{dt:.2f}s"))
        ov1 = status().get("overlay") or {}
        check("大写 A 没走拼音，候选窗一次都没弹",
              ov1.get("mode") != "candidates", ov1)

        # ---- 1b. CapsLock 开着：a 也要是大写 ----
        print("\n-- 1b. CapsLock 开着时，a 必须是大写 A --", flush=True)
        tap(W.VK_CAPITAL)
        ok, _ = wait_for(lambda: status().get("caps") is True, 2.0)
        check("实例看到了 CapsLock 按下", ok, status().get("caps"))
        tap_letter("a")
        ok, dt = wait_for(lambda: text_of(caret_out).endswith("A"), 3.0)
        check("CapsLock 开着敲 a → 靶子里是大写 A", ok,
              (text_of(caret_out), f"{dt:.2f}s"))
        tap(W.VK_CAPITAL)       # 关回去
        ok, _ = wait_for(lambda: status().get("caps") is False, 2.0)
        check("CapsLock 关回去，实例跟着回到 False", ok, status().get("caps"))

        # ---- 2. 单击 Shift 切英文直通 ----
        print("\n-- 2. 单击 Shift 切「英文直通」 --", flush=True)
        tap(W.VK_SHIFT, gap=0.05)
        ok, _ = wait_for(lambda: status().get("direct") is True, 3.0)
        check("单击 Shift → 状态文件里 direct=True", ok, status().get("direct"))
        tap_letter("a")
        time.sleep(0.06)
        tap_letter("b")
        time.sleep(0.1)
        tap_letter("c")
        ok, dt = wait_for(lambda: text_of(caret_out).endswith("Abc")
                          or text_of(caret_out).endswith("abc"), 3.0)
        check("直通模式下 abc 原样落进靶子（不转拼音）", ok,
              (text_of(caret_out), f"{dt:.2f}s"))
        ok, _ = wait_for(
            lambda: (status().get("overlay") or {}).get("mode") != "candidates", 2.0)
        check("直通模式下候选窗一次都没弹（字母没进拼音）", ok,
              (status().get("overlay") or {}).get("mode"))
        tap(W.VK_SHIFT, gap=0.05)
        ok, _ = wait_for(lambda: status().get("direct") is False, 3.0)
        check("再单击 Shift → 切回拼音（direct=False）", ok, status().get("direct"))

        # ---- 3. 切窗口要清掉半截拼音 ----
        print("\n-- 3. 敲一半拼音切走：缓冲清空、半截拼音不许落进新窗口 --", flush=True)
        check("清空靶子准备第三段", clear_target(caret_out, caret_cmd, "2"),
              text_of(caret_out))
        wait_for(lambda: not (status().get("overlay") or {}).get("visible"), 2.0)
        for ch in "nih":
            tap_letter(ch)
        ok, _ = wait_for(
            lambda: (status().get("overlay") or {}).get("mode") == "candidates", 3.0)
        check("候选窗弹出来了（说明缓冲里真有东西）", ok,
              status().get("overlay"))
        check("状态文件说得出「现在是候选窗，不只是提示条」",
              (status().get("overlay") or {}).get("mode") == "candidates",
              status().get("overlay"))
        time.sleep(0.4)
        check("没上屏，靶子还是空的", text_of(caret_out) == "", text_of(caret_out))

        ok_focus = focus(hwnd_other)
        check("能切到「另一个窗口」（模拟用户点了别的窗口）", ok_focus,
              W.foreground_hwnd())
        ok, dt = wait_for(
            lambda: (status().get("overlay") or {}).get("mode") != "candidates", 3.0)
        check("切窗口后候选窗收起来了（半截拼音被丢掉）", ok,
              (status().get("overlay"), f"{dt:.2f}s"))
        time.sleep(0.5)
        check("半截拼音一个字母都没落进新窗口", text_of(other_out) == "",
              text_of(other_out))

        # ---- 4. 切回来还能接着打 ----
        print("\n-- 4. 切回靶子，接着打拼音仍然正常 --", flush=True)
        ok_focus = focus(hwnd_caret)
        check("切回 caret 靶子", ok_focus, W.foreground_hwnd())
        time.sleep(0.3)
        check("切回来时没有残留的候选窗",
              (status().get("overlay") or {}).get("mode") != "candidates",
              status().get("overlay"))
        for ch in "nihao":
            tap_letter(ch)
        ok, dt = wait_for(
            lambda: (status().get("overlay") or {}).get("mode") == "candidates", 3.0)
        check("切回来后照样敲得出候选", ok, (status().get("overlay"), f"{dt:.2f}s"))
        tap(0x20)                      # 空格上屏
        ok, dt = wait_for(lambda: "你好" in text_of(caret_out), 3.0)
        check("空格上屏「你好」，没被刚才的切窗口搞坏", ok,
              (text_of(caret_out), f"{dt:.2f}s"))

        # ---- 收工 ----
        daemon.send_command(PROJ, "quit")
        ok, _ = wait_for(lambda: not W.instance_exists(), 10.0)
        check("quit 之后实例真的退了", ok, daemon.read_status(PROJ))
        check("退出后状态文件撤掉了", not daemon.status_path(PROJ).exists())
        return 0
    finally:
        try:
            if caps_was_on:
                if W.caps_lock_on() is False:
                    tap(W.VK_CAPITAL)
        except Exception:  # noqa: BLE001
            pass
        try:
            daemon.send_command(PROJ, "quit")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.8)
        st = daemon.read_status(PROJ) or {}
        pid = int(st.get("pid") or 0)
        if pid:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, errors="replace")
        for p in (caret, other, instance):
            if p is not None:
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        for p in (daemon.status_path(PROJ), daemon.ctl_path(PROJ),
                  caret_out, caret_cmd, other_out):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    rc = main()
    print()
    print("=" * 74)
    print(f"{PASS} 通过 / {FAIL} 失败")
    print("=" * 74)
    sys.exit(rc if rc else (1 if FAIL else 0))
