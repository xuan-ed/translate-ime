# -*- coding: utf-8 -*-
"""``--demo``：README 开头就写着的那条「不装钩子、只看界面」的路。

README 第二行：

    python main.py --demo          # 只弹候选窗，不装钩子，用来验收界面

这个入口是给「不想装钩子、只想先看看长什么样」的人用的，也是文档截图
（``tests/demo_frames.py``）背后的东西。可 ``grep`` 遍 ``tests/`` 里 ``--demo``：
除了 ``test_diagnostics.py`` 里那句「``--demo-seconds`` 单独出现会被忽略」的旁证，
**没有任何一项真的跑过它**。它要是坏了（比如 ``Overlay`` 改了构造函数、主循环改成
按需渲染），用户照着 README 敲出来的就是一闪而过或干脆不动。

本测试钉住四件事：

  1. ``demo_snapshot()`` 的形状 —— 界面验收看到的到底是什么内容；
  2. 这个假状态能真的渲染出来（候选窗真的出现、真的在屏幕内）；
  3. **它不装钩子**（把 ``W.KeyboardHook`` 换成「一构造就炸」的版本，跑完不炸）、
     **不占实例互斥体**、**不写状态文件**；
  4. ``--demo`` 子进程会自己退出（``--demo-seconds`` 真的生效），退出码 0、没有
     traceback，默认还是 30 秒。

直接跑：``python tests/test_demo_mode.py``
"""
import inspect
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
sys.path.insert(0, str(PROJ))

from t_ime import daemon  # noqa: E402
from t_ime.app import demo_snapshot, run_demo  # noqa: E402
from t_ime.config import load as load_config  # noqa: E402
from t_ime.overlay import Overlay  # noqa: E402
from t_ime.console import fix_console_encoding  # noqa: E402
import main as M  # noqa: E402

fix_console_encoding()

OK = "[OK]  "
BAD = "[FAIL]"
results: list[tuple[bool, str]] = []


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"  — {detail}" if detail else ""))


print("=" * 74)
print("  --demo：只弹候选窗、不装钩子，用来验收界面")
print("=" * 74)

cfg, cfg_path = load_config()
print(f"配置来自 {cfg_path}")

STATUS = daemon.status_path(PROJ)
BOOSTS = PROJ / "user_boosts.json"
status_before = STATUS.exists()
boosts_before = BOOSTS.exists()

