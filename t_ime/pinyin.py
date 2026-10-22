# -*- coding: utf-8 -*-
"""拼音引擎：音节判定 + 词库反向索引 + N-best 候选。

设计要点
--------
1. **索引键用「拼接后的拼音字符串」**，而不是音节元组。
   这样 ``先``(xian) 和 ``西安``(xi+an) 天然落在同一个键 ``"xian"`` 上，
   搜索只需要对原始输入串切片查表，不必先枚举所有音节切分。
2. **词库来自 jieba 自带的 dict.txt**（约 35 万词，带词频），用 pypinyin
   反算拼音建索引；单字条目 jieba 词库里本来就有，不用另外补。
3. **打分在建索引时就烘死**成 ``log P(词) + 词长先验``，运行时只做加法::

       score(w) = log(freq(w)) - log(总词频) + WORD_LENGTH_PRIOR * (len(w) - 1)

   这两项都是实测调出来的（76 条常用语料上的 top-1 命中率）：

   * 最初写成 ``log(freq) * len(w)`` 再累加，**每一项都是正分，切得越碎
     总分越高**，于是 ``nihao`` 给出的首选是「你哈哦」而不是「你好」，
     命中率只有 **6%**。换成对数概率后词数越多总分越负，才是语言模型
     该有的样子 —— 单这一步就到 **88%**。
   * 光有 unigram 概率仍不够：jieba 的词频是为「分词」调过的，天然偏袒
     单字（``你``=234587，而 ``你好`` 只有 725）。再加一个随词长线性增长
     的先验，语义是「能用长词解释就别拆碎」，命中率 **88% -> 92%**。

4. 搜索结果用 **DAG 上的 K-best Viterbi**（按位置分层）而不是普通 beam，
   原因见 :meth:`PinyinEngine._search` 的注释。
5. 索引构建一次后 pickle 缓存，二次启动毫秒级加载。
"""
from __future__ import annotations

import hashlib
import math
import pickle
import re
import time
from pathlib import Path

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# 拼音输入允许的字符（字母 + 分隔符）
PINYIN_INPUT_RE = re.compile(r"^[a-z']+$")

INDEX_VERSION = 4
MAX_KEY_CHARS = 18          # 单个词条的拼音串最长字符数
MAX_ENTRIES_PER_KEY = 8     # 每个键保留多少个候选词
WORD_LENGTH_PRIOR = 1.0     # 词长先验强度，实测 1.0 最优（见模块 docstring）
USER_BOOST_WEIGHT = 2.0     # 用户每选中一次某候选，给它加多少分

# 最近一次 load_or_build_index 写缓存失败的原因（空串 = 一切正常）。
# --check / --doctor 靠它把「索引没落盘、下次启动还得重建」说出来：
# 以前这里只往日志打一行「不影响使用」，可它明明影响每次启动的耗时（第 89 轮）。
LAST_CACHE_ERROR = ""


