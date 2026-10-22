# -*- coding: utf-8 -*-
"""翻译层自己的护栏：缓存、截断、方向、条数、防抖、候选去重。

为什么单独一个文件：README 承诺了「停手 250 毫秒后按当前最佳中文候选去翻」
「译文默认最多 4 条」「没配 API key 也有一条约 160 条的离线词表」，
但在此之前 `tests/` 里**没有任何一行**碰过 `Translator` 的缓存、`max_length`
截断、`enabled` 开关，也没有一行真的数过那张词表。状态机那边验的是
「按了什么键 -> 上屏了什么」，翻译层自己那几道闸门（命不命中缓存、
超长文本会不会原样发出去、失败会不会被缓存）一直是靠读代码相信的。

这里全部用假 provider（不联网、不花 token），只有词表那几条用真的 mock。

直接跑：``python tests/test_translate.py``
"""
import copy
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

from t_ime.config import load, sanitize  # noqa: E402
from t_ime.core import ROW_EN, IMECore, TranslateWorker  # noqa: E402
from t_ime.pinyin import PinyinEngine, load_or_build_index  # noqa: E402
from t_ime.translator import (  # noqa: E402
    EN2ZH, ZH2EN, DeepSeekProvider, LRUCache, MockProvider, Translator,
    _GLOSS_EN_ZH, _GLOSS_ZH_EN,
    _mock_en2zh, _mock_zh2en, _split_variants,
)

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


class FakeProvider:
    """不发网络请求的 provider；把每次调用记下来，方便断言「到底打了几次电话」。"""

    name = "fake"

    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []
        self.fail = False
        self.answer = lambda text, direction, n: [f"{direction}|{text}|{i}" for i in range(n)]

    def translate_many(self, text, direction, n=1):
        self.calls.append((text, direction, n))
        if self.fail:
            raise RuntimeError("boom")
        return self.answer(text, direction, n)

    def translate(self, text, direction):
        return self.translate_many(text, direction, 1)[0]


def mk_translator(**tr_over):
    """一份默认配置上的 Translator，provider 换成假的。"""
    cfg, _ = load()
    cfg = copy.deepcopy(cfg)
    cfg.setdefault("api", {})["api_key"] = ""
    tr_cfg = cfg.setdefault("translate", {})
    tr_cfg.update({"enabled": True, "max_length": 280, "cache_size": 1024, "variants": 4})
    tr_cfg.update(tr_over)
    tr = Translator(cfg, verbose=False)
    fake = FakeProvider()
    tr.provider = fake
    return tr, fake, cfg


def sec(title):
    print(f"\n== {title} ==")


# --------------------------------------------------------------------------- #
# 1. LRU 缓存本身
# --------------------------------------------------------------------------- #
def test_lru():
    sec("1. LRU 缓存：容量、淘汰顺序、返回副本")
    c = LRUCache(capacity=3)
    for k in "abc":
        c.put(k, [k.upper()])
    check("塞满 3 条后长度就是 3", len(c) == 3, len(c))
    check("三条都取得到", [c.get(k) for k in "abc"] == [["A"], ["B"], ["C"]],
          [c.get(k) for k in "abc"])

    # 上面那三次 get 让顺序变成 a→b→c，a 仍是最早的
    c.put("d", ["D"])
    check("超过容量后长度仍是 3", len(c) == 3, len(c))
    check("最早写进去的 a 被淘汰了", c.get("a") is None, c.get("a"))
    check("新来的 d 在", c.get("d") == ["D"], c.get("d"))

    # 把 b 摸一下，让它的位置变新；再塞一个，被淘汰的应该是没被摸过的 c
    c.get("b")
    c.put("e", ["E"])
    check("刚被 get 过的 b 没被淘汰（LRU 序真的在动）", c.get("b") == ["B"], c.get("b"))
    check("没被摸过的 c 被淘汰了", c.get("c") is None, c.get("c"))

    before = len(c)
    c.put("b", ["B2"])
    check("同一个键再写一次不增加条数", len(c) == before, (before, len(c)))
    check("同一个键的值被替换", c.get("b") == ["B2"], c.get("b"))

    got = c.get("b")
    got.append("污染")
    check("get 返回的是副本，改它污染不到缓存", c.get("b") == ["B2"], c.get("b"))

    c.clear()
    check("clear 之后是空的", len(c) == 0, len(c))


