# -*- coding: utf-8 -*-
"""自学习权重的端到端测试：**真的写进 user_boosts.json 了吗、重启之后真的顶到首位了吗**。

README 的特性清单里写着：

    自学习 —— 你用数字键改选了非首选项，那个词会被记住并加权，下次顶到首位。
    权重存在 ``user_boosts.json``。

但在这之前，全仓库**没有一行测试碰过 ``save_boosts`` / ``load_boosts`` / ``boost_path``：
``tests/test_core.py`` 那一节只验了「同一个引擎里被顶到首位」（内存），
文件那一层是空的。而文件那一层恰恰是这个承诺的全部 —— 进程一重启，
内存里那份权重就没了。

写这个测试时发现并修掉的真问题：``save_boosts()`` **只被 ``shutdown()`` 调过一次**。
于是：

* 干净退出（托盘退出 / ``Ctrl+Alt+Q`` / 投一条 ``quit``）→ 权重落盘，没问题；
* 被 ``taskkill /F``、崩溃、注销、断电 → **这一场学到的全丢**；
* 常驻用户根本不主动退出 —— 也就是说这条「自学习」在最常见的用法下几乎不落盘。

现在心跳里会定期存一次（``core.BOOST_SAVE_INTERVAL`` 节流，没脏就立刻返回）。

直接跑：``python tests/test_boosts.py``
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime import daemon  # noqa: E402
from t_ime.app import App  # noqa: E402
from t_ime.config import load as load_config  # noqa: E402
from t_ime.console import fix_console_encoding  # noqa: E402
from t_ime.core import BOOST_SAVE_INTERVAL, IMECore  # noqa: E402
from t_ime.pinyin import PinyinEngine, load_or_build_index  # noqa: E402
from t_ime.translator import Translator  # noqa: E402

fix_console_encoding()

PASS = 0
FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok ] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  <-- {extra!r}")


TMP = Path(tempfile.mkdtemp(prefix="t-ime-boost-"))
BOOST = TMP / "user_boosts.json"

cfg, cfg_path = load_config()
cfg["output"]["boost_file"] = str(BOOST)       # 绝对路径 → core 不再拿项目根去拼
if isinstance(cfg.get("api"), dict):
    cfg["api"]["api_key"] = ""                 # 别真去调网络，mock 就够

INDEX = load_or_build_index(
    PROJ / "pinyin_index.pkl", min_freq=20, max_word_len=6, verbose=False
)

WORD = "上传"
PINYIN = "shangchuan"


def new_engine():
    return PinyinEngine(
        INDEX,
        beam_width=int(cfg["pinyin"]["beam_width"]),
        max_candidates=int(cfg["pinyin"]["max_candidates"]),
    )


def new_core(engine=None, sent=None):
    engine = engine if engine is not None else new_engine()
    core = IMECore(
        cfg, engine, Translator(cfg, verbose=False),
        committer=(lambda t: (sent.append(t), True)[1]) if sent is not None else None,
        verbose=False,
    )
    return core, engine


def tapstr(core, s):
    for ch in s:
        vk = ord(ch.upper())
        core.handle_key(vk, True, False)
        core.handle_key(vk, False, False)


print("=" * 74)
print(f"配置来自 {cfg_path}")
print(f"临时权重文件 {BOOST}")
print("=" * 74)

print()
print("== 1. 用数字键改选一个非首选项 → 记忆在内存里、dirty 置位 ==")
sent: list[str] = []
core, engine = new_core(sent=sent)
tapstr(core, PINYIN)
zh = list(core.zh)
print(f"        候选: {zh}")
pick = next((i for i, w in enumerate(zh) if w == WORD), None)
check(f"候选里有『{WORD}』（不然这个场景没法演）", pick is not None, zh)
check(f"而且它不在首位（改选才有意义：{WORD} 必须是非首选项）",
      pick is not None and pick > 0, (pick, zh))
check("文件此刻还没有（没人存过）", not BOOST.exists(), str(BOOST))
if pick is None or pick == 0:
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{PASS} 通过 / {FAIL} 失败")
    sys.exit(1)

core.handle_key(0x31 + pick, True, False)
core.handle_key(0x31 + pick, False, False)
check(f"数字键上屏的是『{WORD}』（不是首选项）", sent == [WORD], sent)
check("权重被标脏（等着落盘）", core._boosts_dirty is True)
check("内存里的权重记着这个词", engine.user_boosts() == {WORD: 1.0}, engine.user_boosts())

print()
print("== 2. 落盘节流：刚改选马上存会被 BOOST_SAVE_INTERVAL 挡住 ==")
check(f"节流窗口是 {BOOST_SAVE_INTERVAL:g} 秒", BOOST_SAVE_INTERVAL > 0, BOOST_SAVE_INTERVAL)
check("立刻 save_boosts() 返回 False（被节流挡下）", core.save_boosts() is False)
check("被挡住的时候没写文件", not BOOST.exists(), str(BOOST))
wait = BOOST_SAVE_INTERVAL + 0.3
print(f"        等 {wait:.1f} 秒…")
time.sleep(wait)
check("过了节流窗口再存 → True（真的写下去了）", core.save_boosts() is True)
check("文件出现了", BOOST.exists(), str(BOOST))
data = json.loads(BOOST.read_text(encoding="utf-8")) if BOOST.exists() else {}
check(f"文件里就是刚学到的那个词（{WORD}）", data.get(WORD) == 1.0, data)
check("存完之后不再是脏的", core._boosts_dirty is False)
check("没有变化时再存一次 → False（不会白写盘）", core.save_boosts() is False)

print()
print("== 3. 重启：新引擎从文件载入 → 那个词真的顶到首位 ==")
engine2 = new_engine()
check("新引擎一开始没有权重（模拟重启）", engine2.user_boosts() == {}, engine2.user_boosts())
core2, _ = new_core(engine=engine2)
loaded = core2.load_boosts()
check("load_boosts() 读到了 1 条", loaded == 1, loaded)
check("权重进了新引擎", engine2.user_boosts() == {WORD: 1.0}, engine2.user_boosts())
sent2: list[str] = []
core2._committer = lambda t: (sent2.append(t), True)[1]
tapstr(core2, PINYIN)
check(f"重启后『{WORD}』顶到了首位（README 的承诺）",
      bool(core2.zh) and core2.zh[0] == WORD, core2.zh[:4])
core2.handle_key(0x20, True, False)   # 空格 = 上屏首选项
core2.handle_key(0x20, False, False)
check(f"空格上屏的就是它（{WORD}）", sent2 == [WORD], sent2)

print()
print("== 4. 坏文件不许把启动搞崩（手工编辑过的 user_boosts.json）==")
BOOST.write_text("\ufeff" + json.dumps({WORD: 2.0}, ensure_ascii=False), encoding="utf-8")
core3, engine3 = new_core()
check("带 BOM 的文件也能读（utf-8-sig）", core3.load_boosts() == 1)
check("BOM 没把权重吃掉", engine3.user_boosts() == {WORD: 2.0}, engine3.user_boosts())
BOOST.write_text("{ 这不是 json", encoding="utf-8")
core4, _ = new_core()
check("垃圾内容返回 0、不抛异常", core4.load_boosts() == 0)
core4.handle_key(ord("N"), True, False)
core4.handle_key(ord("N"), False, False)
check("读坏了也照样能打字", core4.buffer == "n", core4.buffer)
BOOST.write_text("[]", encoding="utf-8")
core5, _ = new_core()
check("合法 JSON 但不是对象 → 0", core5.load_boosts() == 0)
BOOST.unlink(missing_ok=True)   # 坏/非字典的文件会被改名留证（.broken），这里可能已经不在
core6, _ = new_core()
check("文件不存在 → 0（第一次用没有文件是正常的）", core6.load_boosts() == 0)

print()
print("== 4b. 某一项的值不是数字：跳过坏项、好的一项照常生效、不把启动搞崩 ==")
# user_boosts.json 是用户能手改的文件，也可能被别的程序写坏。
# 旧代码里 float("abc") 的 ValueError 会从 load_boosts 里直接冒出来 → App 起不来，
# 后台（pythonw）用户看到的就是「双击 run.bat 什么都没发生」。
BOOST.write_text(
    '{"你好": "abc", "世界": 1.5, "邮箱": [1, 2], "空": null, "坏": NaN, "坏2": Infinity}',
    encoding="utf-8",
)
core7, engine7 = new_core()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    loaded7 = core7.load_boosts()
out7 = buf.getvalue()
check("坏值不再抛异常，load_boosts() 正常返回", loaded7 == 1, loaded7)
check("只认了那一条合法权重", engine7.user_boosts() == {"世界": 1.5}, engine7.user_boosts())
check("字符串值被跳过", "你好" not in engine7.user_boosts(), engine7.user_boosts())
check("数组 / null 值被跳过",
      "邮箱" not in engine7.user_boosts() and "空" not in engine7.user_boosts(),
      engine7.user_boosts())
check("NaN / inf 也被跳过（它们能转成 float，但会毒死排序）",
      "坏" not in engine7.user_boosts() and "坏2" not in engine7.user_boosts(),
      engine7.user_boosts())
check("丢了几项要说出来（不是静默丢）", "5 项不是数字" in out7, out7)
check("而且点名了是哪个文件", str(BOOST) in out7, out7)

# 混着坏值读进来的好词，仍然要真的被加权（不能因为一个坏值整份丢弃）。
fresh = new_engine()
before_list = fresh.candidates(PINYIN)
idx_before = before_list.index(WORD) if WORD in before_list else 99
fresh.load_boosts({"你好": "abc", WORD: 1.0})
after_list = fresh.candidates(PINYIN)
idx_after = after_list.index(WORD) if WORD in after_list else 99
check("好的一项照常生效（候选被顶得更前）", idx_after <= idx_before, (idx_before, idx_after))

core7._boosts_dirty = True
check("坏值文件也能被下一次保存洗干净", core7.save_boosts(force=True) is True)
clean = json.loads(BOOST.read_text(encoding="utf-8"))
check("干净文件里只剩合法权重", clean == {"世界": 1.5}, clean)

for label, payload in (("列表", "[]"), ("数字", "5"), ("字符串", '"hi"')):
    BOOST.write_text(payload, encoding="utf-8")
    corex, enginex = new_core()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = corex.load_boosts()
    txtx = buf.getvalue()
    check(f"根是{label} → 返回 0、不抛异常、也不多加载",
          got == 0 and enginex.user_boosts() == {} and "不是字典" in txtx,
          (got, enginex.user_boosts(), txtx))

BOOST.unlink(missing_ok=True)

print()
print("== 5. 主循环那条路：跑着不退出，也要能落盘（本轮修的就是这个）==")
BOOST2 = TMP / "tick_boosts.json"
cfg2 = dict(cfg)
cfg2["output"] = dict(cfg["output"], boost_file=str(BOOST2))
# 这个 App 会写真实的状态文件、读命令收件箱 —— 别把用户正在用的那个实例的痕迹弄丢
status_path = daemon.status_path(PROJ)
ctl_path = daemon.ctl_path(PROJ)
backup: dict[Path, bytes] = {}
for p in (status_path, ctl_path):
    if p.exists():
        backup[p] = p.read_bytes()
app = App(cfg2, PROJ, verbose=False, install_hook=False)
try:
    app._running = True
    sentA: list[str] = []
    app.core._committer = lambda t: (sentA.append(t), True)[1]
    tapstr(app.core, PINYIN)
    zhA = list(app.core.zh)
    pickA = next((i for i, w in enumerate(zhA) if w == WORD), 1)
    app.core.handle_key(0x31 + pickA, True, False)
    app.core.handle_key(0x31 + pickA, False, False)
    check("实例里改选成功（内存里记下了）", app.core._boosts_dirty is True,
          app.core.engine.user_boosts())
    check("此刻文件还没写（节流窗口内）", not BOOST2.exists(), str(BOOST2))

    deadline = time.time() + BOOST_SAVE_INTERVAL + 3.0
    while time.time() < deadline and not BOOST2.exists():
        app._tick()          # 心跳，等于真实运行时的那个循环
        time.sleep(0.05)
    check("★ 跑着的时候（没退出）心跳把权重写下去了", BOOST2.exists(), str(BOOST2))
    ticked = json.loads(BOOST2.read_text(encoding="utf-8")) if BOOST2.exists() else {}
    check("写下去的就是刚学的那个词", ticked.get(zhA[pickA]) == 1.0, ticked)
    check("落盘计数 +1（状态文件里也看得到）", app._boost_saves >= 1, app._boost_saves)

    # 状态文件是「心跳那一刻的快照」：落盘发生在 _publish_status() 之后，
    # 所以这一次心跳写下去的 boost_saves 还是旧的，要再等一轮才看得到。
    # （这也是真机上的行为：面板最多晚一个心跳显示。）
    st: dict = {}
    deadline = time.time() + 4.0
    while time.time() < deadline:
        app._tick()
        st = daemon.read_status(PROJ) or {}
        if int(st.get("boost_saves") or 0) >= 1:
            break
        time.sleep(0.1)
    check("状态文件里报了记住几个词", int(st.get("boosts") or 0) >= 1, st.get("boosts"))
    check("状态文件里报了落盘次数（下一轮心跳追上）",
          int(st.get("boost_saves") or 0) >= 1, st.get("boost_saves"))

    app.shutdown()          # 干净退出仍然要存一次（force），两条路都得留着
    after = json.loads(BOOST2.read_text(encoding="utf-8")) if BOOST2.exists() else {}
    check("干净退出之后文件还在、内容还在", after.get(zhA[pickA]) == 1.0, after)
finally:
    for p in (status_path, ctl_path):
        if p in backup:
            p.write_bytes(backup[p])
        elif p.exists():
            p.unlink()

shutil.rmtree(TMP, ignore_errors=True)

print()
print("== 6. boost_file 的父目录：不存在要自己建，建不出来要说清楚、而且只报一次 ==")
# 第 81 轮真机：``"boost_file": "data/boosts.json"``（子目录还不存在）→ 实例每个心跳
# 写失败一次、日志 3 秒多 1.1 KB 且一条权重都没落盘，而面板一直挂着「最多 5 秒后落盘」。
DEEP = TMP / "deep" / "deeper" / "boosts.json"
cfg_deep = dict(cfg)
cfg_deep["output"] = dict(cfg["output"], boost_file=str(DEEP))
core_deep, engine_deep = new_core()
core_deep.boost_path = DEEP
engine_deep.boost("你好", 1.0)
core_deep._boosts_dirty = True
check("父目录本来不存在", not DEEP.parent.exists(), str(DEEP.parent))
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    saved_deep = core_deep.save_boosts(force=True)
out_deep = buf.getvalue()
check("★ 父目录不存在也能落盘（自己建目录）", saved_deep is True, out_deep)
check("文件真的写出来了", DEEP.exists(), str(DEEP))
check("内容就是学到的那条", json.loads(DEEP.read_text(encoding="utf-8")).get("你好") == 1.0)
check("成功时不留错误原因", core_deep.boost_error == "", core_deep.boost_error)

# 父路径被一个「文件」占住：这是真会写不进去的情形（目录建不出来）。
BLOCKED_DIR = TMP / "blocked"
BLOCKED_DIR.mkdir(parents=True, exist_ok=True)
BLOCKER = BLOCKED_DIR / "not-a-dir"
BLOCKER.write_text("我是个文件", encoding="utf-8")
BLOCKED = BLOCKER / "boosts.json"
core_bad, engine_bad = new_core()
core_bad.boost_path = BLOCKED
engine_bad.boost("你好", 1.0)
core_bad._boosts_dirty = True
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    bad1 = core_bad.save_boosts(force=True)
    bad2 = core_bad.save_boosts(force=True)
out_bad = buf.getvalue()
check("写不进去时返回 False", bad1 is False and bad2 is False, (bad1, bad2))
check("★ 连调两次只报一行（不是每拍刷一行）",
      len([ln for ln in out_bad.splitlines() if "写入自学习权重" in ln]) == 1, out_bad)
check("错误原因可读（点名是文件不是目录）", "是个文件，不是目录" in core_bad.boost_error,
      core_bad.boost_error)
check("错误原因点名了那个路径", str(BLOCKER) in core_bad.boost_error, core_bad.boost_error)
BLOCKER.unlink()
core_bad._last_boost_save = 0.0
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    fixed = core_bad.save_boosts(force=True)
check("★ 路径修好后自动补写（不用重启）", fixed is True and BLOCKED.exists(), out_bad)
check("补写成功后错误原因被清空", core_bad.boost_error == "", core_bad.boost_error)

print()
print("== 7. 权重文件读不懂时：不许就地覆盖，要留一份 .broken 给用户 ==")
# 第 87 轮真机：user_boosts.json 少个引号（`{"nihao": 3, oops`）→ 实例只打一行日志、
# 整份忽略，而下一次保存直接把这份文件写成新内容 —— 用户那些词（也许只差一个逗号）
# 永久没了，他唯一的线索是一行可能根本没看到的日志。--check 当时还判 `[ok] 已存在`。
BROKEN = TMP / "broken_boosts.json"
raw_bad = '{"nihao": 3, oops'
BROKEN.write_text(raw_bad, encoding="utf-8")
core_br, engine_br = new_core()
core_br.boost_path = BROKEN
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    got_br = core_br.load_boosts()
out_br = buf.getvalue()
kept = BROKEN.with_name(BROKEN.name + ".broken")
check("坏 JSON 返回 0、不抛异常", got_br == 0, got_br)
check("★ 原文件被改名留成 .broken（不是删掉）", kept.exists(), str(kept))
check("★ .broken 里的内容和用户写的一模一样（一个字节都没丢）",
      kept.read_text(encoding="utf-8") == raw_bad, kept.read_text(encoding="utf-8") if kept.exists() else "")
check("原路径上不再有文件（下一次保存不会覆盖它）", not BROKEN.exists(), str(BROKEN))
check("错误原因记下来了（面板/状态要报它）", "JSONDecodeError" in core_br.boost_load_error,
      core_br.boost_load_error)
check("日志里说了「留成 .broken」", ".broken" in out_br, out_br)

engine_br.boost("你好", 1.0)
core_br._boosts_dirty = True
saved_br = core_br.save_boosts(force=True)
check("读坏之后仍然能正常记新词、落盘", saved_br is True and BROKEN.exists(), saved_br)
check("★ 新文件写出来了，但 .broken 一个字节没动（用户的词还在）",
      kept.read_text(encoding="utf-8") == raw_bad, kept.read_text(encoding="utf-8") if kept.exists() else "")
check("新文件里只有新学的那条", json.loads(BROKEN.read_text(encoding="utf-8")) == {"你好": 1.0})

# 第二份坏文件不许把第一份 .broken 覆盖掉。
BROKEN.write_text("{ 又坏了", encoding="utf-8")
core_br2, _ = new_core()
core_br2.boost_path = BROKEN
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    core_br2.load_boosts()
check("★ 第二次再坏 → 生成 .broken2，不覆盖 .broken",
      kept.exists() and BROKEN.with_name(BROKEN.name + ".broken2").exists(),
      sorted(p.name for p in TMP.glob("broken_boosts*")))

# 好文件不该误报。
GOOD = TMP / "good_boosts.json"
GOOD.write_text(json.dumps({"你好": 2.0, "世界": 1.0}), encoding="utf-8")
core_good, engine_good = new_core()
core_good.boost_path = GOOD
check("好文件返回词数", core_good.load_boosts() == 2, core_good.boost_load_error)
check("好文件不留读入错误", core_good.boost_load_error == "", core_good.boost_load_error)

shutil.rmtree(TMP, ignore_errors=True)

print()
print("=" * 74)
print(f"{PASS} 通过 / {FAIL} 失败")
print("=" * 74)
sys.exit(1 if FAIL else 0)