# --------------------------------------------------------------------------- #
# 标准汉语拼音音节表（无声调）
# --------------------------------------------------------------------------- #
_RAW_SYLLABLES = """
a ai an ang ao
ba bai ban bang bao bei ben beng bi bian biao bie bin bing bo bu
ca cai can cang cao ce cen ceng cha chai chan chang chao che chen cheng chi
chong chou chu chua chuai chuan chuang chui chun chuo ci cong cou cu cuan cui cun cuo
da dai dan dang dao de dei den deng di dia dian diao die ding diu dong dou du duan dui dun duo
e ei en eng er
fa fan fang fei fen feng fo fou fu
ga gai gan gang gao ge gei gen geng gong gou gu gua guai guan guang gui gun guo
ha hai han hang hao he hei hen heng hong hou hu hua huai huan huang hui hun huo
ji jia jian jiang jiao jie jin jing jiong jiu ju juan jue jun
ka kai kan kang kao ke kei ken keng kong kou ku kua kuai kuan kuang kui kun kuo
la lai lan lang lao le lei leng li lia lian liang liao lie lin ling liu lo long lou
lu luan lue lun luo lv lve
ma mai man mang mao me mei men meng mi mian miao mie min ming miu mo mou mu
na nai nan nang nao ne nei nen neng ni nian niang niao nie nin ning niu nong nou
nu nuan nue nun nuo nv nve
o ou
pa pai pan pang pao pei pen peng pi pian piao pie pin ping po pou pu
qi qia qian qiang qiao qie qin qing qiong qiu qu quan que qun
ran rang rao re ren reng ri rong rou ru rua ruan rui run ruo
sa sai san sang sao se sen seng sha shai shan shang shao she shei shen sheng shi
shou shu shua shuai shuan shuang shui shun shuo si song sou su suan sui sun suo
ta tai tan tang tao te teng ti tian tiao tie ting tong tou tu tuan tui tun tuo
wa wai wan wang wei wen weng wo wu
xi xia xian xiang xiao xie xin xing xiong xiu xu xuan xue xun
ya yan yang yao ye yi yin ying yo yong you yu yuan yue yun
za zai zan zang zao ze zei zen zeng zha zhai zhan zhang zhao zhe zhei zhen zheng
zhi zhong zhou zhu zhua zhuai zhuan zhuang zhui zhun zhuo zi zong zou zu zuan zui zun zuo
"""
SYLLABLES = frozenset(_RAW_SYLLABLES.split())
MAX_SYLLABLE_LEN = max(len(s) for s in SYLLABLES)  # 6 = zhuang/chuang/shuang


def canon_syllable(s: str) -> str:
    """把 ü 的各种写法归一化（pypinyin 用 v）。"""
    if "ü" in s or "u:" in s:
        s = s.replace("ü", "v").replace("u:", "v")
    # lüe/nüe 常被写成 lue/nue，统一到 v 形式
    if s in ("lue", "nue"):
        return s[0] + "ve"
    return s


def _canon_word_pinyin(py: list[str]) -> str:
    return "".join(canon_syllable(p) for p in py)


def can_segment(s: str) -> bool:
    """s 能否被完全切成合法拼音音节（用来判断用户打的是拼音还是英文）。"""
    n = len(s)
    if n == 0:
        return False
    ok = [False] * (n + 1)
    ok[0] = True
    for i in range(n):
        if not ok[i]:
            continue
        for j in range(i + 1, min(i + MAX_SYLLABLE_LEN, n) + 1):
            if s[i:j] in SYLLABLES:
                ok[j] = True
        if ok[n]:
            return True
    return ok[n]


def segment(s: str) -> list[str]:
    """给出一种切分（最长音节优先），仅用于展示。"""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        for j in range(min(i + MAX_SYLLABLE_LEN, n), i, -1):
            if s[i:j] in SYLLABLES:
                out.append(s[i:j])
                i = j
                break
        else:
            out.append(s[i])
            i += 1
    return out


# --------------------------------------------------------------------------- #
# 索引构建
# --------------------------------------------------------------------------- #
def _jieba_dict_path() -> Path | None:
    """定位 jieba 的 dict.txt（找不到返回 None）。

    这里刻意 **不** ``import jieba``：jieba 源码里有一批非法的正则转义，
    光为了拿词典路径就导入它会往控制台刷一串 SyntaxWarning，还要白跑一遍
    模块初始化。``find_spec`` 只解析位置、不执行模块。
    """
    import importlib.util

    candidates: list[Path] = []
    try:
        spec = importlib.util.find_spec("jieba")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.origin:
        candidates.append(Path(spec.origin).resolve().parent / "dict.txt")
    # 也允许用户把词典单独放进项目 data/
    candidates.append(Path(__file__).resolve().parent.parent / "data" / "dict.txt")
    for path in candidates:
        if path.exists():
            return path
    return None


