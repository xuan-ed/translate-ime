# -*- coding: utf-8 -*-
"""主循环防线：``after`` 回调里的异常**绝不能**让心跳停掉。

这条是「打字没反应」最阴的一种死法：Tk 的 ``after`` 回调抛异常时只往 stderr
打一行 traceback，**不会**再排下一次。而重排程的
``self.overlay.after(TICK_MS, self._tick)`` 早先正好写在函数末尾 —— 于是
主循环静默死亡：钩子还装着、按键还被吞掉、候选窗永远不再出现、上屏永远不会
发生，用户那边就是「打字没反应」，日志里只有一行不起眼的 traceback。

这个测试不跑 Tk 主循环，而是手动调 ``_tick()``，这样能精确地数「排了几次
心跳」：

* 异常一次 → 计数 +1、错误文本记下来、给用户看的提醒出现、**心跳照排**
* 再异常一次 → 计数继续涨，但不重复刷提醒
* 收到退出请求 → 不再排心跳（别留一个转不停的定时器）
* ``_tick_once()`` 自己**不许**再排一次（否则心跳会指数翻倍）

直接跑：``python tests/test_tick_guard.py``
"""
import shutil
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime import daemon  # noqa: E402
from t_ime.app import App  # noqa: E402
from t_ime.config import load as load_config  # noqa: E402
from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

OK = "[OK]  "
BAD = "[FAIL]"
results: list[tuple[bool, str]] = []


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"  — {detail}" if detail else ""))
    return cond


def pending(root) -> int:
    """现在排队等着跑的几个 Tk 定时器。"""
    return len(root.tk.call("after", "info"))


print("=" * 74)
cfg, cfg_path = load_config()
print(f"配置来自 {cfg_path}")
print("=" * 74)

# 这个测试会写状态文件 / 读命令收件箱，别把用户正在用的那个实例的痕迹弄丢
status_path = daemon.status_path(PROJ)
ctl_path = daemon.ctl_path(PROJ)
backup: dict[Path, bytes] = {}
for p in (status_path, ctl_path):
    if p.exists():
        backup[p] = p.read_bytes()

app = App(cfg, PROJ, verbose=False, install_hook=False)
try:
    root = app.overlay.root
    app._running = True

    # ---- 1. 正常心跳：不报错，且确实排了下一次 ----
    n0 = pending(root)
    app._tick()
    check(app._tick_errors == 0, "正常 tick 不产生错误计数")
    check(pending(root) == n0 + 1, "正常 tick 之后正好排了一次下一次心跳",
          f"{n0} -> {pending(root)}")

    # ---- 2. `_tick_once` 自己不许再排心跳（双倍心跳回归）----
    n1 = pending(root)
    try:
        app._tick_once()
    except Exception as exc:  # 真实 tick 可能因为没窗口而抛，不影响这条断言
        print(f"       （_tick_once 抛了 {exc!r}，仍然只数定时器）")
    check(pending(root) == n1, "_tick_once() 自己不排心跳（重排程只在 _tick 的 finally 里）",
          f"{n1} -> {pending(root)}")

    # ---- 3. 真故障：异常必须被吃掉，循环必须活着 ----
    def boom():
        raise RuntimeError("测试用的假故障")

    app._tick_once = boom
    n2 = pending(root)
    warns0 = len(app._warnings)
    app._tick()  # 不许往外抛
    check(app._tick_errors == 1, "tick 抛异常后计数为 1")
    check(bool(app._last_tick_error) and "测试用的假故障" in (app._last_tick_error or ""),
          "把完整 traceback 记下来了")
    check(len(app._warnings) == warns0 + 1, "第一次故障会给用户一条提醒")
    check(any("主循环报错" in w for w in app._warnings),
          "提醒文案说的是主循环报错", repr(app._warnings[-1] if app._warnings else ""))
    check(pending(root) == n2 + 1,
          "异常之后仍然排了下一次心跳（旧代码在这里静默死掉）",
          f"{n2} -> {pending(root)}")

    # ---- 4. 再抛：计数累加，不重复刷提醒 ----
    warns1 = len(app._warnings)
    app._tick()
    check(app._tick_errors == 2, "第二次故障计数累加为 2")
    check(len(app._warnings) == warns1, "第二次故障不再重复提醒（不刷屏）")

    # ---- 5. 退出请求之后不再排心跳 ----
    app.core.quit_requested = True
    n3 = pending(root)
    app._tick()
    check(pending(root) == n3, "收到退出请求后不再排下一次心跳", f"{n3} -> {pending(root)}")
finally:
    try:
        app.shutdown()
    except Exception:
        pass
    for p, data in backup.items():
        p.write_bytes(data)
    # 同上：测试写出来的状态文件不能留在仓库根目录，否则用户下次启动时
    # 会看到一句「它可能已经卡死或被强杀了」的假警报。
    for p in (status_path, ctl_path):
        if p not in backup:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

print()
print("=" * 74)
passed = sum(1 for ok, _ in results if ok)
print(f"结果：{passed}/{len(results)} 通过")
for ok, label in results:
    if not ok:
        print(f"  未通过：{label}")
print("=" * 74)
sys.exit(0 if passed == len(results) else 1)
