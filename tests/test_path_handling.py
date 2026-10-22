# -*- coding: utf-8 -*-
"""把程序放在「带空格、带中文」的目录里，还能不能正常用？

真实用户很常把程序塞在 ``Program Files``、``桌面``、``我的软件``、``D:\\工具``
这类路径下，而仓库自己的路径虽然含中文（``D:\\dsh-work\\杂活\\translate-ime``）
却**没有空格** —— 引号少一个、路径拼接少一层，在这两种文件夹里才会炸。

这个测试把 ``main.py`` / ``run.bat`` / ``t_ime/`` 拷到临时目录
（``…\\Temp\\输入法 路径 xxxx\\带 空格 的 程序``），然后真跑：

  1. ``main.py --check``：索引建在带空格的路径里、配置/状态文件路径都对；
  2. ``main.py --show-config`` / ``--doctor-quick``：路径原样打出来、报告落在那儿；
  3. ``run.bat --check``：``cd /d "%~dp0"`` 与引号没被空格弄坏；
  4. ``--background`` 常驻：状态文件在空格路径里、跑的是 pythonw、钩子装上；
  5. 真按键打一遍 ``nihao`` + 空格：上屏「你好」并退出；
  6. ``autostart_command()`` 的形状：四个参数各自被引号包住（空格路径下 8 个引号）。

``run.bat`` 需要 ``.venv`` 才能跑：用 ``mklink /J`` 把仓库的 venv 联进来
（目录联接不需要管理员权限）。联不上就把 run.bat 那组标 SKIP，其余照跑。

直接跑：``python tests/test_path_handling.py``（约 20 秒）
"""
import ctypes
import json
import shutil
import subprocess
import sys
import tempfile
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
u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]

TITLE = "T-IME-CARET"
VK_SPACE = 0x20

PASS = 0
FAIL = 0
SKIP = 0

ENV = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
}


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok ] {label}", flush=True)
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  <-- {extra!r}", flush=True)


def skip(label, why):
    global SKIP
    SKIP += 1
    print(f"  [skip] {label}  <-- {why}", flush=True)


def run(argv, cwd, timeout=180.0):
    import os

    env = dict(os.environ)
    env.update(ENV)
    return subprocess.run(
        argv, cwd=str(cwd), capture_output=True, timeout=timeout,
        text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, env=env,
    )


def kill(pid):
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def scan(vk):
    return W.user32.MapVirtualKeyW(int(vk), W.MAPVK_VK_TO_VSC)


def tap(vk, gap=0.05):
    # extra_info=0：钩子眼里这是「别的程序转发的按键」，会当真实按键处理。
    W._send([W._kb_input(int(vk), scan(vk), 0, 0)])
    time.sleep(gap)
    W._send([W._kb_input(int(vk), scan(vk), W.KEYEVENTF_KEYUP, 0)])
    time.sleep(0.07)


def fg_title():
    hwnd = u32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    u32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 256)
    return hwnd, buf.value


def read_dump(out):
    for _ in range(6):
        try:
            got = json.loads(out.read_text(encoding="utf-8"))
            if isinstance(got, dict) and "insert" in got:
                return got
        except Exception:
            pass
        time.sleep(0.05)
    return {"text": None, "cmd": None, "insert": None}


def clear(cmd_path, out, n):
    cmd_path.write_text(f"clear {n}", encoding="utf-8")
    for _ in range(60):
        got = read_dump(out)
        if got.get("cmd") == f"clear {n}":
            return got
        time.sleep(0.05)
    return read_dump(out)