# --------------------------------------------------------------------------- #
# 2. Translator 的闸门
# --------------------------------------------------------------------------- #
def test_translator_gates():
    sec("2. Translator：缓存命中、方向与条数进 key、截断、失败不缓存")

    tr, fake, _ = mk_translator()
    got = tr.translate_many("你好", ZH2EN, 2)
    check("第一次真的去问 provider", len(fake.calls) == 1, fake.calls)
    check("provider 收到的条数就是 n", fake.calls[0][2] == 2, fake.calls)
    check("拿到 2 条", got == ["zh2en|你好|0", "zh2en|你好|1"], got)
    check("请求计数 +1", tr.stats["requests"] == 1, tr.stats)
    check("还没命中过缓存", tr.stats["cache_hits"] == 0, tr.stats)

    again = tr.translate_many("你好", ZH2EN, 2)
    check("同一句再来一次不再问 provider", len(fake.calls) == 1, fake.calls)
    check("命中计数 +1", tr.stats["cache_hits"] == 1, tr.stats)
    check("命中拿到的和第一次一样", again == got, again)

    tr.translate_many("  你好  ", ZH2EN, 2)
    check("前后空格不影响命中（先 strip 再做 key）", len(fake.calls) == 1, fake.calls)

    tr.translate_many("你好", EN2ZH, 2)
    check("同一个词换个方向要重新问（方向在 key 里）", len(fake.calls) == 2, fake.calls)

    tr.translate_many("你好", ZH2EN, 1)
    check("同一个词换个条数也要重新问（n 在 key 里）", len(fake.calls) == 3, fake.calls)

    tr2, fake2, _ = mk_translator(max_length=10)
    long_text = "一二三四五六七八九十十一十二"
    tr2.translate_many(long_text, ZH2EN, 1)
    seen = fake2.calls[0][0] if fake2.calls else ""
    check("超长文本按 max_length 截断后才发出去", len(seen) == 10, (len(seen), seen))
    check("发出去的就是前 10 个字", seen == long_text[:10], seen)
    tr2.translate_many(long_text[:10], ZH2EN, 1)
    check("截断后的文本与已缓存的那条是同一个 key", len(fake2.calls) == 1, fake2.calls)

    tr3, fake3, _ = mk_translator()
    check("空字符串不发请求", tr3.translate_many("   ", ZH2EN) == [], fake3.calls)
    check("空字符串也不记请求", tr3.stats["requests"] == 0, tr3.stats)

    tr4, fake4, _ = mk_translator(enabled=False)
    check("翻译关掉之后返回空", tr4.translate_many("你好", ZH2EN) == [], fake4.calls)
    check("翻译关掉之后一次请求都不发", fake4.calls == [], fake4.calls)

    tr5, fake5, _ = mk_translator()
    fake5.fail = True
    check("provider 抛异常时返回空列表而不是把异常抛上去",
          tr5.translate_many("你好", ZH2EN) == [], "raised")
    check("失败计数 +1", tr5.stats["errors"] == 1, tr5.stats)
    check("失败原因记下来了", "boom" in tr5.stats["last_error"], tr5.stats)
    fake5.fail = False
    out = tr5.translate_many("你好", ZH2EN)
    check("失败的结果不会被缓存（下一句还是问 provider）", len(fake5.calls) == 2, fake5.calls)
    check("provider 好了之后正常返回", out == [f"zh2en|你好|{i}" for i in range(4)], out)

    tr6, fake6, _ = mk_translator()
    fake6.answer = lambda text, direction, n: []
    check("provider 返回空列表时结果是空的", tr6.translate_many("你好", ZH2EN) == [], "")
    fake6.answer = lambda text, direction, n: ["好"]
    check("空结果也不缓存（第二次仍要问）", tr6.translate_many("你好", ZH2EN) == ["好"], "")
    check("空结果那次确实问了两次", len(fake6.calls) == 2, fake6.calls)

    tr7, fake7, _ = mk_translator()
    fake7.answer = lambda text, direction, n: ["A", "", None, "B"]
    out = tr7.translate_many("你好", ZH2EN)
    check("provider 给的空项被过滤掉", out == ["A", "B"], out)

    tr8, fake8, _ = mk_translator()
    check("translate() 只要一条", tr8.translate("你好", ZH2EN) == "zh2en|你好|0", tr8.translate("你好", ZH2EN))
    check("translate() 走的是 n=1", fake8.calls[0][2] == 1, fake8.calls)

    desc = tr8.describe()
    check("describe() 把缓存与请求数字说出来",
          "缓存=" in desc and "请求=" in desc and "命中=" in desc, desc)
    check("describe() 认得假 provider 的名字", "fake" in desc, desc)

    tr9, fake9, cfg9 = mk_translator(variants=3)
    tr9.translate_many("你好", ZH2EN)
    check("不传 n 时按配置里的 variants 走", fake9.calls[0][2] == 3, fake9.calls)


