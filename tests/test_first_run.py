# -*- coding: utf-8 -*-
"""新机器第一次双击 run.bat：缺依赖时必须当场报出来，而不是「黑框一闪」。

第 41 轮真机 bug 的回归测试。修之前是这样的：``main()`` 一开始就
``from t_ime.app import App``，而 App 一构造就要 pypinyin / jieba ——
``ModuleNotFoundError`` 发生在**日志建立之前**，子进程的 stderr 又接到
DEVNULL；``daemon.spawn_background`` 也从不看子进程死没死，立刻打印
「已在后台常驻」并退出 0。于是用户双击 ``run.bat``：黑框一闪，什么也没发生，
连日志文件都没有。

这里造一个「干净机器」目录：只有 ``main.py`` / ``run.bat`` /
``requirements.txt`` / ``t_ime/``，**没有** ``.venv``、没有 ``config.json``、
没有 ``pinyin_index.pkl``。子进程统一用 ``python -S`` 跑：``-S`` 跳过
site-packages，于是 pypinyin / jieba 一定不在 —— 与一台没装依赖的机器等价，
而且不会碰本机 ``.venv`` 里的依赖。
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
sys.path.insert(0, str(PROJ))

RESULTS: list[tuple[str, bool, str]] = []


def fix_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    mark = "ok  " if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not ok:
        line += f"  ({detail})"
    print(line, flush=True)


def make_fresh() -> Path:
    """造一个「没装依赖、没建过索引」的干净目录。"""
    d = Path(tempfile.mkdtemp(prefix="t-ime-first-run-"))
    for name in ("main.py", "run.bat", "requirements.txt"):
        shutil.copy2(PROJ / name, d / name)
    shutil.copytree(PROJ / "t_ime", d / "t_ime", ignore=shutil.ignore_patterns("__pycache__"))
    return d


def run(argv: list[str], cwd: Path, timeout: float = 180.0, use_shell: bool = False):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=timeout, env=env, shell=use_shell,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def spawn_with(cmd: list[str], argv: list[str], cwd: Path):
    """打桩 background_command 之后调 spawn_background，收它的 stderr。"""
    import t_ime.daemon as daemon

    orig = daemon.background_command
    daemon.background_command = lambda root, a: cmd  # type: ignore[assignment]
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = buf = io.StringIO()
    try:
        rc = daemon.spawn_background(cwd, argv)
    finally:
        daemon.background_command = orig  # type: ignore[assignment]
        sys.stdout, sys.stderr = old_out, old_err
    return rc, buf.getvalue()


def main() -> int:
    fix_console_encoding()
    print("== 第 41 轮：新机器第一次双击 run.bat ==", flush=True)

    fresh = make_fresh()
    log = fresh / "translate-ime.log"
    try:
        # 节 1：干净目录确实干净
        check("干净目录有 main.py", (fresh / "main.py").exists())
        check("干净目录有 run.bat", (fresh / "run.bat").exists())
        check("干净目录有 t_ime/", (fresh / "t_ime" / "pinyin.py").exists())
        check("干净目录没有 .venv", not (fresh / ".venv").exists())
        check("干净目录没有建过索引", not (fresh / "pinyin_index.pkl").exists())

        # 节 2：--check 是一份人话报告，不是 traceback
        rc, out = run([sys.executable, "-S", "main.py", "--check"], fresh)
        check("--check 退出码是 1（不是 0）", rc == 1, f"rc={rc}")
        check("--check 指出 jieba 词库没找到", "[FAIL] jieba dict.txt" in out, out[-400:])
        check("--check 说「多半是没装依赖」", "多半是没装依赖" in out)
        check("--check 给出装依赖的命令", "-m pip install -r requirements.txt" in out)
        check("--check 指出索引不可用的原因是缺依赖",
              "[FAIL] 索引可用" in out and "缺依赖 'pypinyin'" in out)
        check("--check 不甩 traceback（这份报告是给用户看的）", "Traceback" not in out)
        check("--check 没有留下索引文件", not (fresh / "pinyin_index.pkl").exists())

        # 节 3：前台启动 —— traceback 给开发者，人话给用户，两句都要
        rc, out = run([sys.executable, "-S", "main.py", "--foreground", "--no-hook"], fresh)
        check("前台启动失败退出码 1", rc == 1, f"rc={rc}")
        check("前台启动打印 traceback", "Traceback" in out)
        check("前台启动点名缺的是 pypinyin",
              "ModuleNotFoundError: No module named 'pypinyin'" in out)
        check("前台启动有那句人话", "[main] 启动失败：缺依赖 'pypinyin'" in out)
        check("前台启动给出 pip 指令", "pip install -r requirements.txt" in out)
        check("那句人话也进了日志文件（双击用户只看得到它）",
              log.exists() and "启动失败：缺依赖" in log.read_text(encoding="utf-8", errors="replace"))

        # 节 4：后台交棒 —— 子进程死了就不许报「已在后台常驻」
        log.write_text("[2026-01-01 00:00:00] ModuleNotFoundError: No module named 'pypinyin'\n"
                       "[2026-01-01 00:00:00] [main] 启动失败：缺依赖 'pypinyin'。\n",
                       encoding="utf-8")
        rc, err = spawn_with([sys.executable, "-c", "import sys; sys.exit(7)"],
                             ["--log", str(log)], fresh)
        check("子进程立刻退出 7 时 spawn_background 返回 1", rc == 1, f"rc={rc}")
        check("说出退出码", "退出码 7" in err, err[-300:])
        check("不谎报「已在后台常驻」", "已在后台常驻" not in err)
        check("把日志尾巴贴出来", "日志最后几行" in err and "缺依赖 'pypinyin'" in err)
        check("告诉用户去 --foreground 看完整原因", "run.bat --foreground" in err)

        # 子进程死了、连日志都没留下：最可能的原因要一句话说出来
        log.unlink()
        rc, err = spawn_with([sys.executable, "-c", "import sys; sys.exit(1)"], [], fresh)
        check("没有日志可贴时明说「连日志都没写出来」",
              rc == 1 and "连日志都没写出来" in err and "依赖没装" in err)

        rc, err = spawn_with([sys.executable, "-c", "import time; time.sleep(0.3)"], [], fresh)
        # 第 93 轮：进程活着不等于能打字。这个桩子从不写状态文件，所以要报
        # 「没就绪」（SPAWN_NOT_READY=4），而不是返回 0 让上层宣布启动成功。
        check("健康子进程活着、但一直没报到就绪 → 返回 4（没就绪），不是 0",
              rc == 4, f"rc={rc}")
        check("只有子进程活着才报「已在后台常驻」", "已在后台常驻" in err)

        # 节 5：真跑一遍 run.bat（--foreground，不会起后台进程）
        rc, out = run(["cmd", "/c", "run.bat --foreground"], fresh)
        check("run.bat 把退出码传出来", rc == 1, f"rc={rc}")
        check("run.bat 输出里有人话", "启动失败：缺依赖" in out, out[-400:])
        check("run.bat 最后打印 exit code 1", "[run.bat] exit code 1" in out)
        check("run.bat 给了修法", "pip install -r requirements.txt" in out)
        check("run.bat 没有自己去装依赖（不替用户做决定）",
              "Collecting" not in out and "Successfully installed" not in out)

        # 节 6：尾部读取的小工具
        import t_ime.daemon as daemon

        check("_tail_lines 文件不存在给空列表", daemon._tail_lines(fresh / "nope.log") == [])
        long_log = fresh / "long.log"
        long_log.write_text("".join(f"line{i}\n" for i in range(20)), encoding="utf-8")
        tail = daemon._tail_lines(long_log)
        check("_tail_lines 只回最后 12 行", len(tail) == 12 and tail[-1] == "line19",
              f"len={len(tail)}")
        check("_tail_lines 去掉行尾换行", all(not ln.endswith("\n") for ln in tail))
        check("_tail_lines 空文件给空列表", daemon._tail_lines(fresh / "empty.log") == [])
    finally:
        shutil.rmtree(fresh, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print()
    print(f"结果：{passed}/{len(RESULTS)} 通过", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