def wait_status(root, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        st = D.read_status(root)
        if st and st.get("pid"):
            return st
        time.sleep(0.2)
    return None


def wait_gone(root, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if not (D.read_status(root) or {}).get("pid"):
            return True
        time.sleep(0.2)
    return False


def make_app_copy():
    base = Path(tempfile.mkdtemp(prefix="输入法 路径 "))
    app = base / "带 空格 的 程序"
    app.mkdir()
    shutil.copytree(ROOT / "t_ime", app / "t_ime",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("main.py", "run.bat", "requirements.txt", "config.example.json"):
        shutil.copy2(ROOT / name, app / name)
    return base, app


def link_venv(app):
    """把仓库的 venv 用目录联接挂到副本里，run.bat 才找得到 ``.venv``。"""
    target = app / ".venv"
    try:
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(ROOT / ".venv")],
            capture_output=True, timeout=30,
            text=True, encoding="utf-8", errors="replace",
        )
    except Exception:
        return False, "mklink 跑不起来"
    ok = (app / ".venv" / "Scripts" / "python.exe").exists()
    return ok, (proc.stdout + proc.stderr).strip()


def main():
    print("== 空格/中文路径真机验证 ==", flush=True)
    if W.instance_exists():
        print("[paths] 仓库根已经有实例在跑（可能用户自己在用），这轮跳过", flush=True)
        return 0

    base, app = make_app_copy()
    print(f"\n副本目录：{app}", flush=True)
    out = base / "target_out.json"
    cmd = base / "target_cmd.txt"
    target = None
    pid = None
    try:
        print("\n-- 1. 副本形状 --", flush=True)
        check("目录名里同时有空格的与中文", " " in str(app) and any(ord(c) > 127 for c in str(app)),
              str(app))
        check("t_ime 整个拷过来了", (app / "t_ime" / "core.py").exists(), app)
        check("main.py / run.bat 都在", (app / "main.py").exists() and (app / "run.bat").exists(), app)
        check("副本里没有 config.json（走内置默认值）", not (app / "config.json").exists(), app)

        print("\n-- 2. main.py --check：索引与路径 --", flush=True)
        r = run([sys.executable, str(app / "main.py"), "--check"], cwd=app)
        text = (r.stdout or "") + (r.stderr or "")
        check("--check exit 0", r.returncode == 0, (r.returncode, text[-400:]))
        check("--check 说全部通过", "全部通过" in text, text[-400:])
        check("--check 认得这个空格路径的索引", str(app / "pinyin_index.pkl") in text,
              [ln for ln in text.splitlines() if "索引" in ln])
        idx = app / "pinyin_index.pkl"
        check("索引文件真落在这个目录里", idx.exists() and idx.stat().st_size > 100_000,
              idx.stat().st_size if idx.exists() else None)
        check("--check 没有留下状态文件", not (app / "translate-ime.status.json").exists(), app)
        check("--check 的输出里没有 [app] 日志", "[app]" not in text, text[:200])

        print("\n-- 3. --show-config / --doctor-quick --", flush=True)
        r = run([sys.executable, str(app / "main.py"), "--show-config"], cwd=app)
        text = (r.stdout or "") + (r.stderr or "")
        check("--show-config exit 0", r.returncode == 0, (r.returncode, text[-300:]))
        check("--show-config 打出的配置文件路径是空格路径",
              str(app / "config.json") in text, [ln for ln in text.splitlines() if "配置文件" in ln])
        check("--show-config 打出了 API key 来源", "API key" in text, text[:200])

        r = run([sys.executable, str(app / "main.py"), "--doctor-quick"], cwd=app)
        text = (r.stdout or "") + (r.stderr or "")
        report = app / "translate-ime-doctor.txt"
        check("--doctor-quick exit 0", r.returncode == 0, (r.returncode, text[-400:]))
        check("doctor 报告落在空格路径里", report.exists() and report.stat().st_size > 200,
              report.stat().st_size if report.exists() else None)
        check("报告里写着「报告已写入」这路径", str(report) in text,
              [ln for ln in text.splitlines() if "报告" in ln])
        if report.exists():
            body = report.read_text(encoding="utf-8", errors="replace")
            check("报告内容里带着空格路径的根目录", str(app) in body, body[:200])
        else:
            skip("报告内容里带着空格路径的根目录", "报告没写出来")

        print("\n-- 4. run.bat --check：引号与 cd /d --", flush=True)
        linked, why = link_venv(app)
        if linked:
            r = run(["cmd", "/c", "run.bat", "--check"], cwd=app)
            text = (r.stdout or "") + (r.stderr or "")
            check("run.bat --check exit 0", r.returncode == 0, (r.returncode, text[-500:]))
            check("run.bat --check 说全部通过", "全部通过" in text, text[-400:])
            # run.bat 认 .venv\Scripts\python.exe；这个 python 的 jieba 在 .venv 的
            # site-packages 里，--check 会把那条路径打出来 —— 能看见它就说明
            # run.bat 用的是 venv 解释器，而不是回落到系统的 python。
            check("run.bat 用的是 venv 解释器（能看见 venv 的 site-packages）",
                  "site-packages" in text and "jieba" in text,
                  [ln for ln in text.splitlines() if "jieba" in ln])
            check("run.bat 没在副本里留下状态文件",
                  not (app / "translate-ime.status.json").exists(), app)
        else:
            for label in ("run.bat --check exit 0", "run.bat --check 说全部通过",
                          "run.bat 用的是副本里的 .venv", "run.bat 没在副本里留下状态文件"):
                skip(label, f"目录联接建不起来：{why}")

        print("\n-- 5. --background 常驻：状态/日志都在空格路径 --", flush=True)
        r = run([sys.executable, str(app / "main.py"), "--background"], cwd=app, timeout=60)
        text = (r.stdout or "") + (r.stderr or "")
        check("启动器 exit 0", r.returncode == 0, (r.returncode, text[-400:]))
        check("启动器说已在后台启动", "已在后台启动" in text, text[-300:])
        st = wait_status(app)
        check("实例起来了（状态文件有 pid）", bool(st and st.get("pid")), st)
        if st:
            pid = int(st["pid"])
            check("跑的是 pythonw（无控制台）",
                  Path(W.process_name(pid)).name.lower() == "pythonw.exe", W.process_name(pid))
            check("状态文件里的 root 就是空格路径", str(st.get("root")) == str(app),
                  st.get("root"))
            check("日志路径也在空格路径里",
                  str(st.get("log")) == str(app / "translate-ime.log"), st.get("log"))
            check("钩子装上了", st.get("hook") is True, st.get("hook"))
            check("实例是启用状态", st.get("enabled") is True, st.get("enabled"))

        print("\n-- 6. 真按键：空格路径下的实例能上屏 --", flush=True)
        target = subprocess.Popen(
            [sys.executable, str(HERE / "caret_target.py"), str(out), str(cmd)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        hwnd = None
        for _ in range(100):
            hwnd = u32.FindWindowW(None, TITLE)
            if hwnd:
                break
            time.sleep(0.1)
        check("靶子窗口起来了", bool(hwnd), hwnd)
        if hwnd:
            u32.ShowWindow(ctypes.c_void_p(hwnd), 9)
            u32.SetForegroundWindow(ctypes.c_void_p(hwnd))
            for _ in range(30):
                if u32.GetForegroundWindow() == hwnd:
                    break
                time.sleep(0.1)
            check("靶子是前台窗口", u32.GetForegroundWindow() == hwnd, fg_title())
            time.sleep(0.5)
            clear(cmd, out, 1)
            for ch in "nihao":
                tap(ord(ch.upper()))
            time.sleep(0.45)
            st = D.read_status(app) or {}
            ov = st.get("overlay") or {}
            # 状态文件里没有 buffer 字段（那属于内部实现），能证明「字母进到了输入法里」
            # 的是：候选窗已经显出来。
            check("打字母时候选窗可见", ov.get("visible") is True, ov)
            tap(VK_SPACE)
            time.sleep(0.8)
            got = read_dump(out)
            check("空格把「你好」上屏到靶子", got.get("text") == "你好", got.get("text"))
            st = D.read_status(app) or {}
            check("上屏没有失败记录", int(st.get("commit_failures") or 0) == 0,
                  st.get("commit_failures"))
            hk = st.get("hook_info") or {}
            # 注入的键带 extra_info=0（钩子眼里是「别的程序转发」），产品把它们当真实
            # 按键处理；real 只统计硬件键，所以这里看 foreign_injected。
            check("钩子把这 5 个字母都收进来了", int(hk.get("foreign_injected") or 0) >= 5, hk)
        else:
            for label in ("靶子是前台窗口", "打字母时候选窗可见",
                          "空格把「你好」上屏到靶子", "上屏没有失败记录",
                          "钩子把这 5 个字母都收进来了"):
                skip(label, "靶子没起来")

        print("\n-- 7. 退出：日志落在空格路径 --", flush=True)
        D.send_command(app, "quit")
        check("quit 之后实例真的走了（状态文件也撤了）", wait_gone(app), D.read_status(app))
        log = app / "translate-ime.log"
        check("日志写在空格路径里", log.exists() and log.stat().st_size > 0,
              log.stat().st_size if log.exists() else None)
        if log.exists():
            body = log.read_text(encoding="utf-8", errors="replace")
            check("日志里记了上屏与退出", "已上屏：你好" in body and "收到命令：quit" in body,
                  body[-300:])
        else:
            check("日志里记了上屏与退出", False, log)

        print("\n-- 8. 自启命令的形状（空格路径别漏引号）--", flush=True)
        ac = D.autostart_command(app)
        check("autostart_command 非空", bool(ac), ac)
        if ac:
            check("最外层有引号", ac.startswith('"') and ac.endswith('"'), ac)
            check("四个参数各自被引号包住（8 个引号）", ac.count('"') == 8, ac)
            check("带上了副本的 main.py", str(app / "main.py") in ac, ac)
            check("带上了 --background --detached",
                  "--background" in ac and "--detached" in ac, ac)
        bc = D.background_command(app, [])
        check("background_command 是参数列表（没有引号残留）",
              bc == [str(D.pythonw_path()), str(app / "main.py"), "--background", "--detached"], bc)
    finally:
        if target is not None:
            try:
                target.kill()
                target.wait(timeout=5)
            except Exception:
                pass
        if pid:
            kill(pid)
        st = D.read_status(app) or {}
        if st.get("pid"):
            kill(int(st["pid"]))
        try:
            (app / "translate-ime.status.json").unlink(missing_ok=True)
        except Exception:
            pass
        shutil.rmtree(base, ignore_errors=True)

    total = PASS + FAIL
    print(f"\n结果：{PASS}/{total} 通过" + (f"（{SKIP} 项跳过）" if SKIP else ""), flush=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