def _read_jieba_dict() -> list[tuple[str, int]]:
    path = _jieba_dict_path()
    if path is None:
        raise FileNotFoundError(
            "找不到中文词库 dict.txt。请先安装依赖：pip install -r requirements.txt"
        )
    words: list[tuple[str, int]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                freq = int(parts[1])
            except ValueError:
                continue
            words.append((parts[0], freq))
    return words


def build_index(
    *,
    min_freq: int = 20,
    max_word_len: int = 6,
    verbose: bool = True,
) -> dict[str, list[tuple[str, float]]]:
    """用 jieba 词库 + pypinyin 反算拼音，构建 ``拼音串 -> [(词, 分数)]`` 索引。

    返回的是**已烘死的分数**而不是词频：运行时只需把分数相加，省掉每次
    查表的 log 运算和总词频传递。分数定义见模块 docstring。
    """
    from pypinyin import Style, lazy_pinyin

    t0 = time.time()
    raw = _read_jieba_dict()
    if verbose:
        print(f"[pinyin] jieba 词库 {len(raw)} 条，开始反算拼音…")

    # 总词频要在算 log P 之前先知道，所以这里存 (词, 词频)，最后再统一转分。
    index: dict[str, list[tuple[str, int]]] = {}
    kept = 0
    for word, freq in raw:
        if freq < min_freq:
            continue
        if not (1 <= len(word) <= max_word_len):
            continue
        if not all(CJK_RE.match(ch) for ch in word):
            continue
        py = lazy_pinyin(word, style=Style.NORMAL)
        if len(py) != len(word):
            continue
        key = _canon_word_pinyin(py)
        if not key or len(key) > MAX_KEY_CHARS:
            continue
        index.setdefault(key, []).append((word, freq))
        kept += 1

    # 桶内按词频降序裁剪：既省内存，也砍掉搜索的分支
    for bucket in index.values():
        bucket.sort(key=lambda t: -t[1])
        del bucket[MAX_ENTRIES_PER_KEY:]

    total = sum(freq for bucket in index.values() for _, freq in bucket)
    log_total = math.log(max(total, 1))
    scored: dict[str, list[tuple[str, float]]] = {
        key: [
            (word, math.log(freq) - log_total + WORD_LENGTH_PRIOR * (len(word) - 1))
            for word, freq in bucket
        ]
        for key, bucket in index.items()
    }

    if verbose:
        print(
            f"[pinyin] 索引完成：{len(scored)} 个拼音键 / {kept} 个词条，"
            f"总词频 {total:,}，耗时 {time.time() - t0:.1f}s"
        )
    return scored


def _pypinyin_version() -> str:
    """拿 pypinyin 的版本号（不导入它，只看装的是哪一版）。"""
    try:
        from importlib.metadata import version

        return version("pypinyin")
    except Exception:
        return "na"


def missing_dependencies() -> list[str]:
    """这台解释器缺哪些建索引的依赖（空列表 = 齐了）。

    第 118 轮：用户拿**系统的** python 跑 ``--check``（依赖其实装在项目的
    ``.venv`` 里）时，缺依赖会让 ``_dep_signature()`` 退化成
    ``dict:na|pypinyin:na`` —— 一份好缓存因此被算成「过期」，屏幕上是
    「缓存已过期（词库或参数变了），重建索引」，把用户引去查词库版本。
    结论对（确实建不出来），理由错，所以这里单独把「缺了什么」问出来。
    """
    import importlib.util

    missing: list[str] = []
    if _jieba_dict_path() is None:
        missing.append("jieba（dict.txt 找不到）")
    try:
        has_pypinyin = importlib.util.find_spec("pypinyin") is not None
    except (ImportError, ValueError):
        has_pypinyin = False
    if not has_pypinyin:
        missing.append("pypinyin")
    return missing


def _dep_signature() -> str:
    """索引依赖的外部数据指纹：jieba 词库 + pypinyin 版本。

    第 42 轮之前 tag 里只有「版本号 + 三个参数」，于是 ``pip install -U jieba``
    （词频变了）、换一份 ``data/dict.txt``、或者 pypinyin 升级（切词规则变了）
    之后，旧缓存依旧算「命中」：引擎会继续用旧词频排候选，而 README 里的准确率
    是拿新依赖量的。把词库的路径/大小/mtime 和 pypinyin 版本一起塞进 tag，
    换个词库就自动重建。
    """
    parts: list[str] = []
    path = _jieba_dict_path()
    if path is None:
        parts.append("dict:na")
    else:
        try:
            st = path.stat()
            parts.append(f"dict:{path.name}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append(f"dict:{path.name}:stat-error")
    parts.append("pypinyin:" + _pypinyin_version())
    return "|".join(parts)


def _params_hash(min_freq: int, max_word_len: int) -> str:
    sig = (
        f"v{INDEX_VERSION}|{min_freq}|{max_word_len}|"
        f"{MAX_KEY_CHARS}|{MAX_ENTRIES_PER_KEY}|{WORD_LENGTH_PRIOR}|"
        f"{_dep_signature()}"
    )
    return hashlib.md5(sig.encode()).hexdigest()[:12]


def load_or_build_index(
    cache_path: Path,
    *,
    min_freq: int = 20,
    max_word_len: int = 6,
    verbose: bool = True,
) -> dict[str, list[tuple[str, float]]]:
    global LAST_CACHE_ERROR
    LAST_CACHE_ERROR = ""
    tag = _params_hash(min_freq, max_word_len)
    if cache_path.exists():
        try:
            with cache_path.open("rb") as fh:
                payload = pickle.load(fh)
            if payload.get("tag") == tag:
                if verbose:
                    print(
                        f"[pinyin] 命中缓存 {cache_path.name}"
                        f"（{len(payload['index'])} 键）"
                    )
                return payload["index"]
            if verbose:
                # 缺依赖时 tag 会退化成 dict:na|pypinyin:na，好缓存看起来像过期
                # 缓存 —— 说清楚是「这台解释器读不到词库」，别让用户去查词库版本（第 118 轮）。
                _missing = missing_dependencies()
                if _missing:
                    print(
                        f"[pinyin] 缺依赖 {'、'.join(_missing)} —— 这台解释器读不到词库，"
                        f"所以没法判断缓存是不是过期（不是它过期，是环境不对）；"
                        f"用项目里的解释器：.venv\\Scripts\\python.exe -m pip install -r requirements.txt"
                    )
                else:
                    print("[pinyin] 缓存已过期（词库或参数变了），重建索引")
        except Exception as exc:
            if verbose:
                print(f"[pinyin] 缓存不可用（{exc}），重建索引")

    _build_t0 = time.monotonic()
    index = build_index(min_freq=min_freq, max_word_len=max_word_len, verbose=verbose)
    build_s = time.monotonic() - _build_t0
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        with tmp.open("wb") as fh:
            pickle.dump({"tag": tag, "index": index}, fh, protocol=4)
        tmp.replace(cache_path)
        if verbose:
            print(f"[pinyin] 索引已缓存到 {cache_path}")
    except Exception as exc:
        # 「不影响使用」是假话：没落盘就等于下次启动还得再建一遍（约 2 秒）。
        LAST_CACHE_ERROR = f"{type(exc).__name__}: {exc}"
        print(
            f"[pinyin] 索引缓存写不进去（{LAST_CACHE_ERROR}）—— 这次索引只在内存里"
            f"建好（{build_s:.1f} 秒），下次启动还得再建一遍；"
            f"把 pinyin.dict_cache 指到可写的位置就能省下这段时间"
        )
    return index


# --------------------------------------------------------------------------- #
# 候选生成
# --------------------------------------------------------------------------- #
class PinyinEngine:
    """持索引，负责把输入串变成中文候选。"""

    def __init__(
        self,
        index: dict[str, list[tuple[str, float]]],
        *,
        beam_width: int = 24,
        max_candidates: int = 7,
    ):
        self.index = index
        self.beam_width = beam_width
        self.max_candidates = max_candidates
        self._cache: dict[str, list[str]] = {}
        self._user_boost: dict[str, float] = {}

    # -- 判定 ------------------------------------------------------------- #
    @staticmethod
    def looks_like_pinyin(text: str) -> bool:
        s = text.replace("'", "")
        if not s or not PINYIN_INPUT_RE.match(text):
            return False
        return can_segment(s)

    @staticmethod
    def english_like_ratio(text: str) -> float:
        """返回「按拼音能解释的比例」，仅用于调试/提示。"""
        s = text.replace("'", "")
        if not s:
            return 0.0
        n = len(s)
        ok = [0] * (n + 1)
        ok[0] = 1
        for i in range(n):
            if not ok[i]:
                continue
            for j in range(i + 1, min(i + MAX_SYLLABLE_LEN, n) + 1):
                if s[i:j] in SYLLABLES:
                    ok[j] = max(ok[j], ok[i] + (j - i))
        return ok[n] / n

    # -- 自学习 ----------------------------------------------------------- #
    def boost(self, word: str, amount: float = 1.0) -> None:
        """用户选中了某个候选 —— 下次把它往前顶。

        词频覆盖不到的词（``上传``、``登录``、网络新词）靠这个纠正，
        比去改 jieba 词库现实得多。上层可以 :meth:`user_boosts` 导出后落盘。
        """
        if not word:
            return
        self._user_boost[word] = self._user_boost.get(word, 0.0) + amount
        self._cache.clear()

    def user_boosts(self) -> dict[str, float]:
        """导出自学习权重，供上层持久化。"""
        return dict(self._user_boost)

    def load_boosts(self, boosts: dict[str, float]) -> int:
        """载入上次落盘的自学习权重，返回被丢弃的条数。

        这个文件用户能手改、也可能被别的程序写坏，所以每一项都单独转：
        转不动的跳过、好的一项照常生效，不让一个坏值把整份权重丢掉，
        更不让异常冒进启动流程（后台跑起来时那等于「双击没反应」）。
        NaN / inf 能转成 float，但会让候选排序变成 garbage，同样丢掉。
        """
        if not isinstance(boosts, dict):
            return 0
        skipped = 0
        for key, value in boosts.items():
            try:
                amount = float(value)
            except (TypeError, ValueError):
                skipped += 1
                continue
            if not math.isfinite(amount):
                skipped += 1
                continue
            self._user_boost[str(key)] = amount
        self._cache.clear()
        return skipped

    def _entry_score(self, word: str, base: float) -> float:
        boost = self._user_boost.get(word)
        return base + boost * USER_BOOST_WEIGHT if boost else base

    # -- 主入口 ----------------------------------------------------------- #
    def candidates(self, text: str) -> list[str]:
        """给出中文候选（整句级），第一个是最佳猜测。"""
        key = text.replace("'", "").lower()
        if not key:
            return []
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        result = self._search(key)
        if len(self._cache) > 4096:
            self._cache.clear()
        self._cache[key] = result
        return result

    def _search(self, s: str) -> list[str]:
        """DAG 上的 K-best Viterbi：按位置分层，每层只留最好的 K 条路径。

        为什么不用普通 beam search：本方案给每个词加的是 **负数** 分数
        （对数概率），于是「覆盖得越少、累计分反而越高」——普通 beam 会被
        只走了一两个字的短前缀占满，永远走不到句尾。按输入位置分层的 DP
        没有这个偏置：宽度 K 只影响次优候选，最优解始终可达。
        """
        n = len(s)
        k = self.beam_width
        # states[i] = [(累计得分, 词元组), ...]，表示「恰好覆盖到位置 i」的路径
        states: list[list[tuple[float, tuple[str, ...]]]] = [[] for _ in range(n + 1)]
        states[0] = [(0.0, ())]

        for i in range(n):
            cur = states[i]
            if not cur:
                continue
            # 先把这一层收敛到 K 条，再往后扩，控制状态数
            if len(cur) > k:
                cur.sort(key=lambda t: -t[0])
                del cur[k:]
            hi = min(i + MAX_KEY_CHARS, n)
            for j in range(i + 1, hi + 1):
                bucket = self.index.get(s[i:j])
                if not bucket:
                    continue
                dst = states[j]
                for word, base in bucket:
                    score = self._entry_score(word, base)
                    for acc, words in cur:
                        dst.append((acc + score, words + (word,)))

        final = states[n]
        if not final:
            return []
        final.sort(key=lambda t: -t[0])
        out: list[str] = []
        for _, words in final:
            text = "".join(words)
            if text and text not in out:
                out.append(text)
            if len(out) >= self.max_candidates:
                break
        return out