# --------------------------------------------------------------------------- #
# 3. 后台翻译线程的防抖 / 合并
# --------------------------------------------------------------------------- #
def test_worker():
    sec("3. TranslateWorker：防抖、只翻最新那句、能干净停下")

    tr, fake, _ = mk_translator()
    worker = TranslateWorker(tr, debounce=0.20, verbose=False)
    worker.start()
    for seq, text in ((1, "甲"), (2, "乙"), (3, "你好")):
        worker.submit(seq, text)
        time.sleep(0.03)
    got = worker.results.get(timeout=3.0)
    seq, text, out = got
    check("连按三下只产出一条结果", seq == 3, got)
    check("翻译的是最后那句", text == "你好", got)
    check("结果内容就是那句的译文", out == [f"zh2en|你好|{i}" for i in range(4)], out)
    check("中间那两句一次都没发出去", [c[0] for c in fake.calls] == ["你好"], fake.calls)
    check("结果队列没有多余的东西", worker.results.empty(), "")

    worker.stop()
    worker.join(2.0)
    check("stop() 之后线程真的退出了", not worker.is_alive(), worker.is_alive())

    # 防抖窗口内的密集输入：一次都不能发出去
    tr2, fake2, _ = mk_translator()
    w2 = TranslateWorker(tr2, debounce=0.30, verbose=False)
    w2.start()
    for seq in range(1, 9):
        w2.submit(seq, f"第{seq}句")
        time.sleep(0.02)
    seq2, text2, _ = w2.results.get(timeout=3.0)
    check("8 次密集提交只翻最后一次", (seq2, text2) == (8, "第8句"), (seq2, text2))
    check("前面 7 次一次都没发出去", len(fake2.calls) == 1, fake2.calls)
    w2.stop()
    w2.join(2.0)

    # debounce=0：立刻翻
    tr3, fake3, _ = mk_translator()
    w3 = TranslateWorker(tr3, debounce=0.0, verbose=False)
    w3.start()
    t0 = time.monotonic()
    w3.submit(1, "你好")
    r = w3.results.get(timeout=3.0)
    check("debounce=0 时几乎立刻就有结果", time.monotonic() - t0 < 1.0, time.monotonic() - t0)
    check("debounce=0 时结果就是那句", r[2] == [f"zh2en|你好|{i}" for i in range(4)], r)
    w3.stop()
    w3.join(2.0)

    # provider 炸了，线程不能死、也不能卡住
    tr4, fake4, _ = mk_translator()
    fake4.fail = True
    w4 = TranslateWorker(tr4, debounce=0.0, verbose=False)
    w4.start()
    w4.submit(1, "你好")
    r4 = w4.results.get(timeout=3.0)
    check("provider 抛异常时线程仍然回一条空结果", r4 == (1, "你好", []), r4)
    check("线程还活着（翻译炸了不影响打字）", w4.is_alive(), w4.is_alive())
    w4.stop()
    w4.join(2.0)


