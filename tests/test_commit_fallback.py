# -*- coding: utf-8 -*-
"""上屏失败必须**说得出口**，而且不该往自己窗口里打字。

以前 ``App._handle_event`` 的 EV_COMMIT 分支只在 ``verbose and ok`` 时打印一行：
上屏失败 → 候选窗照样 hide、缓冲照样 reset，用户打出来的字凭空消失，屏幕上
一个字都没有（常驻时连控制台都没有）。这个文件盯三件事：

  1. ``App._commit`` 的四条「送不到」路径都要给出人话原因，而且**不许调用**
     ``send_text``（UIPI 拦下的注入会假装成功 —— 第 13 轮实测）；
  2. 失败要弹到候选窗上、并且把这次打的字放进剪贴板兜底；
  3. ``winapi.same_window`` / ``set_foreground`` 的诚实回答（Tk 包装窗口
     会让裸比句柄得出「还原失败」的假结论）。

用法：``python tests/test_commit_fallback.py``（有实例在跑时跳过会打扰它的两节）
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime import daemon  # noqa: E402
from t_ime import winapi as W  # noqa: E402
from t_ime.app import App  # noqa: E402
from t_ime.config import load as load_config  # noqa: E402
from t_ime.core import EV_COMMIT  # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  —— {detail}" if detail else ""))
    return bool(ok)


def skip(label: str, why: str) -> None:
    results.append((True, label))
    print(f"  [SKIP] {label}  —— {why}")


def release_mutex() -> bool:
    held = W._MUTEX_HANDLES.pop("translate-ime", None)
    if held is None:
        return True
    try:
        W.kernel32.CloseHandle(held)
    except Exception:
        return False
    return True


cfg, _cfg_path = load_config()
busy = W.instance_exists("translate-ime")
status_path = daemon.status_path(PROJ)
ctl_path = daemon.ctl_path(PROJ)
backup = {p: p.read_bytes() for p in (status_path, ctl_path) if p.exists()}

app = App(cfg, PROJ, verbose=False, install_hook=False)
app._welcome_done = True          # 常驻半小时之后才会上屏失败，引导早播完了

toasts: list[tuple[str, float | None]] = []
clips: list[str] = []
toasts_real = app.core.show_toast
clips_real = app.overlay.set_clipboard
app.core.show_toast = lambda text, seconds=None: toasts.append((text, seconds))
app.overlay.set_clipboard = lambda text: (clips.append(text), True)[1]

# 被换掉的 winapi 函数（每节自己设，节末还原）
real_fg_hwnd = W.foreground_hwnd
real_window_pid = W.window_pid
real_process_name = W.process_name
real_send_text = W.send_text
real_inject_reason = W.inject_blocked_reason
real_restore = app.overlay.restore_foreground

TEXT = "你好世界"
FOREIGN = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"

try:
    # ------------------------------------------------------------------ #
    print("\n-- 1. same_window：两个句柄说的是不是同一个窗口 --")
    # ------------------------------------------------------------------ #
    check(W.same_window(1234, 1234) is True, "同一个句柄 → True")
    check(W.same_window(0, 1234) is False and W.same_window(1234, 0) is False,
          "有 0 参与就一定不是同一个窗口")
    real_root = W._root_hwnd
    roots = {11: 100, 12: 100, 21: 200}
    W._root_hwnd = lambda h: roots.get(int(h), int(h))
    try:
        check(W.same_window(11, 12) is True,
              "句柄不同、但顶层窗口是同一个 → True（Tk 包装窗口就是这种）")
        check(W.same_window(11, 21) is False, "顶层窗口不同 → False")
    finally:
        W._root_hwnd = real_root
    check(W.set_foreground(0) is False, "set_foreground(0) → False")
    check(W.set_foreground(W.foreground_hwnd()) is True,
          "把前台设成当前前台 → True（且什么都没做）")

    # ------------------------------------------------------------------ #
    print("\n-- 2. _commit：四条送不到的路都要说话，而且不许乱送 --")
    # ------------------------------------------------------------------ #
    sent: list[str] = []
    W.send_text = lambda text: (sent.append(text), True)[1]
    W.foreground_hwnd = lambda: 4242
    W.window_pid = lambda hwnd: 987654
    W.process_name = lambda pid: FOREIGN

    W.inject_blocked_reason = lambda pid: "拿不到那个窗口的权限信息（本程序是 Low（低），多半它比本程序权限高）"
    sent.clear()
    ok = app._commit(TEXT)
    check(ok is False, "权限不够的目标 → 拒绝")
    check(sent == [], "拒绝时**一次都没调用** send_text（UIPI 下它会假装成功）", f"sent={sent}")
    check("msedge.exe" in app._commit_reason and "权限" in app._commit_reason,
          "原因里带了是哪个程序、以及为什么", app._commit_reason)

    W.inject_blocked_reason = lambda pid: ""
    app._inject_cache.clear()   # 权限判断是按 pid 缓存的，换结论要先清缓存
    sent.clear()
    ok = app._commit(TEXT)
    check(ok is True and sent == [TEXT], "权限够的目标 → 照常送出去", f"sent={sent}")
    check(app._commit_reason == "", "成功时不留下失败原因")

    W.send_text = lambda text: False
    app._inject_cache.clear()
    app._commit(TEXT)
    check("SendInput" in app._commit_reason,
          "送出去但系统拒绝了 → 原因说 SendInput", app._commit_reason)

    # 前台被我们自己占了，而且还原不回去（那个窗口权限更高，抢不回来）
    sent.clear()
    W.send_text = lambda text: (sent.append(text), True)[1]
    W.window_pid = lambda hwnd: os.getpid()
    app.overlay.restore_foreground = lambda: False
    ok = app._commit(TEXT)
    check(ok is False and sent == [],
          "前台还在自己手里时也不送（否则字会掉进候选窗，用户看到「没反应」）",
          app._commit_reason)
    check("自己手里" in app._commit_reason, "并且说清楚是「没能还给你那个窗口」",
          app._commit_reason)

    # 压根没有前台窗口
    W.foreground_hwnd = lambda: 0
    app.overlay.restore_foreground = lambda: False
    sent.clear()
    ok = app._commit(TEXT)
    check(ok is False and sent == [] and "找不到" in app._commit_reason,
          "没有前台窗口时不乱送，也要说一声", app._commit_reason)

    check(app._commit("") is False, "空文字直接 False")

    calls = {"n": 0}
    W.foreground_hwnd = lambda: 4242
    W.window_pid = lambda hwnd: 987654
    W.send_text = lambda text: True

    def counting_reason(pid):
        calls["n"] += 1
        return ""

    W.inject_blocked_reason = counting_reason
    app._inject_cache.clear()
    app._commit(TEXT)
    app._commit(TEXT)
    check(calls["n"] == 1, "同一个目标的权限判断只做一次（上屏是热路径）", f"calls={calls['n']}")

    # ------------------------------------------------------------------ #
    print("\n-- 3. 失败要说出口：候选窗 + 剪贴板兜底 --")
    # ------------------------------------------------------------------ #
    app._commit_reason = "msedge.exe 不收模拟按键：拿不到那个窗口的权限信息"
    app._commit_failures = 0
    app._inject_warned = False
    toasts.clear()
    clips.clear()
    app._report_commit_failure(TEXT)
    check(app._commit_failures == 1, "失败次数记下来了")
    check(app._last_commit_error == app._commit_reason, "最近一次的失败原因也记下来了")
    check(len(toasts) == 1, "弹了一条提示（以前一个字都没有）",
          f"toasts={len(toasts)}")
    tip = toasts[0][0] if toasts else ""
    check(app._commit_reason in tip, "提示里写清楚了为什么送不到")
    check(TEXT in tip, "提示里把用户刚才打的字显示出来了")
    check(clips == [TEXT], "同时把字放进了剪贴板兜底", f"clips={clips}")
    check("Ctrl+V" in tip, "并且告诉用户怎么救回来")
    check((toasts[0][1] or 0) >= 5.0, "这种提示要停够久才看得见", f"seconds={toasts[0][1]}")
    check("管理员" in tip, "第一次失败会提一句「以管理员身份运行」")

    toasts.clear()
    app._report_commit_failure(TEXT)
    check(len(toasts) == 1 and "管理员" not in toasts[0][0],
          "第二次失败不再重复那句建议（免得每次打字都念一遍）")

    app.overlay.set_clipboard = lambda text: False
    app._commit_reason = "前台窗口还在本程序自己手里，没能还给你正在用的那个窗口"
    toasts.clear()
    app._report_commit_failure(TEXT)
    check("剪贴板也没写进去" in toasts[0][0],
          "剪贴板都写不进去时也照样说，并直接告诉用户刚才打的是什么")
    app.overlay.set_clipboard = lambda text: (clips.append(text), True)[1]

    toasts.clear()
    app._warnings.clear()
    app._welcome_done = False
    app._report_commit_failure(TEXT)
    check(not toasts and any("没能打进" in w for w in app._warnings),
          "引导还没播完就先排队（由引导的尾巴带出来），不会盖住引导",
          f"warnings={len(app._warnings)} toasts={len(toasts)}")
    app._welcome_done = True

    toasts.clear()
    app._handle_event({"type": EV_COMMIT, "text": TEXT, "ok": True, "note": ""})
    check(not toasts, "成功的事件不会弹任何提示")

    before = app._commit_failures
    app._handle_event({"type": EV_COMMIT, "text": TEXT, "ok": False, "note": ""})
    check(app._commit_failures == before + 1 and toasts,
          "_handle_event 收到 ok=False 就会报告（这就是以前一声不吭的那条路）")

    # 状态文件里要能看见（不然控制面板和 --doctor 都不知道发生过）
    written: list[dict] = []
    real_write = daemon.write_status
    daemon.write_status = lambda root_dir, data: (written.append(dict(data)), True)[1]
    try:
        app._status_sig = None
        app._publish_status(force=True)
        sig1 = app._status_sig
        app._commit_failures += 1
        app._last_write = time.monotonic() - (daemon.STATUS_HEARTBEAT_SECONDS + 0.1)
        app._publish_status()
        sig2 = app._status_sig
        last = written[-1] if written else {}
        check(sig1 != sig2, "上屏失败次数变了 → 状态立刻再写一次（不等心跳）")
        check(int(last.get("commit_failures") or 0) == app._commit_failures,
              "状态文件里带着累计失败次数", f"commit_failures={last.get('commit_failures')}")
        check(last.get("last_commit_error") == app._last_commit_error,
              "也带着最近一次的失败原因")
    finally:
        daemon.write_status = real_write

    # ------------------------------------------------------------------ #
    print("\n-- 4. 控制面板与 --doctor 会把这件事报出来 --")
    # ------------------------------------------------------------------ #
    import subprocess  # noqa: E402

    fake = {"pid": os.getpid(), "root": str(PROJ), "started": time.time(),
            "enabled": True, "direct": False, "tray": False, "hook": True,
            "hook_info": {"installed": True, "alive": True, "error": None, "total": 0,
                          "real": 0, "injected": 0, "self_injected": 0,
                          "foreign_injected": 0, "effective": 0, "swallowed": 0,
                          "probes": 0, "probe_seen": 0, "probe_failures": 0,
                          "probe_send_failures": 0, "probe_error": None,
                          "last_real_age": None, "last_effective_age": None},
            "overlay": {"shows": 1, "visible": False, "on_monitor": True,
                        "rect": [0, 0, 10, 10], "error": None},
            "tick_errors": 0, "hook_reinstalls": 0, "hook_misses": 0,
            "hook_reconciles": 0, "hook_giveup": False,
            "commit_failures": 2, "last_commit_error": "msedge.exe 不收模拟按键：权限不够"}
    if busy:
        skip("4. 面板与体检的文案", "已经有实例在跑，不抢它的状态文件")
    else:
        daemon.write_status(PROJ, dict(fake))
        held = W.single_instance("translate-ime")
        check(bool(held) or W.instance_exists(), "本进程拿到单实例锁（面板分支才会走）")
        proc = subprocess.run([sys.executable, "main.py"], cwd=str(PROJ),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=90)
        text = (proc.stdout or "") + (proc.stderr or "")
        panel_line = [ln.strip() for ln in text.splitlines() if "上屏失败" in ln]
        check("已经在跑了" in text, "第二次启动走了控制面板那条路")
        check(bool(panel_line) and "2 次" in panel_line[0] and "msedge.exe" in panel_line[0],
              "面板上写着「上屏失败 累计 2 次（最近一次：…）」",
              panel_line[0] if panel_line else "（面板里没有这一行）")
        out = subprocess.run([sys.executable, "main.py", "--doctor", "--doctor-quick"],
                             cwd=str(PROJ), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=180)
        doc = (out.stdout or "") + (out.stderr or "")
        doc_line = [ln.strip() for ln in doc.splitlines() if "上屏失败" in ln]
        check(bool(doc_line) and "2 次" in doc_line[0],
              "--doctor 也会把这件事列出来", doc_line[0] if doc_line else "（体检里没有这一行）")
        check("剪贴板" in doc or "抓回来" in doc or "Ctrl+V" in doc,
              "并且告诉用户那一刻字已经放进剪贴板了")
        check(release_mutex(), "交还单实例锁")

    # ------------------------------------------------------------------ #
    print("\n-- 5. 真机：权限判断探针本身是对的 --")
    # ------------------------------------------------------------------ #
    check(W.integrity_level_of(os.getpid()) == W.integrity_level(),
          "读自己的令牌 = 自己的完整性级别",
          f"{W.integrity_level_of(os.getpid())}")
    check(W.integrity_level_of(999_999_999) is None,
          "读一个不存在的进程 → None（不抛异常）")
    check(W.inject_blocked_reason(os.getpid()) == "",
          "往自己进程里送按键永远是允许的（这条是探针的自检）")
    check(W.can_inject_into(os.getpid()) is True, "can_inject_into 与原因互为反面")
    fg = W.foreground_hwnd()
    if fg:
        pid = W.window_pid(fg)
        reason = W.inject_blocked_reason(pid)
        print(f"    此刻前台：{W.process_name(pid) or '?'} pid={pid} → "
              f"{reason or '可以送'}")
        check(reason == "" or "权限" in reason,
              "对真前台的判断要么放行、要么给出权限原因",
              reason or "放行")
    else:
        skip("5. 真前台的判断", "此刻没有前台窗口")
finally:
    W.foreground_hwnd = real_fg_hwnd
    W.window_pid = real_window_pid
    W.process_name = real_process_name
    W.send_text = real_send_text
    W.inject_blocked_reason = real_inject_reason
    app.overlay.restore_foreground = real_restore
    app.core.show_toast = toasts_real
    app.overlay.set_clipboard = clips_real
    for p, data in backup.items():
        try:
            p.write_bytes(data)
        except OSError:
            pass
    # 自己造出来的状态文件要收走：留着它，用户下次双击 run.bat 会看到
    # 「状态文件超过 10 秒没更新，那个实例可能已经卡死或被强杀了」的假警报。
    # （顺带：它会污染 test_control 里那条「刚写的状态不算旧」的断言。）
    for p in (status_path, ctl_path):
        if p not in backup:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
    daemon.discard_commands(PROJ)
    release_mutex()
    try:
        app.overlay.destroy()
    except Exception:
        pass

print()
print("=" * 74)
# 体检报告默认写在仓库根 —— 那是产品行为，但测试跑完不该留下它。
(PROJ / "translate-ime-doctor.txt").unlink(missing_ok=True)

passed = sum(1 for ok, _ in results if ok)
print(f"结果：{passed}/{len(results)} 通过")
for ok, label in results:
    if not ok:
        print(f"  未通过：{label}")
print("=" * 74)
sys.exit(0 if passed == len(results) else 1)
