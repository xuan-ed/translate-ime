# -*- coding: utf-8 -*-
"""引号键的真机验证：**真实例 + 真靶子窗口 + 真按键注入**。

为什么要一个真机测试：第 36 轮补做「拼音分词符」时发现了一个必须靠真按键才看得出来
的 bug —— 空缓冲按 ``'``（``VK_OEM_7``）会被无条件吞进拼音缓冲，于是：

  * 用户按了一下引号键，窗口里什么都没出现（键被吞了），候选窗却弹了出来；
  * 再按空格，那个 ``'`` 被当成候选「打」进文档 —— 输入法凭空吐出一个引号。

单元测试能验状态机（``tests/test_core.py`` 里那 17 项），但验不了「这个键最后有没有
真的进到**另一个进程**的文本框里」：那取决于钩子回调返回 ``False`` 之后系统怎么把键
继续分发下去，只有真按键才作数。所以这里跟 ``test_hotkeys_live`` 一样：起一个真的
常驻实例（``run.bat``，和用户双击同一个入口，装的是产品自己的钩子），再起
``tests/caret_target.py`` 当靶子，用 ``SendInput`` 敲真键，读靶子 dump 的文本。

两件事：
  A. 空缓冲按 ``'`` → 那个引号原样落进目标窗口，不留拼音缓冲。
  B. 正在打 ``ni`` 时按 ``'`` → 当分词符被吞（窗口里一个字都没有），再打 ``hao``
     按空格，上屏「你好」。

直接跑：``python tests/test_quote_live.py``（约 20 秒）
"""
import ctypes
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from t_ime import daemon as D  # noqa: E402
from t_ime import winapi as W  # noqa: E402

u32 = ctypes.windll.user32
u32.FindWindowW.restype = ctypes.c_void_p
u32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
u32.GetForegroundWindow.restype = ctypes.c_void_p

TITLE = "T-IME-CARET"
OUT = HERE / "_quote_target_out.json"
CMD = HERE / "_quote_target_cmd.txt"
VK_QUOTE = 0xDE

PASS = 0
FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok ] {label}", flush=True)
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  <-- {extra!r}", flush=True)


def read_dump():
    for _ in range(6):
        try:
            got = json.loads(OUT.read_text(encoding="utf-8"))
            if isinstance(got, dict) and "insert" in got:
                return got
        except Exception:
            pass
        time.sleep(0.05)
    return {"text": None, "cmd": None, "insert": None}


def clear(n):
    """清空靶子（``cmd`` 回显确认那条 clear 真的落地了，见 caret_target 的注释）。"""
    CMD.write_text(f"clear {n}", encoding="utf-8")
    for _ in range(60):
        got = read_dump()
        if got.get("cmd") == f"clear {n}":
            return got
        time.sleep(0.05)
    return read_dump()


def scan(vk):
    return W.user32.MapVirtualKeyW(int(vk), W.MAPVK_VK_TO_VSC)


def tap(vk, gap=0.05):
    # extra_info=0：钩子眼里这是「别的程序转发的按键」（改键器/宏），会当真实按键处理。
    # 不能用默认的 T_IME_INJECT_TAG —— 那是我们自己回灌的标记，产品会直接忽略。
    W._send([W._kb_input(int(vk), scan(vk), 0, 0)])
    time.sleep(gap)
    W._send([W._kb_input(int(vk), scan(vk), W.KEYEVENTF_KEYUP, 0)])
    time.sleep(0.07)


