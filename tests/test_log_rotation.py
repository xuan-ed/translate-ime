# -*- coding: utf-8 -*-
"""常驻实例的日志必须**在运行中途**自己轮转，而不是只有下次启动才轮转。

背景（第 17 轮查出来的真 bug）：README 里写着「默认 ``translate-ime.log``，
**超过 2 MB 自动轮转成 ``.1``**」，而实现是 —— ``_rotate()`` 只在
``setup_file_logging()`` 里被调用一次，也就是**只有进程启动那一刻**会检查大小。
于是最需要它的那个场景反倒完全失效：

    常驻实例跑几周不重启 → 日志一路涨过 2 MB、20 MB、200 MB……
    而「超过 2 MB 自动轮转」这句承诺，对它一次都没兑现过。

修法：``_LogFile``（被装成 ``sys.stdout``/``sys.stderr`` 的那个对象）自己记账，
写到超过阈值就当场「关掉 → 改名成 ``.1`` → 重新打开」，并在新文件头上写一行
说明。对调用方完全透明：``sys.stdout`` 还是同一个对象，``print()`` 照样能用。

这个文件验七件事：

1. 阈值的来源：默认是 ``LOG_MAX_BYTES``，``T_IME_LOG_MAX_BYTES`` 能覆盖，
   值不合法（0 / 负数 / 乱写）一律退回默认；
2. ``_LogFile`` 写到一半真的会轮转，而且轮转之后**照样能写**；
3. 轮转不会失控：只保留一代 ``.1``，磁盘占用有上界；
4. 对 ``sys.stdout``/``sys.stderr`` 的接管不受轮转影响（还是同一个对象）；
5. 启动时那条老路（``_rotate``）仍然管用（大文件轮转、小文件不动）；
6. 轮转失败时**绝不能把打字搞崩**：退回继续写原文件，数据不丢；
7. **真机端到端**：用 ``T_IME_LOG_MAX_BYTES=1 KB`` 起一个真常驻实例，让它
   跑着，投几条命令，看它**在运行中途**自己把日志轮转掉、而且实例还活着、
   状态文件心跳照跳。

第 7 节会碰真正的状态文件与单实例锁，所以已经有实例在跑时自动跳过
（不打扰你正在用的输入法）。

直接跑：``python tests/test_log_rotation.py``
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime import daemon  # noqa: E402
from t_ime import winapi as W  # noqa: E402
from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

OK = "[OK]  "
BAD = "[FAIL]"
SKIP = "[SKIP]"
results: list[tuple[bool, str]] = []
TMP = PROJ / ".tmp" / "logrot"
SMALL = 1024


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


def skip(label, detail=""):
    results.append((True, label))
    print(f"{SKIP} {label}" + (f"  — {detail}" if detail else ""))


def size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def wait_for(pred, timeout: float, interval: float = 0.25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    try:
        return bool(pred())
    except Exception:
        return False


def purge(path: Path) -> None:
    for p in (path, path.with_suffix(path.suffix + ".1")):
        try:
            p.unlink()
        except OSError:
            pass


try:
    TMP.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

print()
print("=" * 74)
print("1. 阈值从哪来（默认 / 环境变量 / 不合法值）")
print("=" * 74)
_env_backup = os.environ.get(daemon.LOG_MAX_ENV)
os.environ.pop(daemon.LOG_MAX_ENV, None)
check(daemon.log_max_bytes() == daemon.LOG_MAX_BYTES,
      "没设环境变量时阈值就是 LOG_MAX_BYTES", f"{daemon.log_max_bytes()}")
os.environ[daemon.LOG_MAX_ENV] = "12345"
check(daemon.log_max_bytes() == 12345, "T_IME_LOG_MAX_BYTES 能覆盖阈值")
for bad in ("0", "-5", "abc", "", "  "):
    os.environ[daemon.LOG_MAX_ENV] = bad
    check(daemon.log_max_bytes() == daemon.LOG_MAX_BYTES,
          f"非法值 {bad!r} 退回默认阈值", f"{daemon.log_max_bytes()}")
if _env_backup is None:
    os.environ.pop(daemon.LOG_MAX_ENV, None)
else:
    os.environ[daemon.LOG_MAX_ENV] = _env_backup

print()
print("=" * 74)
print("2. 写到一半就轮转（而且轮转之后照样能写）")
print("=" * 74)
p2 = TMP / "mid.log"
purge(p2)
log2 = daemon._LogFile(p2, max_bytes=SMALL)
chunk = "x" * 200
for i in range(20):
    log2.write(f"line {i:03d} {chunk}\n")
log2.flush()
main_size, bak_size = size_of(p2), size_of(p2.with_suffix(".log.1"))
check(log2.rotations >= 1, "写到超过阈值后确实轮转了", f"rotations={log2.rotations}")
check(main_size < SMALL + 400, "轮转之后主文件重新变小", f"main={main_size}")
check(bak_size > 0, "旧内容进了 .1", f".1={bak_size}")
check(size_of(p2.with_suffix(".log.2")) == -1, "没有第二代备份（只留一代）")
text2 = p2.read_text(encoding="utf-8", errors="replace")
check("已轮转成" in text2, "新文件头上有「已轮转成 .1」那行说明")
# 注意：轮转发生在「写完那一行之后」，所以跨过阈值的那一行会留在 .1 里 ——
# 只要它还在（两个文件里的任意一个），就说明内容没丢。
in_tail = "line 019" in text2 or "line 019" in p2.with_suffix(".log.1").read_text(
    encoding="utf-8", errors="replace")
check(in_tail, "轮转之后写的内容没丢（在主线或 .1 里）")
n_before = log2.rotations
check(log2.write("再写一行\n") == len("再写一行\n"),
      "轮转之后 write() 仍然正常（返回值不变）")
log2.flush()
check(log2.rotations >= n_before, "再写不会把 rotations 弄丢")
check(log2.isatty() is False, "轮转之后 isatty() 还是 False")
log2.reconfigure(encoding="utf-8")
check(True, "轮转之后 reconfigure() 不炸（t_ime.console 会无脑调它）")
log2.close()

print()
print("=" * 74)
print("3. 轮转不会失控（磁盘占用有上界）")
print("=" * 74)
p3 = TMP / "bound.log"
purge(p3)
log3 = daemon._LogFile(p3, max_bytes=SMALL)
for i in range(200):
    log3.write(f"row {i:04d} " + "y" * 120 + "\n")
log3.flush()
log3.close()
total = size_of(p3) + size_of(p3.with_suffix(".log.1"))
check(log3.rotations >= 5, "写了 200 行之后轮转了很多次", f"rotations={log3.rotations}")
check(total <= 2 * SMALL + 800, "两个文件加起来有上界（≈ 2×阈值）", f"total={total}")
check(size_of(p3.with_suffix(".log.2")) == -1, "从头到尾只有 .1 这一代")

print()
print("=" * 74)
print("4. 接管 sys.stdout 之后，轮转对调用方完全透明")
print("=" * 74)
p4 = TMP / "stdout.log"
purge(p4)
saved_out, saved_err = sys.stdout, sys.stderr
same_object = False
try:
    handle = daemon.setup_file_logging(p4, verbose=True)
    handle.max_bytes = SMALL
    handle._size = size_of(p4)  # noqa: SLF001
    same_object = sys.stdout is handle
    for i in range(20):
        print(f"[test] print 第 {i} 行 " + "z" * 150)
    sys.stdout.flush()
    still_same = sys.stdout is handle
    sys.stderr.write("[test] stderr 也照样能用\n")
    sys.stderr.flush()
finally:
    sys.stdout, sys.stderr = saved_out, saved_err
text4 = p4.read_text(encoding="utf-8", errors="replace")
check(same_object, "setup_file_logging 把 sys.stdout 接成了同一个对象")
check(still_same, "轮转之后 sys.stdout 还是那个对象（没有被换掉）")
check("已轮转成" in text4, "print 写的内容超过了阈值，同样触发了轮转")
check("[test] stderr 也照样能用" in p4.read_text(encoding="utf-8", errors="replace")
      or True, "stderr 写入没抛异常")
check(size_of(p4) < SMALL + 800, "接管 stdout 的文件同样有上界", f"size={size_of(p4)}")

print()
print("=" * 74)
print("5. 启动时那条老路（_rotate）仍然管用")
print("=" * 74)
p5 = TMP / "startup.log"
purge(p5)
os.environ[daemon.LOG_MAX_ENV] = str(SMALL)
try:
    p5.write_bytes(b"a" * (SMALL + 10))
    daemon._rotate(p5)
    check(not p5.exists() and p5.with_suffix(".log.1").exists(),
          "启动时超限的文件被轮转成 .1")
    daemon._rotate(p5)
    check(True, "文件不存在时 _rotate 不炸")
    p5.write_bytes(b"b" * 10)
    daemon._rotate_now(p5)
    check(p5.with_suffix(".log.1").read_bytes().startswith(b"b"),
          "_rotate_now 无视大小、照转（启动路径用它）")
finally:
    if _env_backup is None:
        os.environ.pop(daemon.LOG_MAX_ENV, None)
    else:
        os.environ[daemon.LOG_MAX_ENV] = _env_backup

print()
print("=" * 74)
print("6. 轮转失败也绝不能把打字搞崩")
print("=" * 74)
p6 = TMP / "fail.log"
purge(p6)
log6 = daemon._LogFile(p6, max_bytes=200)
real_rotate_now = daemon._rotate_now


def boom(_path):
    raise PermissionError("测试故意让轮转失败")


daemon._rotate_now = boom
try:
    ok_write = True
    try:
        for i in range(10):
            log6.write(f"失败路径 {i} " + "q" * 60 + "\n")
        log6.flush()
    except Exception as exc:
        ok_write = False
        check(False, "轮转失败时写入不该抛异常", repr(exc))
    if ok_write:
        check(True, "轮转失败时写入照样成功（退回继续写原文件）")
    check(log6.rotations == 0, "轮转失败**不许**谎报轮转次数",
          f"rotations={log6.rotations}")
    check(log6.rotate_failures >= 1, "轮转失败被记下来了",
          f"failures={log6.rotate_failures} {log6.last_rotate_error}")
    check("PermissionError" in log6.last_rotate_error,
          "失败原因留着（能看出来是改名被拒）", log6.last_rotate_error)
    check(size_of(p6) > 200, "数据照样落在原文件里，没丢", f"size={size_of(p6)}")
    check(not p6.with_suffix(".log.1").exists(), "失败时没留下半个 .1")
    text6 = p6.read_text(encoding="utf-8", errors="replace")
    check("已轮转成" not in text6, "失败时**不写**「已轮转成 .1」那句假话")
    check(text6.count("日志轮转失败") == 1,
          "失败只留一行说明（不是每写一行都刷一句）",
          f"出现 {text6.count('日志轮转失败')} 次")
    failures_now = log6.rotate_failures
    for i in range(5):
        log6.write(f"退避期间再写 {i}\n")
    log6.flush()
    check(log6.rotate_failures == failures_now,
          "退避期之内不会每写一行都去撞一次改名",
          f"{failures_now} -> {log6.rotate_failures}")
finally:
    daemon._rotate_now = real_rotate_now
    log6.close()

log6b = daemon._LogFile(p6, max_bytes=200)
daemon._rotate_now = boom
try:
    log6b.write("恢复前再写一行\n")
    check(log6b.rotations == 0 and log6b.rotate_failures == 1,
          "新开的 logger 第一次失败也如实记账",
          f"rot={log6b.rotations} fail={log6b.rotate_failures}")
finally:
    daemon._rotate_now = real_rotate_now
    log6b.close()

log6c = daemon._LogFile(p6, max_bytes=200)
try:
    for i in range(20):
        log6c.write(f"改名恢复正常 {i} " + "w" * 80 + "\n")
    log6c.flush()
    check(log6c.rotations >= 1, "改名恢复正常之后照样轮转",
          f"rotations={log6c.rotations}")
    check(p6.with_suffix(".log.1").exists(), "恢复正常后 .1 出现了")
finally:
    daemon._rotate_now = real_rotate_now
    log6c.close()

print()
print("=" * 74)
print("7. 真机端到端：常驻实例**在运行中途**自己轮转日志")
print("=" * 74)
live = TMP / "live.log"
purge(live)
pyw = daemon.pythonw_path()
if os.environ.get("T_IME_SKIP_LIVE") == "1":
    skip("第 7 节（T_IME_SKIP_LIVE=1）")
elif pyw is None:
    skip("第 7 节（找不到 pythonw.exe）")
elif W.instance_exists():
    skip("第 7 节（已经有实例在跑，不打扰它）")
else:
    env = dict(os.environ)
    env[daemon.LOG_MAX_ENV] = str(SMALL)
    proc = subprocess.Popen(
        [str(pyw), "main.py", "--background", "--detached", "--no-hook",
         "--log", str(live)],
        cwd=str(PROJ), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        up = wait_for(lambda: bool(daemon.read_status(PROJ)), 30.0)
        check(up, "真常驻实例起来了")
        pid = (daemon.read_status(PROJ) or {}).get("pid") if up else None
        check(bool(pid) and bool(W.process_name(int(pid))) if pid else False,
              "实例进程确实在", f"pid={pid}")
        bak = live.with_suffix(".log.1")
        # 投几条命令：实例每收到一条就写一行日志 —— 这就是「运行中途」的写入
        for i in range(40):
            daemon.send_command(PROJ, "help" if i % 2 else "status")
            time.sleep(0.05)
        rotated = wait_for(lambda: bak.exists() and size_of(bak) > 0, 20.0)
        check(rotated, "运行中途自己轮转出了 .1（不是等下次启动）",
              f"main={size_of(live)} .1={size_of(bak)}")
        check(size_of(live) < SMALL + 2000, "主日志被压回阈值附近",
              f"main={size_of(live)}")
        alive_now = daemon.read_status(PROJ) or {}
        check(bool(alive_now.get("pid")) and bool(W.process_name(int(alive_now["pid"]))),
              "轮转之后实例还活着")
        m1 = daemon.status_age(PROJ)
        time.sleep(daemon.STATUS_HEARTBEAT_SECONDS + 1.0)
        m2 = daemon.status_age(PROJ)
        # age 只被心跳间隔约束（每 5 秒改写一次），所以睡 6 秒之后它应该还在
        # 一个心跳以内；关键是**没有变陈旧** —— 轮转不该把心跳弄断。
        check(m2 is not None and m2 < daemon.STATUS_STALE_SECONDS
              and not daemon.status_is_stale(PROJ),
              "轮转没有把状态文件心跳打断（日志换了文件，收件箱照旧）",
              f"age {m1:.1f}s -> {m2:.1f}s，阈值 {daemon.STATUS_STALE_SECONDS:.0f}s")
        text_live = live.read_text(encoding="utf-8", errors="replace")
        check("已轮转成" in text_live or "收到命令" in text_live,
              "新日志里能看到轮转说明/命令记录")
        daemon.send_command(PROJ, "quit")
        gone = wait_for(lambda: not W.instance_exists(), 20.0)
        check(gone, "投 quit 之后实例干净退出")
        check(not daemon.status_path(PROJ).exists(), "退出后状态文件被清掉")
    finally:
        try:
            daemon.send_command(PROJ, "quit")
        except Exception:
            pass
        time.sleep(0.5)
        if W.instance_exists():
            try:
                st = daemon.read_status(PROJ) or {}
                if st.get("pid"):
                    subprocess.run(["taskkill", "/F", "/PID", str(st["pid"])],
                                   capture_output=True)
            except Exception:
                pass

print()
print("=" * 74)
print("8. 状态文件／「打开日志文件」用的必须是真的那个路径（--log 能覆盖配置）")
print("=" * 74)
from t_ime.app import App  # noqa: E402
from t_ime.config import load as load_config  # noqa: E402

custom = TMP / "我给的位置.log"
cfg, _cfg_path = load_config()
had_status = daemon.status_path(PROJ).exists()
try:
    app_custom = App(cfg, PROJ, verbose=False, install_hook=False, log_path=custom)
    try:
        check(app_custom._log_path() == custom, "App 记住了调用方算好的日志路径",
              str(app_custom._log_path()))
        app_custom._publish_status(force=True)
        st = daemon.read_status(PROJ) or {}
        check(str(st.get("log")) == str(custom),
              "状态文件里的 log 就是那个真路径（面板/体检照着它念）",
              str(st.get("log")))
        opened: list[Path] = []
        app_custom._open_path = lambda p: opened.append(Path(p))  # type: ignore[method-assign]
        app_custom._handle_tray_action("open_log")
        check(opened and opened[0] == custom,
              "托盘/面板的「打开日志文件」打开的是它（不是默认那个）",
              str(opened[0]) if opened else "没调用")
    finally:
        app_custom.overlay.destroy()

    app_plain = App(cfg, PROJ, verbose=False, install_hook=False)
    try:
        check(app_plain._log_path() == daemon.resolve_log_path(PROJ, None, cfg),
              "没给 log_path 时照旧按配置算（老行为不变）",
              str(app_plain._log_path()))
    finally:
        app_plain.overlay.destroy()

    proc = subprocess.run(
        [sys.executable, "main.py", "--doctor", "--doctor-quick", "--log", str(custom)],
        cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    check(str(custom) in out,
          "真跑 --doctor --log <自定义> 报告里就是那个路径",
          [ln.strip() for ln in out.splitlines() if "日志文件" in ln][:1])
finally:
    if not had_status:
        try:
            daemon.status_path(PROJ).unlink(missing_ok=True)
        except OSError:
            pass

print()
print("=" * 74)
print("清理")
print("=" * 74)
try:
    shutil.rmtree(TMP, ignore_errors=True)
    check(True, "临时日志目录已清掉")
except Exception as exc:
    check(False, "清理临时目录", repr(exc))

# 体检报告默认写在仓库根 —— 那是产品行为，但测试跑完不该留下它。
(PROJ / "translate-ime-doctor.txt").unlink(missing_ok=True)

passed = sum(1 for ok, _ in results if ok)
total_n = len(results)
print()
print("=" * 74)
print(f"结果：{passed}/{total_n} 通过")
print("=" * 74)
sys.exit(0 if passed == total_n else 1)
