# -*- coding: utf-8 -*-
"""config.json 里某一项的类型写错时，程序会怎样？

用户手改 ``config.json`` 时最常见的两种毁法：

1. 数字写成字符串：``"variants": "four"``（手滑、或者照抄了别处的模板）——
   ``App`` 构造时 ``int("four")`` 抛 ``ValueError``，于是**连 ``--check``
   都跟着崩**，屏幕上只剩一段 traceback，看不出是哪一行配置闯的祸；
2. 数字写成 0 或负数：``"max_candidates": 0`` —— 不崩，但候选窗一个候选都
   不画，用户看到的是「输入法坏了」。

这个测试验证这轮加进 ``t_ime/config.py`` 的 ``sanitize()``：写错就退回默认值、
并且用一句人话说清楚「哪一项、写的是什么、改成了什么」，程序照常启动。

直接跑：``python tests/test_config_types.py``（约 40 秒，含一次真机 ``--check``）
"""
import contextlib
import copy
import atexit
import io
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from t_ime import config as C  # noqa: E402
from t_ime import daemon as D  # noqa: E402
from t_ime import winapi as W  # noqa: E402

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


def run(argv, cwd, timeout=240.0):
    import os

    env = dict(os.environ)
    env.update(ENV)
    return subprocess.run(
        argv, cwd=str(cwd), capture_output=True, timeout=timeout,
        text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, env=env,
    )