# --------------------------------------------------------------------------- #
# 4. 离线词表：README 说「约 160 条」
# --------------------------------------------------------------------------- #
def test_mock_glossary():
    sec("4. 离线词表：真的有多少条、查不到时给什么")
    print(f"  词表实际条数：中文→英文 {len(_GLOSS_ZH_EN)} 条，英文→中文 {len(_GLOSS_EN_ZH)} 条")
    check("中文→英文词表没有缩水到「约 160 条」以下", len(_GLOSS_ZH_EN) >= 150, len(_GLOSS_ZH_EN))
    check("中文→英文词表也没有膨胀得离谱（README 说的是约 160）",
          len(_GLOSS_ZH_EN) <= 200, len(_GLOSS_ZH_EN))
    check("英文→中文词表同样可用", len(_GLOSS_EN_ZH) >= 150, len(_GLOSS_EN_ZH))

    check("词表里查得到「你好」", _mock_zh2en("你好", 3) == ["Hello", "Hi there", "Greetings"],
          _mock_zh2en("你好", 3))
    check("词表里只有 3 种译法时就要 3 条，不拿假变体凑满 4 条",
          len(_mock_zh2en("你好", 4)) == 3, _mock_zh2en("你好", 4))

    unknown = _mock_zh2en("榅桲", 4)
    check("词表里查不到的词会打上 ⟦mock⟧ 标记", all("⟦mock" in x for x in unknown), unknown)
    check("查不到时不要把同一句话重复塞满候选行（会像是真的有 4 种译法）",
          len(set(unknown)) == 1 and len(unknown) == 1, unknown)

    unknown_en = _mock_en2zh("quince", 4)
    check("英文方向同样只给一条占位", len(unknown_en) == 1, unknown_en)
    check("英文方向的占位也带标记", "⟦mock" in unknown_en[0], unknown_en)

    mp = MockProvider()
    check("MockProvider 自报名字是 mock", mp.name == "mock", mp.name)
    check("MockProvider.translate 拿第一条",
          mp.translate("你好", ZH2EN) == "Hello", mp.translate("你好", ZH2EN))

    # 模型返回的「1. xxx / - xxx」要被拆干净
    raw = "1. Hello\n2) Hi there\n- Greetings\nHello\n"
    check("多行译文按行拆成候选", _split_variants(raw, 4) == ["Hello", "Hi there", "Greetings"],
          _split_variants(raw, 4))
    check("重复的行只留一条", _split_variants("Hello\nHello\nBye", 4) == ["Hello", "Bye"],
          _split_variants("Hello\nHello\nBye", 4))
    check("条数上限生效", _split_variants("a\nb\nc\nd\ne", 3) == ["a", "b", "c"],
          _split_variants("a\nb\nc\nd\ne", 3))
    check("引号会被剥掉", _split_variants('"Hello"', 1) == ["Hello"], _split_variants('"Hello"', 1))


# --------------------------------------------------------------------------- #
# 5. 接进状态机：条数上限、过期结果、失败状态、缓存真的被用上
# --------------------------------------------------------------------------- #
def test_core_integration():
    sec("5. 接进状态机：译文行条数上限、过期结果丢弃、失败状态、第二次同样输入命中缓存")

    index = load_or_build_index(ROOT / "pinyin_index.pkl", min_freq=20, max_word_len=6, verbose=False)
    eng = PinyinEngine(index, beam_width=24, max_candidates=7)

    tr, fake, cfg = mk_translator()
    cfg["translate"].update({"debounce_ms": 30, "variants": 4})
    fake.answer = lambda text, direction, n: [f"v{i}" for i in range(6)]  # provider 故意多给
    core = IMECore(cfg, eng, tr, committer=lambda t: True, verbose=False,
                   mods_source=lambda: frozenset())
    try:
        for ch in "nihao":
            vk = ord(ch.upper())
            core.handle_key(vk, True, False)
            core.handle_key(vk, False, False)
        check("打完拼音后是 pending", core.tr_state == "pending", core.tr_state)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and core.tr_state == "pending":
            time.sleep(0.02)
            core.apply_translation_results()
        check("译文回来了", core.tr_state == "done", core.tr_state)
        check("provider 给了 6 条，译文行最多 4 条", len(core.en) == 4, core.en)

        # 过期结果：把 seq 改成旧号再塞进队列，必须被丢掉
        core.worker.results.put((core._tr_seq - 1, "旧的", ["STALE"]))
        core.apply_translation_results()
        check("过期的翻译结果被丢掉（不会把旧译文盖上来）", "STALE" not in core.en, core.en)

        # 同样的输入再来一遍：所有请求都该命中缓存
        reqs_before = tr.stats["requests"]
        hits_before = tr.stats["cache_hits"]
        core.handle_key(0x1B, True, False)   # Esc：清空缓冲
        core.handle_key(0x1B, False, False)
        check("Esc 之后缓冲是空的", core.buffer == "", core.buffer)
        for ch in "nihao":
            vk = ord(ch.upper())
            core.handle_key(vk, True, False)
            core.handle_key(vk, False, False)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and core.tr_state == "pending":
            time.sleep(0.02)
            core.apply_translation_results()
        check("第二遍同样输入，一次 provider 都没再问", tr.stats["requests"] == reqs_before,
              (reqs_before, tr.stats))
        check("第二遍全从缓存拿（命中数涨了）", tr.stats["cache_hits"] > hits_before,
              (hits_before, tr.stats))
        check("第二遍译文照样回来了", core.tr_state == "done" and len(core.en) == 4,
              (core.tr_state, core.en))

        # 失败：provider 给空 → tr_state 变 failed，并且说清楚原因
        fake.answer = lambda text, direction, n: []
        core.handle_key(0x1B, True, False)
        core.handle_key(0x1B, False, False)
        for ch in "shang":
            vk = ord(ch.upper())
            core.handle_key(vk, True, False)
            core.handle_key(vk, False, False)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and core.tr_state == "pending":
            time.sleep(0.02)
            core.apply_translation_results()
        check("翻译失败时状态是 failed", core.tr_state == "failed", core.tr_state)
        check("翻译失败时把原因写在 status 上", "翻译失败" in core.status, core.status)
    finally:
        core.shutdown()