try:
    # ---------------------------------------------------------------- #
    print("\n== 1. demo_snapshot() 里到底是什么 ==")
    st = demo_snapshot()
    check(st["enabled"] is True, "演示状态是「已启用」（不然窗口会显示挂起）",
          st["enabled"])
    check(st["buffer"] == "jintiantianqizenmeyang",
          "缓冲里是一句能真打出来的拼音", st["buffer"])
    check(all(ch.isalpha() and ch.islower() for ch in st["buffer"]),
          "缓冲里只有小写字母（引擎认这个形态）", st["buffer"])
    check(len(st["zh"]) == 4, "中文候选 4 条", st["zh"])
    check(st["zh"][0] == "今天天气怎么样", "第一条候选是那句完整的话", st["zh"][:1])
    check(3 <= len(st["en"]) <= 4, "英文候选 3~4 条（译文行不空）", st["en"])
    check(st["active"] == 0, "默认高亮中文行", st["active"])
    check(st["sel"] == [0, 0], "两行各自的选择都是 0", st["sel"])
    check(st["tr_state"] == "done", "翻译状态是 done（不是 pending，不显示转圈）",
          st["tr_state"])
    check(st["tr_text"] == st["zh"][0], "右上角写的原文就是选中的那条",
          st["tr_text"])
    check(st["mock"] is True, "标了 mock（告诉使用者这是假数据）", st["mock"])
    check(st["hotkeys"] == {}, "演示不依赖真实热键（配置里可以不配）", st["hotkeys"])

    # ---------------------------------------------------------------- #
    print("\n== 2. 这个假状态能真的渲染出来 ==")
    ov = Overlay(cfg, verbose=False)
    try:
        ov.render(st)
        check(ov.visible is True, "render 之后窗口 visible", ov.visible)
        geo = ov.root.geometry()
        check(bool(geo), "拿到了窗口几何信息", geo)
        size = geo.split("+")[0]
        w, h = (int(x) for x in size.split("x")[:2])
        check(w > 0 and h > 0, "窗口有真实像素尺寸（不是 1x1 空窗）", (w, h))
        x, y = (int(v) for v in geo.split("+")[1:3])
        check(-4000 < x < 4000 and -4000 < y < 4000, "窗口落在合理的屏幕坐标上",
              (x, y))
        # 再 render 一次同一份快照：走的是「内容没变就不重画」那条路，不许抛
        took = {"err": None}
        try:
            ov.render(st)
        except Exception as exc:  # noqa: BLE001
            took["err"] = exc
        check(took["err"] is None, "同一份快照重画一次不抛异常", took["err"])
        # 内容变了（英文行空掉、高亮切到英文行）也不许抛
        st2 = dict(st, en=[], active=1, tr_state="failed", tr_text="")
        took["err"] = None
        try:
            ov.render(st2)
        except Exception as exc:  # noqa: BLE001
            took["err"] = exc
        check(took["err"] is None, "英文行空掉、状态改 failed 也不抛", took["err"])
    finally:
        ov.destroy()
    # destroy 之后还能再建一个 Tk 根，说明上一个真的被销毁了
    check((lambda: (Overlay(cfg, verbose=False).destroy(), True)[1])(),
          "destroy 之后能干净地再建一个候选窗")

    # ---------------------------------------------------------------- #
    print("\n== 3. run_demo() 真的「不装钩子」、不占实例、不写状态 ==")
    from t_ime import app as app_mod

    real_hook = app_mod.W.KeyboardHook
    hook_calls = []

    class Boom:
        def __init__(self, *a, **kw):
            hook_calls.append((a, kw))
            raise AssertionError("--demo 不应该装键盘钩子")

    app_mod.W.KeyboardHook = Boom
    try:
        t0 = time.monotonic()
        rc = run_demo(cfg, PROJ, seconds=0.4)
        cost = time.monotonic() - t0
    finally:
        app_mod.W.KeyboardHook = real_hook

    check(rc == 0, "run_demo 返回 0", rc)
    check(hook_calls == [], "全程没有构造过 KeyboardHook（README 的承诺）", hook_calls)
    check(0.3 <= cost <= 8.0, "0.4 秒的演示真的在 0.4 秒左右关掉", round(cost, 2))
    check(daemon.is_running() is False, "演示没有占住实例互斥体（真实例还能起）",
          daemon.is_running())
    check(STATUS.exists() == status_before, "演示没有写状态文件", str(STATUS))
    check(BOOSTS.exists() == boosts_before, "演示没有写 user_boosts.json", str(BOOSTS))

    # ---------------------------------------------------------------- #
    print("\n== 4. `main.py --demo --demo-seconds 1` 自己会退出 ==")
    t0 = time.monotonic()
    r = subprocess.run(
        [sys.executable, str(PROJ / "main.py"), "--demo", "--demo-seconds", "1"],
        cwd=str(PROJ), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    cost = time.monotonic() - t0
    out = (r.stdout or "") + (r.stderr or "")
    check(r.returncode == 0, "退出码 0", r.returncode)
    check(cost < 30, "--demo-seconds 1 之后自己关掉（没有挂着不退）", round(cost, 2))
    check("Traceback" not in out, "输出里没有 traceback",
          out[-300:] if "Traceback" in out else "")
    check(not STATUS.exists() or status_before, "--demo 跑完不该留下状态文件",
          str(STATUS))
    check(daemon.is_running() is False, "跑完没有残留实例互斥体",
          daemon.is_running())

    # ---------------------------------------------------------------- #
    print("\n== 5. 参数与默认值 ==")
    ap = M.build_parser()
    a = ap.parse_args(["--demo"])
    check(a.demo is True and a.demo_seconds == 30.0,
          "只给 --demo 时默认演示 30 秒", (a.demo, a.demo_seconds))
    a = ap.parse_args(["--demo", "--demo-seconds", "2"])
    check(a.demo is True and a.demo_seconds == 2.0,
          "--demo --demo-seconds 2 解析成 2.0", (a.demo, a.demo_seconds))
    a = ap.parse_args([])
    check(a.demo is False, "不给参数时不是演示模式", a.demo)
    sig = inspect.signature(run_demo)
    check(sig.parameters["seconds"].default == 30.0,
          "run_demo(seconds=) 默认 30.0（跟 argparse 的默认值一致）",
          sig.parameters["seconds"].default)

    print()
    passed = sum(1 for ok, _ in results if ok)
    print(f"结果：{passed}/{len(results)} 通过")
    for ok, label in results:
        if not ok:
            print(f"  未通过：{label}")
    sys.exit(0 if passed == len(results) else 1)
finally:
    # 兜底：演示进程/窗口异常退出时，别把状态文件与互斥体留脏
    try:
        if not status_before:
            STATUS.unlink(missing_ok=True)
        if not boosts_before:
            BOOSTS.unlink(missing_ok=True)
    except OSError:
        pass