def capture(fn):
    """跑 fn()，把 stdout 收起来，返回 (fn 的返回值, 打印出来的文本)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ret = fn()
    return ret, buf.getvalue()


def fresh():
    """一份干净的默认配置副本，并且清掉上一次 load 留下的问题清单。"""
    C.LAST_PROBLEMS.clear()
    return copy.deepcopy(C.DEFAULTS)


def san(cfg):
    return C.sanitize(cfg)


def probe(cfg):
    """改一项后跑 sanitize，返回 (问题清单, 唯一的消息或 "")。"""
    probs = san(cfg)
    return probs, (probs[0] if probs else "")


def make_app_copy(base):
    app = base / "app"
    app.mkdir()
    shutil.copytree(ROOT / "t_ime", app / "t_ime",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("main.py", "run.bat", "requirements.txt",
                 "config.example.json"):
        shutil.copy2(ROOT / name, app / name)
    return app


def _new_base() -> Path:
    """给这一轮测试建一个临时目录，并保证收工时删得掉。

    以前这里用 ``tempfile.mkdtemp(prefix="config 类型 ")``：在本机上
    ``gettempdir()`` 会退回**当前工作目录**（系统 TEMP 在这个沙箱里不可写），
    于是每跑一次回归就在仓库根目录留下一个 ``config 类型 xxxxxxxx/`` ——
    ``shutil.rmtree(..., ignore_errors=True)`` 撞上还没退干净的实例文件时
    还会静默失败。现在固定建在 ``.tmp/`` 下，并且用一个注册了 atexit 的
    重试清理兜底（实例退出释放文件需要一点时间）。（第 61 轮顺手修的测试卫生问题）
    """
    parent = ROOT / ".tmp"
    parent.mkdir(parents=True, exist_ok=True)
    base = parent / f"config-types-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    _BASES.append(base)
    return base


_BASES: list[Path] = []


def _cleanup_bases() -> None:
    for base in _BASES:
        for _ in range(25):
            shutil.rmtree(base, ignore_errors=True)
            if not base.exists():
                break
            time.sleep(0.2)


atexit.register(_cleanup_bases)


def main():
    base = _new_base()
    try:
        body(base)
    finally:
        _cleanup_bases()

    print()
    print(f"通过 {PASS} / 失败 {FAIL} / 跳过 {SKIP}")
    if FAIL:
        print("有失败项 ✗")
        return 1
    print("全部通过 ✅")
    return 0


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


def body(base):
    # ------------------------------------------------------------------
    print("== 1. 干净的默认配置：一项都不该被改 ==")
    cfg = fresh()
    probs = san(cfg)
    check("sanitize(DEFAULTS) 是空清单", probs == [], probs)
    check("DEFAULTS 本身没被改坏",
          json.dumps(cfg, sort_keys=True, ensure_ascii=False)
          == json.dumps(C.DEFAULTS, sort_keys=True, ensure_ascii=False))

    ret, text = capture(lambda: C.load(base / "没有这个文件.json"))
    check("没有配置文件时不打任何 [config] 警告", "[config]" not in text, text[:300])
    check("LAST_PROBLEMS 保持为空", C.LAST_PROBLEMS == [], C.LAST_PROBLEMS)

    # ------------------------------------------------------------------
    print("== 2. 数字类型写错：拦住、退回默认值、点名是哪一项 ==")
    cfg = fresh()
    cfg["translate"]["variants"] = "four"
    probs, msg = probe(cfg)
    check("variants=\"four\" 只报这一项", len(probs) == 1, probs)
    check("消息里有点名 translate.variants", "translate.variants" in msg, msg)
    check("消息里带用户写的原值 \"four\"", '"four"' in msg, msg)
    check("消息里带改用的默认值 4", "用默认值 4" in msg, msg)
    check("变体数退回 4", cfg["translate"]["variants"] == 4,
          cfg["translate"]["variants"])

    cfg = fresh()
    cfg["pinyin"]["max_candidates"] = 0
    probs, msg = probe(cfg)
    check("max_candidates=0 报「超出范围」", "超出范围" in msg, msg)
    check("候选数退回 7", cfg["pinyin"]["max_candidates"] == 7,
          cfg["pinyin"]["max_candidates"])

    cfg = fresh()
    cfg["pinyin"]["max_candidates"] = True      # JSON 里写 true，bool 冒充整数
    probs, msg = probe(cfg)
    check("True 不当作整数 1", "应该是整数" in msg, msg)
    check("候选数退回 7（True 版）", cfg["pinyin"]["max_candidates"] == 7,
          cfg["pinyin"]["max_candidates"])

    cfg = fresh()
    cfg["translate"]["variants"] = 4.0          # JSON 里写 4.0
    probs = san(cfg)
    check("4.0 算整数、放行", probs == [], probs)
    check("4.0 归一成 int",
          type(cfg["translate"]["variants"]) is int
          and cfg["translate"]["variants"] == 4,
          cfg["translate"]["variants"])

    cfg = fresh()
    cfg["translate"]["variants"] = 3.5
    probs, msg = probe(cfg)
    check("3.5 不是整数、拦住", "应该是整数" in msg, msg)
    check("变体数退回 4（3.5 版）", cfg["translate"]["variants"] == 4)

    cfg = fresh()
    cfg["overlay"]["font_size"] = -3
    probs, msg = probe(cfg)
    check("负数被拦住", "font_size" in msg, msg)
    check("字号退回 16", cfg["overlay"]["font_size"] == 16)

    cfg = fresh()
    cfg["overlay"]["alpha"] = "light"
    probs, msg = probe(cfg)
    check("alpha 写成字符串被拦住", "应该是数字" in msg, msg)
    check("alpha 退回默认值",
          cfg["overlay"]["alpha"] == C.DEFAULTS["overlay"]["alpha"])

    cfg = fresh()
    cfg["output"] = 5                            # 整段配置写成数字
    probs, msg = probe(cfg)
    check("output 段写成数字时整段恢复",
          "output 应该是一整段配置" in msg, probs)
    check("output 段回到默认 dict", cfg["output"] == C.DEFAULTS["output"])

    # ------------------------------------------------------------------
    print("== 3. 边界值：范围两端放行，越界才拦 ==")
    cfg = fresh()
    cfg["pinyin"]["max_candidates"] = 1
    check("候选数下限 1 放行", san(cfg) == [] and cfg["pinyin"]["max_candidates"] == 1)

    cfg = fresh()
    cfg["translate"]["variants"] = 8
    check("变体数上限 8 放行", san(cfg) == [] and cfg["translate"]["variants"] == 8)

    cfg = fresh()
    cfg["translate"]["variants"] = 9
    probs, msg = probe(cfg)
    check("变体数 9 越界拦住", "超出范围" in msg, msg)
    check("变体数 9 退回 4", cfg["translate"]["variants"] == 4)

    cfg = fresh()
    cfg["overlay"]["alpha"] = 1                  # 整数 1
    probs = san(cfg)
    check("alpha=1 放行", probs == [], probs)
    check("alpha=1 归一成 float",
          isinstance(cfg["overlay"]["alpha"], float)
          and cfg["overlay"]["alpha"] == 1.0,
          cfg["overlay"]["alpha"])

    # ------------------------------------------------------------------
    print("== 4. 多处同时写错：逐条报、一次修好 ==")
    cfg = fresh()
    cfg["translate"]["enabled"] = "yes"
    cfg["pinyin"]["beam_width"] = 0
    cfg["overlay"]["font_size"] = "big"
    probs = san(cfg)
    text = " ".join(probs)
    check("三处错报三条", len(probs) == 3, probs)
    check("每条点名自己的键",
          "translate.enabled" in text and "pinyin.beam_width" in text
          and "overlay.font_size" in text, probs)
    check("修过之后再 sanitize 就是空的", C.sanitize(cfg) == [])

    # ------------------------------------------------------------------
    print("== 5. load()：打印一次人话警告，并记进 LAST_PROBLEMS ==")
    bad = base / "config.json"
    bad.write_text(json.dumps({
        "translate": {"variants": "four"},
        "pinyin": {"max_candidates": 0},
    }, ensure_ascii=False), encoding="utf-8")
    C.LAST_PROBLEMS.clear()
    ret, text = capture(lambda: C.load(bad))
    cfg, used = ret
    check("load 打印了配置警告",
          "[config]" in text and "里有 2 项写错" in text, text[:400])
    check("警告只打一遍（不是两遍）",
          text.count("里有 2 项写错") == 1, text)
    check("两条问题各一行", text.count("[config]   - ") == 2, text)
    check("LAST_PROBLEMS 记下两条", len(C.LAST_PROBLEMS) == 2,
          C.LAST_PROBLEMS)
    check("cfg 里已经按默认值修好",
          cfg["translate"]["variants"] == 4
          and cfg["pinyin"]["max_candidates"] == 7, cfg["translate"])

    ret2, text2 = capture(lambda: C.load(bad))
    check("同一次运行里再 load 一遍不重复刷屏", "[config]" not in text2,
          text2[:300])

    good = base / "good.json"
    good.write_text("{}", encoding="utf-8")
    ret3, text3 = capture(lambda: C.load(good))
    check("干净文件一声不吭", "[config]" not in text3, text3[:300])
    check("LAST_PROBLEMS 清空", C.LAST_PROBLEMS == [], C.LAST_PROBLEMS)

    # ------------------------------------------------------------------
    print("== 6. 真机：坏配置下 --check 不再崩、--show-config 显示默认值 ==")
    app = make_app_copy(base)
    (app / "config.json").write_text(json.dumps({
        "translate": {"variants": "four"},
        "pinyin": {"max_candidates": 0},
        # 拼错的键：不报错、也不生效，只在 --show-config 里露出来
        "overlay": {"font_szie": 20},
    }, ensure_ascii=False), encoding="utf-8")

    py = sys.executable
    r = run([py, str(app / "main.py"), "--check"], app)
    check("--check 发现写错的值时退出 1（不再崩、也不再假装全部通过）", r.returncode == 1,
          (r.stdout + r.stderr)[-600:])
    check("--check 打出配置警告并点名 translate.variants",
          "[config]" in r.stdout and "translate.variants" in r.stdout,
          r.stdout[:600])
    check("--check 也点名拼错的 overlay.font_szie",
          "不认识" in r.stdout and "font_szie" in r.stdout, r.stdout[:900])
    check("--check 最后说「有项目未通过」而不是「全部通过」",
          "有项目未通过" in r.stdout and "全部通过" not in r.stdout,
          r.stdout[-500:])
    check("全程没有 traceback",
          "Traceback" not in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-600:])

    r = run([py, str(app / "main.py"), "--show-config"], app, timeout=120)
    check("--show-config 发现拼错的键时退出码 1（这是它的约定）",
          r.returncode == 1, (r.stdout + r.stderr)[-400:])
    check("--show-config 里 variants 显示 4",
          re.search(r"\[文件\]\s+translate\.variants\s+= 4\b", r.stdout)
          is not None, r.stdout[:600])
    check("--show-config 里 max_candidates 显示 7",
          re.search(r"\[文件\]\s+pinyin\.max_candidates\s+= 7\b", r.stdout)
          is not None, r.stdout[:600])
    check("--show-config 把拼错的键单列出来",
          "font_szie" in r.stdout and "不认识" in r.stdout, r.stdout[-900:])

    # ------------------------------------------------------------------
    print("== 7. 后台常驻：配置警告必须落进日志（否则用户永远看不到） ==")
    if W.instance_exists():
        skip("后台日志里的配置警告", "已经有一个实例在跑，这一节跳过")
    else:
        r = run([py, str(app / "main.py"), "--background"], app, timeout=180)
        check("--background 启动器退出 0", r.returncode == 0,
              (r.stdout + r.stderr)[-400:])
        check("启动器说「已在后台启动」", "已在后台启动" in r.stdout, r.stdout[-300:])
        st = wait_status(app)
        check("后台实例起来了", bool(st and st.get("pid")), st)
        log_path = app / "translate-ime.log"
        text = ""
        for _ in range(50):
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""
            if "写错" in text and "不认识" in text:
                break
            time.sleep(0.2)
        check("日志里有「配置写错」的警告", "写错" in text, text[-600:])
        check("日志里点名了 translate.variants", "translate.variants" in text,
              text[-600:])
        check("日志里说清了用的是默认值 4", "用默认值 4" in text, text[-600:])
        check("日志里也点名拼错的 overlay.font_szie",
              "不认识" in text and "font_szie" in text, text[-900:])
        if st and st.get("pid"):
            D.send_command(app, "quit")
            check("quit 之后实例退出", wait_gone(app), D.read_status(app))

    # ------------------------------------------------------------------
    print("== 8. 拼错的键：启动时点名，但不影响生效值 ==")
    C.LAST_PROBLEMS.clear()
    C.LAST_UNKNOWN.clear()
    typo = base / "typo.json"
    typo.write_text(json.dumps({
        "overlay": {"font_szie": 20, "font_size": 18},
        "hotkey": {"compose": "ctrl+alt+space"},   # 少了个 s
        "_comment": "自己的备注",
        "hotkeys": {"quit_typ": "ctrl+alt+q"},     # 由 core._parse_hotkeys 点名
    }, ensure_ascii=False), encoding="utf-8")
    ret, text = capture(lambda: C.load(typo))
    cfg, _used = ret
    check("拼错的键会被点名", "有 2 个选项我不认识" in text, text)
    check("点名 overlay.font_szie", "overlay.font_szie" in text, text)
    check("点名整个段拼错的 hotkey.compose", "hotkey.compose" in text, text)
    check("拼错的键记进 LAST_UNKNOWN",
          C.LAST_UNKNOWN == ["hotkey.compose", "overlay.font_szie"], C.LAST_UNKNOWN)
    check("拼错的键不影响真正的 font_size（文件里写的是 18）",
          cfg["overlay"]["font_size"] == 18, cfg["overlay"])
    check("下划线开头的注释键不唠叨", "_comment" not in text, text)
    check("hotkeys.<动作名> 留给热键层点名（这里不重复报）",
          "hotkeys.quit_typ" not in C.LAST_UNKNOWN, C.LAST_UNKNOWN)

    ret2, text2 = capture(lambda: C.load(typo))
    check("同一次运行里再 load 一遍不重复刷屏", "[config]" not in text2, text2)

    scalar = base / "scalar.json"
    scalar.write_text(json.dumps({"output": 5}, ensure_ascii=False), encoding="utf-8")
    C.LAST_PROBLEMS.clear()
    C.LAST_UNKNOWN.clear()
    ret3, text3 = capture(lambda: C.load(scalar))
    check("整段写成标量只按类型问题报一次",
          text3.count("写错") == 1 and "不认识" not in text3, text3)
    check("那种情况 LAST_UNKNOWN 为空", C.LAST_UNKNOWN == [], C.LAST_UNKNOWN)

    clean = base / "clean.json"
    clean.write_text(json.dumps({"_notes": ["随便写"]}, ensure_ascii=False),
                     encoding="utf-8")
    C.LAST_PROBLEMS.clear()
    C.LAST_UNKNOWN.clear()
    ret4, text4 = capture(lambda: C.load(clean))
    check("只有注释键的干净配置一声不吭", "[config]" not in text4, text4)
    check("LAST_UNKNOWN 清空", C.LAST_UNKNOWN == [], C.LAST_UNKNOWN)

    # ------------------------------------------------------------------
    print("== 9. 整份配置没读成（JSON 坏掉 / 顶层不是对象）：照常起来，但要留话 ==")
    # 尾逗号是最常见的 JSON 语法错 —— 以前整份配置被静默忽略，后台模式日志里
    # 一个字都没有。
    C.LAST_LOAD_ERROR = ""
    broken = base / "broken.json"
    broken.write_text('{"overlay": {"font_size": 20,}}', encoding="utf-8")
    ret5, text5 = capture(lambda: C.load(broken))
    cfg5, _p5 = ret5
    check("JSON 写坏时 load 不抛异常", True)
    check("全部退回默认值（font_size = 16）", cfg5["overlay"]["font_size"] == 16,
          cfg5["overlay"])
    check("打印里说清「整份配置被忽略」", "整份配置被忽略" in text5, text5)
    check("打印里带着文件名", broken.name in text5, text5)
    check("LAST_LOAD_ERROR 记下了这件事",
          "读取" in C.LAST_LOAD_ERROR and broken.name in C.LAST_LOAD_ERROR,
          C.LAST_LOAD_ERROR)

    ret6, text6 = capture(lambda: C.load(broken))
    check("同一次运行里再 load 一遍不重复刷屏", "[config]" not in text6, text6)

    C.LAST_LOAD_ERROR = ""
    notobj = base / "not-object.json"
    notobj.write_text("[1, 2, 3]", encoding="utf-8")
    ret7, text7 = capture(lambda: C.load(notobj))
    cfg7, _p7 = ret7
    check("顶层是数组时不再 AttributeError 崩掉", cfg7["overlay"]["font_size"] == 16,
          cfg7["overlay"])
    check("说清顶层不是 JSON 对象", "顶层不是一个 JSON 对象" in text7, text7)
    check("点名拿到的类型是 list", "list" in C.LAST_LOAD_ERROR, C.LAST_LOAD_ERROR)

    C.LAST_LOAD_ERROR = ""
    strroot = base / "string-root.json"
    strroot.write_text('"hello"', encoding="utf-8")
    ret8, text8 = capture(lambda: C.load(strroot))
    check("顶层是字符串也照常起来",
          ret8[0]["overlay"]["font_size"] == 16, text8)
    check("点名拿到的类型是 str", "str" in C.LAST_LOAD_ERROR, C.LAST_LOAD_ERROR)

    C.LAST_LOAD_ERROR = ""
    nullroot = base / "null-root.json"
    nullroot.write_text("null", encoding="utf-8")
    ret9, _text9 = capture(lambda: C.load(nullroot))
    check("顶层是 null 也照常起来",
          ret9[0]["overlay"]["font_size"] == 16, C.LAST_LOAD_ERROR)
    check("顶层是 null 也留话", "NoneType" in C.LAST_LOAD_ERROR, C.LAST_LOAD_ERROR)

    C.LAST_LOAD_ERROR = ""
    C.LAST_PROBLEMS.clear()
    C.LAST_UNKNOWN.clear()
    ret10, text10 = capture(lambda: C.load(clean))
    check("之后再读一份干净配置，LAST_LOAD_ERROR 清空",
          C.LAST_LOAD_ERROR == "", C.LAST_LOAD_ERROR)
    check("干净配置仍然一声不吭", "[config]" not in text10, text10)

    C.LAST_LOAD_ERROR = ""
    missing = base / "does-not-exist.json"
    ret11, text11 = capture(lambda: C.load(missing))
    check("文件不存在不算「读失败」（首次运行不该报警）",
          ret11[0]["overlay"]["font_size"] == 16 and C.LAST_LOAD_ERROR == "",
          (C.LAST_LOAD_ERROR, text11))

    # 真机：副本里放一份坏 JSON，控制台上和后台日志里都得有那句话
    broken_cfg = '{"overlay": {"font_size": 20,}}'
    (app / "config.json").write_text(broken_cfg, encoding="utf-8")
    r = run([py, str(app / "main.py"), "--check"], app)
    check("坏 JSON 下 --check 退出 1 并说明原因（以前照样全绿）", r.returncode == 1,
          (r.stdout + r.stderr)[-600:])
    check("--check 的输出里有「整份配置被忽略」", "整份配置被忽略" in r.stdout,
          r.stdout[:800])
    check("--check 最后说「有项目未通过」", "有项目未通过" in r.stdout and "全部通过" not in r.stdout,
          r.stdout[-400:])

    if W.instance_exists():
        skip("后台日志里的整份配置警告", "已经有一个实例在跑，这一节跳过")
    else:
        r = run([py, str(app / "main.py"), "--background"], app, timeout=180)
        check("坏 JSON 下 --background 启动器退出 0", r.returncode == 0,
              (r.stdout + r.stderr)[-400:])
        st = wait_status(app)
        check("坏 JSON 下后台实例照样起来", bool(st and st.get("pid")), st)
        log_path = app / "translate-ime.log"
        text = ""
        for _ in range(50):
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""
            if "整份配置被忽略" in text:
                break
            time.sleep(0.2)
        check("日志里也有「整份配置被忽略」（后台模式 stdout 是 DEVNULL）",
              "整份配置被忽略" in text, text[-700:])
        check("日志里点名是哪个文件", "config.json" in text, text[-700:])
        if st and st.get("pid"):
            D.send_command(app, "quit")
            check("quit 之后实例退出", wait_gone(app), D.read_status(app))

    # ------------------------------------------------------------------
    print("== 10. 从模板里删掉的旧选项：改了也没用，得明说 ==")
    C.LAST_PROBLEMS.clear()
    C.LAST_UNKNOWN.clear()
    C.LAST_RETIRED.clear()
    check("DEFAULTS 的 input 段只剩真正在用的三项",
          set(C.DEFAULTS["input"]) == {"shift_tap_ms", "shift_tap_toggles", "toast_ms"},
          sorted(C.DEFAULTS["input"]))
    check("commit_delay_ms 不再出现在数值白名单里",
          ("input", "commit_delay_ms") not in C._INT_SPECS)
    check("config.example.json 也不再有这两个键",
          "commit_method" not in C.EXAMPLE_PATH.read_text(encoding="utf-8-sig"),
          "模板里还留着，用户还会照着抄")

    old = base / "old-template.json"
    old.write_text(json.dumps({
        "input": {
            "commit_method": "clipboard",   # 旧模板里的选项，代码从来没读过
            "commit_delay_ms": 50,          # 同上
            "toast_ms": 1600,
        }
    }, ensure_ascii=False), encoding="utf-8")
    ret_old, text_old = capture(lambda: C.load(old))
    cfg_old, _p_old = ret_old
    check("老配置里的旧选项会被点名", "从旧模板留下的选项" in text_old, text_old)
    check("点名 input.commit_method", "input.commit_method" in text_old, text_old)
    check("点名 input.commit_delay_ms", "input.commit_delay_ms" in text_old, text_old)
    check("说清从来没生效过", "从来没生效" in text_old, text_old)
    check("不把它污蔑成「拼写错」", "不认识" not in text_old, text_old)
    check("记进 LAST_RETIRED",
          C.LAST_RETIRED == ["input.commit_delay_ms", "input.commit_method"],
          C.LAST_RETIRED)
    check("不记进 LAST_UNKNOWN", C.LAST_UNKNOWN == [], C.LAST_UNKNOWN)
    check("生效配置里已经摘掉这个键", "commit_method" not in cfg_old["input"],
          cfg_old["input"])
    check("同一份文件里真正写了的项仍然生效",
          cfg_old["input"]["toast_ms"] == 1600, cfg_old["input"])
    check("DEFAULTS 没被这份老配置改坏",
          set(C.DEFAULTS["input"]) == {"shift_tap_ms", "shift_tap_toggles", "toast_ms"},
          sorted(C.DEFAULTS["input"]))

    _ret_old2, text_old2 = capture(lambda: C.load(old))
    check("同一次运行里再 load 一遍不重复刷屏", "[config]" not in text_old2, text_old2)

    clean_again = base / "clean-again.json"
    clean_again.write_text(json.dumps({"_notes": ["随便写"]}, ensure_ascii=False),
                           encoding="utf-8")
    C.LAST_RETIRED.clear()
    _ret_clean, text_clean = capture(lambda: C.load(clean_again))
    check("干净配置不提旧选项", "[config]" not in text_clean, text_clean)
    check("LAST_RETIRED 清空", C.LAST_RETIRED == [], C.LAST_RETIRED)

    # 真机：副本里放一份「照旧模板抄的」配置，控制台上和后台日志里都得有那句话
    (app / "config.json").write_text(json.dumps({
        "input": {"commit_method": "clipboard", "commit_delay_ms": 50},
        "translate": {"enabled": True},
    }, ensure_ascii=False), encoding="utf-8")
    r = run([py, str(app / "main.py"), "--check"], app)
    check("带旧选项的配置下 --check 仍然退出 0", r.returncode == 0,
          (r.stdout + r.stderr)[-600:])
    check("--check 的输出里点名旧选项", "input.commit_method" in r.stdout, r.stdout[:900])
    check("--check 仍然「全部通过」", "全部通过" in r.stdout, r.stdout[-400:])
    r = run([py, str(app / "main.py"), "--show-config"], app)
    check("--show-config 带着旧选项也不崩", r.returncode == 0,
          (r.stdout + r.stderr)[-500:])
    check("--show-config 不再把它当「不认识的键」", "不认识" not in r.stdout,
          r.stdout[-600:])

    if W.instance_exists():
        skip("后台日志里的旧选项警告", "已经有一个实例在跑，这一节跳过")
    else:
        r = run([py, str(app / "main.py"), "--background"], app, timeout=180)
        check("带旧选项时 --background 启动器退出 0", r.returncode == 0,
              (r.stdout + r.stderr)[-400:])
        st = wait_status(app)
        check("带旧选项时后台实例照样起来", bool(st and st.get("pid")), st)
        log_path = app / "translate-ime.log"
        text = ""
        for _ in range(50):
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""
            if "从旧模板留下的选项" in text:
                break
            time.sleep(0.2)
        check("日志里也有「从旧模板留下的选项」（后台模式 stdout 是 DEVNULL）",
              "从旧模板留下的选项" in text, text[-800:])
        check("日志里点名 commit_method", "commit_method" in text, text[-800:])
        if st and st.get("pid"):
            D.send_command(app, "quit")
            check("quit 之后实例退出", wait_gone(app), D.read_status(app))

    # ------------------------------------------------------------------
    print("== 11. 文字、颜色、枚举项写错：不该崩，也不该静默 ==")
    cfg = fresh()
    cfg["pinyin"]["dict_cache"] = 5           # 数字当路径 -> 以前拼路径 TypeError
    cfg["overlay"]["bg"] = 5                  # 数字当颜色 -> 以前 TclError
    cfg["overlay"]["accent_bg"] = "#12"       # 看着像十六进制，但少一位
    cfg["output"]["default_language"] = "jp"  # 以前静默按 zh 跑
    cfg["api"]["model"] = ["x"]               # 数组当模型名
    cfg["output"]["log_file"] = ""            # 空串路径
    cfg["overlay"]["font_family"] = None      # null 字体
    probs = san(cfg)
    check("七处写错全部报出来", len(probs) == 7, probs)
    check("点名 pinyin.dict_cache 应该是缓存文件路径",
          any("pinyin.dict_cache 应该是缓存文件路径" in p for p in probs), probs)
    check("点名 overlay.bg 应该是颜色",
          any("overlay.bg 应该是颜色" in p for p in probs), probs)
    check("点名 overlay.accent_bg 应该是颜色",
          any("overlay.accent_bg 应该是颜色" in p for p in probs), probs)
    check("点名 output.default_language 且列出 zh / en",
          any("output.default_language 只能是 zh / en 之一" in p for p in probs),
          probs)
    check("点名 api.model 应该是模型名",
          any("api.model 应该是模型名" in p for p in probs), probs)
    check("点名 output.log_file 应该是日志文件路径",
          any("output.log_file 应该是日志文件路径" in p for p in probs), probs)
    check("点名 overlay.font_family 应该是字体名",
          any("overlay.font_family 应该是字体名" in p for p in probs), probs)
    check("dict_cache 退回默认路径",
          cfg["pinyin"]["dict_cache"] == "pinyin_index.pkl", cfg["pinyin"]["dict_cache"])
    check("bg 退回默认颜色", cfg["overlay"]["bg"] == "#1b1c22", cfg["overlay"]["bg"])
    check("accent_bg 退回默认颜色",
          cfg["overlay"]["accent_bg"] == "#2f6fd0", cfg["overlay"]["accent_bg"])
    check("default_language 退回 zh",
          cfg["output"]["default_language"] == "zh", cfg["output"]["default_language"])
    check("model 退回默认模型",
          cfg["api"]["model"] == "deepseek-chat", cfg["api"]["model"])
    check("log_file 退回默认日志名",
          cfg["output"]["log_file"] == "translate-ime.log", cfg["output"]["log_file"])
    check("font_family 退回默认字体",
          cfg["overlay"]["font_family"] == C.DEFAULTS["overlay"]["font_family"],
          cfg["overlay"]["font_family"])
    check("DEFAULTS 没被这份坏配置改坏",
          C.DEFAULTS["pinyin"]["dict_cache"] == "pinyin_index.pkl"
          and C.DEFAULTS["overlay"]["bg"] == "#1b1c22",
          (C.DEFAULTS["pinyin"]["dict_cache"], C.DEFAULTS["overlay"]["bg"]))

    cfg = fresh()
    cfg["api"]["api_key"] = ""
    check("api_key 空串是合法的（没配 key = 用离线）", san(cfg) == [], cfg["api"]["api_key"])
    cfg = fresh()
    cfg["api"]["api_key"] = 123
    p_key = san(cfg)
    check("api_key 写成数字会被点名",
          len(p_key) == 1 and "api.api_key" in p_key[0] and "可以是空串" in p_key[0],
          p_key)
    check("api_key 退回空串", cfg["api"]["api_key"] == "", cfg["api"]["api_key"])

    cfg = fresh()
    cfg["output"]["default_language"] = "EN"
    check("default_language 大写 EN 合法并归一成小写",
          san(cfg) == [] and cfg["output"]["default_language"] == "en",
          cfg["output"]["default_language"])

    cfg = fresh()
    cfg["api"]["provider"] = "openai"
    p_prov = san(cfg)
    check("api.provider 写不认识的值会被点名并列出选项",
          len(p_prov) == 1 and "只能" in p_prov[0]
          and "deepseek" in p_prov[0] and "mock" in p_prov[0], p_prov)
    check("provider 退回默认 deepseek",
          cfg["api"]["provider"] == "deepseek", cfg["api"]["provider"])
    cfg = fresh()
    cfg["api"]["provider"] = "MOCK"
    check("provider 大写 MOCK 合法并归一成小写",
          san(cfg) == [] and cfg["api"]["provider"] == "mock", cfg["api"]["provider"])

    cfg = fresh()
    cfg["overlay"]["bg"] = "#FFF"
    check("#FFF 这种三位十六进制颜色合法", san(cfg) == [], cfg["overlay"]["bg"])
    cfg = fresh()
    cfg["overlay"]["bg"] = "black"
    check("英文颜色名 black 合法（Tk 自认的颜色名一律放行）", san(cfg) == [],
          cfg["overlay"]["bg"])

    # 真机：副本里放一份「路径/颜色/语言都写错」的配置，--check 不能崩
    (app / "config.json").write_text(json.dumps({
        "pinyin": {"dict_cache": 5},
        "overlay": {"bg": 5},
        "output": {"default_language": "jp"},
    }, ensure_ascii=False), encoding="utf-8")
    r = run([py, str(app / "main.py"), "--check"], app)
    check("文字/颜色写错时 --check 退出 1（不再崩，也不再假装全部通过）",
          r.returncode == 1, (r.stdout + r.stderr)[-700:])
    check("--check 全程没有 traceback",
          "Traceback" not in (r.stdout + r.stderr), (r.stdout + r.stderr)[-600:])
    check("--check 点名 pinyin.dict_cache", "pinyin.dict_cache" in r.stdout,
          r.stdout[:900])
    check("--check 最后说「有项目未通过」而不是「全部通过」",
          "有项目未通过" in r.stdout and "全部通过" not in r.stdout, r.stdout[-400:])
    r = run([py, str(app / "main.py"), "--show-config"], app, timeout=120)
    check("--show-config 带着坏值也不崩", r.returncode == 0,
          (r.stdout + r.stderr)[-500:])
    check("--show-config 显示的是修好后的默认路径",
          "pinyin_index.pkl" in r.stdout, r.stdout[:600])
    check("--show-config 不把写坏的项当「不认识的键」", "不认识" not in r.stdout,
          r.stdout[-600:])


    # 第 122 轮：整段写成标量（"hotkeys": 5）是**值**写错，不是拼错。
    # 以前 --show-config 把它列进「不认识的键（多半是拼错）」，还把退出码翻成 1
    # —— 同一次运行里 [config] 说「1 个选项我不认识」（只算真正的拼写错），
    # 它却说「2 个键 DEFAULTS 不认识」，退出码也和「坏值 rc 0」的约定相反。
    print("== 12. 整段类型写错，不许算成「拼错的键」（第 122 轮） ==")
    (app / "config.json").write_text(json.dumps({
        "hotkeys": 5,
        "output": 5,
    }, ensure_ascii=False), encoding="utf-8")
    r = run([py, str(app / "main.py"), "--show-config"], app, timeout=120)
    text = r.stdout + r.stderr
    check("两个整段写成数字时 --show-config 不崩", "Traceback" not in text, text[-500:])
    check("退出码按「坏值」的约定是 0（不是拼错的 1）", r.returncode == 0,
          (r.stdout + r.stderr)[-700:])
    check("不把它们列进「不认识的键」", "不认识" not in r.stdout, r.stdout[-700:])
    check("但要说明「另有 2 个键是整段类型写错」",
          "另有 2 个键是整段类型写错" in r.stdout, r.stdout[-700:])
    check("原因仍由 [config] 那几行负责（写错 + hotkeys 应该是一整段配置）",
          "写错" in text and "hotkeys 应该是一整段配置" in text, text[:900])
    check("没有拼错时仍然说「没有拼错的键 ✓」",
          "没有拼错的键" in r.stdout, r.stdout[-700:])

    # 真的拼错 + 整段写错同时存在：两处计数必须一致（不许一个说 1、一个说 2），
    # 而且只准点真正的拼写错。
    (app / "config.json").write_text(json.dumps({
        "output": 5,
        "overlay": {"font_szie": 18},
    }, ensure_ascii=False), encoding="utf-8")
    r = run([py, str(app / "main.py"), "--show-config"], app, timeout=120)
    text = r.stdout + r.stderr
    check("真拼错时退出码仍是 1", r.returncode == 1, (r.stdout + r.stderr)[-700:])
    check("只点真正的拼写错（font_szie），不点整段写错的 output",
          "font_szie" in r.stdout and "! output" not in r.stdout, r.stdout[-800:])
    check("两处计数一致：都说 1 个不认识的键",
          "有 1 个选项我不认识" in text and "有 1 个键 DEFAULTS 不认识" in r.stdout,
          text[-900:])


if __name__ == "__main__":
    sys.exit(main())