def test_provider_choice():
    sec("6. api.provider：模板里那个选项真的决定走在线还是离线")

    cfg, _ = load()
    cfg = copy.deepcopy(cfg)
    cfg["api"]["api_key"] = "sk-test"
    cfg["api"]["provider"] = "mock"
    check("provider=mock + 有 key 时也强制离线（不想花钱 / 没网时用）",
          isinstance(Translator(cfg, verbose=False).provider, MockProvider),
          type(Translator(cfg, verbose=False).provider).__name__)

    cfg["api"]["provider"] = "deepseek"
    check("provider=deepseek + 有 key 时走在线",
          isinstance(Translator(cfg, verbose=False).provider, DeepSeekProvider),
          type(Translator(cfg, verbose=False).provider).__name__)

    cfg["api"]["api_key"] = ""
    check("provider=deepseek 但没 key 时退回离线（不能干等着报错）",
          isinstance(Translator(cfg, verbose=False).provider, MockProvider),
          type(Translator(cfg, verbose=False).provider).__name__)

    cfg["api"]["provider"] = ""
    cfg["api"]["api_key"] = "sk-test"
    check("provider 留空 + 有 key 时按默认走在线",
          isinstance(Translator(cfg, verbose=False).provider, DeepSeekProvider),
          type(Translator(cfg, verbose=False).provider).__name__)

    bad = copy.deepcopy(cfg)
    bad["api"]["provider"] = "openai"
    probs = sanitize(bad)
    check("provider 写不认识的值会被点名",
          any("api.provider" in p and "deepseek" in p for p in probs), probs)
    check("provider 写错后退回默认 deepseek", bad["api"]["provider"] == "deepseek",
          bad["api"]["provider"])
    check("修好后的配置按默认值继续走在线",
          isinstance(Translator(bad, verbose=False).provider, DeepSeekProvider),
          type(Translator(bad, verbose=False).provider).__name__)

    # 第 62 轮：离线有两种原因，说错就会冤枉「没配 key」。
    cfg["api"]["provider"] = "mock"
    cfg["api"]["api_key"] = "sk-test"
    forced = Translator(cfg, verbose=False)
    check("forced mock 记得「是你自己选的离线」",
          "api.provider=mock" in forced.mock_reason, forced.mock_reason)
    check("forced mock 的 describe() 不再说「未配置 API key」",
          "未配置 API key" not in forced.describe() and "api.provider=mock" in forced.describe(),
          forced.describe())

    cfg["api"]["provider"] = "deepseek"
    cfg["api"]["api_key"] = ""
    passive = Translator(cfg, verbose=False)
    check("没配 key 时如实说「没配 API key」",
          passive.mock_reason == "没配 API key", passive.mock_reason)
    check("没配 key 的 describe() 带这句话",
          "没配 API key" in passive.describe(), passive.describe())

    cfg["api"]["api_key"] = "sk-test"
    online = Translator(cfg, verbose=False)
    check("在线时没有 mock_reason（别把在线也说成离线）",
          online.mock_reason == "" and not online.is_mock, online.mock_reason)

    # 第 78 轮：translate.enabled=false 时引擎一次都没开工，describe() 必须
    # 先说「翻译已关闭」，否则「mock（没配 key）」会被读成「翻译在跑，只是离线」。
    cfg_off = copy.deepcopy(cfg)
    cfg_off["translate"]["enabled"] = False
    off = Translator(cfg_off, verbose=False)
    check("关掉翻译时 describe() 把「已关闭」放在最前面",
          off.describe().startswith("翻译已关闭（translate.enabled=false）"), off.describe())
    check("关掉翻译时 translate_many 直接返回空、也不记账",
          off.translate_many("你好", ZH2EN, n=3) == [] and off.stats["requests"] == 0,
          off.stats)
    cfg_on = copy.deepcopy(cfg_off)
    cfg_on["translate"]["enabled"] = True
    check("开关改回 true 就恢复正常（不是一次性的）",
          "翻译已关闭" not in Translator(cfg_on, verbose=False).describe())