def wait_status(timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        st = D.read_status(ROOT)
        if st and st.get("pid"):
            return st
        time.sleep(0.2)
    return None


def main():
    print("== 引号键真机验证 ==", flush=True)
    if W.instance_exists():
        print("[quote] 已经有实例在跑（可能用户自己在用），这轮跳过", flush=True)
        return 0

    for p in (OUT, CMD):
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    # 真实例用的就是仓库根的 config.json，正常退出时会把学到的词写进
    # user_boosts.json。跑完必须还原：别把测试打的那几个词留在仓库里，也别抹掉
    # 用户自己原有的自学习结果（第 39 轮全量回归之后在仓库根发现残留才补的）。
    boosts_path = ROOT / "user_boosts.json"
    boosts_before = boosts_path.read_bytes() if boosts_path.exists() else None

    print("\n-- 1. 用 run.bat 起一个真实例 --", flush=True)
    run = subprocess.run(["cmd", "/c", "run.bat"], cwd=str(ROOT),
                         capture_output=True, timeout=40)
    check("run.bat 启动器 exit 0", run.returncode == 0, run.returncode)
    st = wait_status()
    check("实例起来了（状态文件里有 pid）", bool(st and st.get("pid")), st)
    if not st:
        return 1
    check("产品自己的钩子装上了", st.get("hook") is True, st.get("hook"))
    check("跑的是 pythonw（没有控制台窗口）",
          Path(W.process_name(st["pid"])).name.lower() == "pythonw.exe",
          W.process_name(st["pid"]))
    check("实例处于启用状态", st.get("enabled") is True, st.get("enabled"))

    target = subprocess.Popen(
        [sys.executable, str(HERE / "caret_target.py"), str(OUT), str(CMD)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        print("\n-- 2. 靶子窗口就位 --", flush=True)
        hwnd = None
        for _ in range(100):
            hwnd = u32.FindWindowW(None, TITLE)
            if hwnd:
                break
            time.sleep(0.1)
        check("靶子窗口起来了", bool(hwnd), hwnd)
        if not hwnd:
            return 1
        u32.ShowWindow(ctypes.c_void_p(hwnd), 9)
        u32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        time.sleep(0.6)
        check("靶子是前台窗口", u32.GetForegroundWindow() == hwnd,
              (u32.GetForegroundWindow(), hwnd))

        print("\n-- 3. 空缓冲按引号：必须原样进窗口 --", flush=True)
        clear(1)
        tap(VK_QUOTE)
        time.sleep(0.5)
        got = read_dump()
        check("引号落进了目标窗口（修之前会被吞掉）", got.get("text") == "'", got.get("text"))
        st2 = D.read_status(ROOT) or {}
        check("没有留下拼音缓冲", not st2.get("buffer"), st2.get("buffer"))

        print("\n-- 4. 打拼音时引号当分词符：必须被吞 --", flush=True)
        clear(2)
        tap(ord("N"))
        tap(ord("I"))
        time.sleep(0.4)
        check("打拼音时字母被吞（窗口里还没东西）", read_dump().get("text") == "",
              read_dump().get("text"))
        tap(VK_QUOTE)
        time.sleep(0.35)
        check("打拼音时引号被当分词符吞掉（窗口里还没东西）",
              read_dump().get("text") == "", read_dump().get("text"))
        for vk in (ord("H"), ord("A"), ord("O")):
            tap(vk)
        time.sleep(0.45)
        tap(W.VK_SPACE)
        time.sleep(0.7)
        got = read_dump()
        check("空格把「你好」上屏到目标窗口", got.get("text") == "你好", got.get("text"))
        st3 = D.read_status(ROOT) or {}
        check("上屏之后拼音缓冲清空", not st3.get("buffer"), st3.get("buffer"))
        check("上屏没有失败记录", int(st3.get("commit_failures") or 0) == 0,
              st3.get("commit_failures"))

        print("\n-- 5. 收摊 --", flush=True)
        D.send_command(ROOT, "quit")
        gone = False
        for _ in range(75):
            if not (D.read_status(ROOT) or {}).get("pid"):
                gone = True
                break
            time.sleep(0.2)
        check("quit 之后实例真的走了（状态文件也撤了）", gone, D.read_status(ROOT))
    finally:
        try:
            target.kill()
        except Exception:
            pass
        try:
            target.wait(timeout=5)      # 不 wait 就 unlink，靶子那侧的文件句柄还没放开
        except Exception:
            pass
        for p in (OUT, CMD):
            for _ in range(20):
                try:
                    p.unlink()
                    break
                except FileNotFoundError:
                    break
                except PermissionError:   # 靶子刚被杀、句柄还没放开（第 39 轮偶发）
                    time.sleep(0.1)
        if boosts_before is None:
            boosts_path.unlink(missing_ok=True)
        else:
            boosts_path.write_bytes(boosts_before)

    print(f"\n结果：{PASS}/{PASS + FAIL} 通过", flush=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