def test_error_kinds():
    sec("7. 失败的种类要记下来：没网 / 服务器拒绝 / 回了垃圾（第 117 轮）")
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from t_ime.translator import TranslateError

    # 一个没人听的端口：连接会被立刻拒绝（不是超时，测试才快）
    _s = socket.socket()
    _s.bind(("127.0.0.1", 0))
    _dead = _s.getsockname()[1]
    _s.close()

    def cfg_for(base: str) -> dict:
        cfg, _ = load()
        cfg = copy.deepcopy(cfg)
        cfg["translate"]["enabled"] = True
        cfg["api"]["provider"] = "deepseek"
        cfg["api"]["api_key"] = "sk-probe-not-real"
        cfg["api"]["timeout"] = 1.0
        cfg["api"]["base_url"] = base
        return cfg

    check("没失败过时 error_kind 是空串",
          Translator(cfg_for("https://api.deepseek.com"), verbose=False).error_kind == "")

    tr = Translator(cfg_for(f"http://127.0.0.1:{_dead}/v1"), verbose=False)
    check("连不上时返回空列表（老契约不变）", tr.translate_many("你好", ZH2EN, n=1) == [])
    check("★ 连不上记为 network", tr.error_kind == "network", tr.stats)
    check("★ 连不上时没有 HTTP 状态码", tr.error_status is None, tr.stats)

    seen = {"path": ""}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen["path"] = self.path
            if self.path.startswith("/401"):
                body = b'{"error": {"message": "Authentication Fails"}}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
            else:
                body = b"<html>not json at all</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # noqa: A003
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        tr2 = Translator(cfg_for(f"http://127.0.0.1:{port}/401"), verbose=False)
        check("401 时返回空列表", tr2.translate_many("你好", ZH2EN, n=1) == [])
        check("★ 401 记为 http", tr2.error_kind == "http", tr2.stats)
        check("★ 401 的状态码记下来了", tr2.error_status == 401, tr2.stats)
        check("★ 原因里带着 HTTP 401", "HTTP 401" in tr2.stats["last_error"], tr2.stats["last_error"])

        tr3 = Translator(cfg_for(f"http://127.0.0.1:{port}/garbage"), verbose=False)
        check("回了垃圾时返回空列表", tr3.translate_many("你好", ZH2EN, n=1) == [])
        check("★ 回了 200 但内容不是 JSON，记为 parse", tr3.error_kind == "parse", tr3.stats)
        check("★ parse 失败没有 HTTP 状态码", tr3.error_status is None, tr3.stats)
    finally:
        srv.shutdown()

    special = TranslateError("boom", kind="http", status=503)
    check("TranslateError 自己带着 kind / status",
          special.kind == "http" and special.status == 503 and isinstance(special, RuntimeError))


def main() -> int:
    test_lru()
    test_translator_gates()
    test_worker()
    test_mock_glossary()
    test_core_integration()
    test_provider_choice()
    test_error_kinds()
    print(f"\n  结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
