# -*- coding: utf-8 -*-
"""翻译层：DeepSeek provider、LRU 缓存，以及离线可用的 mock provider。

设计要点
--------

* 只用标准库 ``urllib``，不额外依赖 requests。
* 对外统一是 **多候选**：:meth:`Translator.translate_many` 返回若干种译法，
  因为候选窗的「英文行」需要像中文行一样能横向挑（并排多选），
  只给一个译文的话 Tab 切过去就没得选了。
* 没配 API key 时自动降级到 :class:`MockProvider`。它不是纯粹占位 —— 内置了
  一份常用词表，能把整个交互流程离线跑通、看得见真实效果；真译不出来的部分
  才用 ``⟦mock⟧`` 标出来。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict

ZH2EN = "zh2en"
EN2ZH = "en2zh"

_SYS_ZH2EN = (
    "你是一个专业的中英翻译引擎。把用户给出的中文翻译成地道、自然的英文。"
    "如果输入是单个词或短语，给出最常用的对应译法。"
    "只输出译文本身：不要加引号、不要解释、不要音标、不要重复原文、不要换行。"
)
_SYS_EN2ZH = (
    "你是一个专业的中英翻译引擎。把用户给出的英文翻译成地道、自然的简体中文。"
    "如果输入是单个词或短语，给出最常用的对应译法。"
    "只输出译文本身：不要加引号、不要解释、不要拼音、不要重复原文、不要换行。"
)
_SYS_MANY = (
    "\n给出最多 {n} 种**不同**的译法，按常用程度从高到低排列；"
    "每行只写一种译法，不要编号、不要项目符号、不要解释。"
)

# --------------------------------------------------------------------------- #
# 内置词表：让 mock provider 在完全离线的环境下也能演示真实交互
# --------------------------------------------------------------------------- #
_GLOSS_ZH_EN: dict[str, list[str]] = {
    "你好": ["Hello", "Hi there", "Greetings"],
    "您好": ["Hello", "Good day"],
    "早上好": ["Good morning"],
    "下午好": ["Good afternoon"],
    "晚上好": ["Good evening"],
    "晚安": ["Good night"],
    "再见": ["Goodbye", "See you", "Bye"],
    "谢谢": ["Thank you", "Thanks", "Cheers"],
    "感谢": ["Thank you", "I appreciate it"],
    "不客气": ["You're welcome", "No problem"],
    "对不起": ["Sorry", "I'm sorry", "My apologies"],
    "抱歉": ["Sorry", "My apologies"],
    "没关系": ["It's fine", "Never mind", "No worries"],
    "请": ["Please"],
    "是的": ["Yes", "That's right"],
    "不是": ["No", "That's not it"],
    "可以": ["Sure", "OK", "Yes, you can"],
    "不行": ["No", "That won't work"],
    "好的": ["OK", "Alright", "Sure"],
    "我": ["I", "me"],
    "你": ["you"],
    "他": ["he", "him"],
    "她": ["she", "her"],
    "我们": ["we", "us"],
    "你们": ["you", "you all"],
    "他们": ["they", "them"],
    "爱": ["love"],
    "我爱你": ["I love you"],
    "喜欢": ["like", "be fond of"],
    "想": ["want", "think", "miss"],
    "我想吃": ["I want to eat", "I'd like to eat"],
    "吃": ["eat"],
    "喝": ["drink"],
    "去": ["go"],
    "来": ["come"],
    "看": ["look", "watch", "see"],
    "说": ["say", "speak"],
    "知道": ["know"],
    "觉得": ["feel", "think"],
    "希望": ["hope", "wish"],
    "需要": ["need"],
    "帮助": ["help", "assist"],
    "工作": ["work", "job"],
    "学习": ["study", "learn"],
    "生活": ["life"],
    "时间": ["time"],
    "今天": ["today"],
    "明天": ["tomorrow"],
    "昨天": ["yesterday"],
    "现在": ["now"],
    "星期": ["week"],
    "月": ["month"],
    "年": ["year"],
    "早上": ["morning"],
    "上午": ["morning", "before noon"],
    "下午": ["afternoon"],
    "晚上": ["evening", "night"],
    "天气": ["weather"],
    "今天天气": ["the weather today", "today's weather"],
    "怎么样": ["How is it?", "How about it?", "What do you think?"],
    "怎么样？": ["How is it?", "What do you think?"],
    "生日快乐": ["Happy birthday"],
    "新年快乐": ["Happy New Year"],
    "恭喜": ["Congratulations"],
    "世界": ["world"],
    "中国": ["China"],
    "北京": ["Beijing"],
    "城市": ["city"],
    "国家": ["country"],
    "朋友": ["friend"],
    "同学": ["classmate"],
    "老师": ["teacher"],
    "学生": ["student"],
    "家人": ["family", "family members"],
    "家庭": ["family", "household"],
    "公司": ["company", "firm"],
    "大学": ["university", "college"],
    "问题": ["problem", "question", "issue"],
    "办法": ["way", "solution", "method"],
    "因为": ["because"],
    "所以": ["so", "therefore"],
    "但是": ["but", "however"],
    "如果": ["if"],
    "还是": ["or", "still"],
    "非常": ["very", "extremely"],
    "真的": ["really", "truly"],
    "一起": ["together"],
    "已经": ["already"],
    "开始": ["start", "begin"],
    "结束": ["end", "finish"],
    "输入": ["input", "type"],
    "输入法": ["input method", "IME"],
    "翻译": ["translate", "translation"],
    "翻译器": ["translator"],
    "中文": ["Chinese"],
    "英文": ["English"],
    "语言": ["language"],
    "手机": ["phone", "mobile phone"],
    "电脑": ["computer"],
    "计算机": ["computer"],
    "网络": ["network", "the Internet"],
    "程序": ["program", "app"],
    "数据": ["data"],
    "信息": ["information", "info"],
    "服务器": ["server"],
    "客户端": ["client"],
    "用户": ["user"],
    "密码": ["password"],
    "登录": ["log in", "sign in"],
    "注册": ["register", "sign up"],
    "上传": ["upload"],
    "下载": ["download"],
    "保存": ["save"],
    "删除": ["delete", "remove"],
    "修改": ["modify", "edit"],
    "查询": ["query", "look up"],
    "调试": ["debug"],
    "测试": ["test"],
    "本地": ["local"],
    "远程": ["remote"],
    "工作量": ["workload"],
    "可能": ["maybe", "possible"],
    "重要": ["important"],
    "简单": ["simple", "easy"],
    "复杂": ["complex", "complicated"],
    "漂亮": ["beautiful", "pretty"],
    "好吃": ["delicious", "tasty"],
    "便宜": ["cheap", "inexpensive"],
    "贵": ["expensive"],
    "多少": ["how much", "how many"],
    "什么": ["what"],
    "哪里": ["where"],
    "谁": ["who"],
    "为什么": ["why"],
    "怎么": ["how"],
    "你好吗": ["How are you?"],
    "好久不见": ["Long time no see"],
    "欢迎": ["welcome"],
    "加油": ["Come on!", "You can do it!", "Keep it up"],
    "注意": ["note", "pay attention"],
    "安全": ["safe", "security"],
    "系统": ["system"],
    "软件": ["software"],
    "硬件": ["hardware"],
    "键盘": ["keyboard"],
    "鼠标": ["mouse"],
    "屏幕": ["screen"],
    "窗口": ["window"],
    "文件": ["file"],
    "目录": ["directory", "folder"],
    "代码": ["code"],
    "错误": ["error", "bug"],
    "版本": ["version"],
    "更新": ["update"],
    "功能": ["feature", "function"],
    "设置": ["settings", "configuration"],
    "选择": ["choose", "select"],
    "切换": ["switch", "toggle"],
    "快捷方式": ["shortcut"],
    "快捷键": ["hotkey", "shortcut key"],
}

_GLOSS_EN_ZH: dict[str, list[str]] = {}
for _zh, _ens in _GLOSS_ZH_EN.items():
    for _en in _ens:
        _GLOSS_EN_ZH.setdefault(_en.lower(), [])
        if _zh not in _GLOSS_EN_ZH[_en.lower()]:
            _GLOSS_EN_ZH[_en.lower()].append(_zh)

_MAX_GLOSS_LEN = max(len(k) for k in _GLOSS_ZH_EN)


class LRUCache:
    def __init__(self, capacity: int = 1024):
        self.capacity = capacity
        self._data: OrderedDict[str, list[str]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> list[str] | None:
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return list(self._data[key])

    def put(self, key: str, value: list[str]) -> None:
        with self._lock:
            self._data[key] = list(value)
            self._data.move_to_end(key)
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #
class MockProvider:
    """离线 provider：先查内置词表，查不到再做贪心逐词替换，最后才打标记。

    存在的意义是让「没配 API key / 网络不通」时整个输入法仍然可用可演示，
    而不是弹一堆错误。
    """

    name = "mock"

    def translate_many(self, text: str, direction: str, n: int = 1) -> list[str]:
        if direction == ZH2EN:
            return _mock_zh2en(text, n)
        return _mock_en2zh(text, n)

    def translate(self, text: str, direction: str) -> str:
        return self.translate_many(text, direction, 1)[0]


def _mock_zh2en(text: str, n: int) -> list[str]:
    if text in _GLOSS_ZH_EN:
        # 有几条译法就给几条 —— 不拿 "xxx (2)" 这种假变体去凑满候选行，
        # 那会让人以为存在一个其实不存在的第二种译法。
        return list(_GLOSS_ZH_EN[text])[:n]

    pieces: list[tuple[str, list[str]]] = []
    i = 0
    hit_any = False
    while i < len(text):
        for length in range(min(_MAX_GLOSS_LEN, len(text) - i), 0, -1):
            piece = text[i : i + length]
            if piece in _GLOSS_ZH_EN:
                pieces.append((piece, _GLOSS_ZH_EN[piece]))
                hit_any = True
                i += length
                break
        else:
            pieces.append((text[i], []))
            i += 1

    if not hit_any:
        # 一个词条都没命中时只给**一条**占位：以前是按 n 重复 n 遍，
        # 于是没配 API key 的用户会看到 4 个一模一样的候选 chip，
        # 像是有 4 种译法可选 —— 其实一种都没有。
        return [f"⟦mock zh→en⟧{text}"]

    out = []
    for k in range(n):
        words = []
        for _piece, alts in pieces:
            if alts:
                words.append(alts[min(k, len(alts) - 1)])
            else:
                words.append(_piece)
        out.append(" ".join(words))
    # 只去重，不补齐：真的只有 2 种译法就显示 2 个 chip。
    seen: list[str] = []
    for item in out:
        if item not in seen:
            seen.append(item)
    return seen[:n]


def _mock_en2zh(text: str, n: int) -> list[str]:
    low = text.strip().lower()
    if low in _GLOSS_EN_ZH:
        return list(_GLOSS_EN_ZH[low])[:n]

    words = text.split()
    converted: list[tuple[str, list[str]]] = []
    hit_any = False
    for w in words:
        alts = _GLOSS_EN_ZH.get(w.strip(".,!?;:").lower(), [])
        if alts:
            hit_any = True
        converted.append((w, alts))

    if not hit_any:
        # 同 _mock_zh2en：查不到就一条占位，不按 n 重复。
        return [f"⟦mock en→zh⟧{text}"]

    out = []
    for k in range(n):
        parts = [alts[min(k, len(alts) - 1)] if alts else w for w, alts in converted]
        out.append("".join(parts))
    seen = []
    for item in out:
        if item not in seen:
            seen.append(item)
    return seen[:n]


class TranslateError(RuntimeError):
    """翻译失败，并且**带上失败的种类**。

    第 117 轮加的：以前所有失败都被包成一个光秃秃的 `RuntimeError`，字符串里
    混着 `HTTP 401` 和 `<urlopen error [WinError 10061] …>`，下游（`--check`、
    面板、日志）只能把这串原文照抄给用户 —— 用户看到 `[FAIL] 翻译往返` 以为
    自己配置写错了，其实只是这台机器没网。分成四类之后，每个下游都能按种类
    说人话，而不用去猜字符串。

    kind:
      - "network" —— 连不上（没网 / DNS / 端口拒绝 / 超时 / 防火墙）。改配置修不好。
      - "http"    —— 服务器回了非 2xx（key 错 / 额度用完 / 模型名错 / 服务端 5xx）。
      - "parse"   —— 连上了、也回了 200，但回来的不是我们要的 JSON 结构。
      - "other"   —— 其它（不该常出现）。
    """

    def __init__(self, message: str, *, kind: str = "other", status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


class DeepSeekProvider:
    name = "deepseek"

    def __init__(self, cfg: dict):
        self.base_url = str(cfg.get("base_url", "https://api.deepseek.com")).rstrip("/")
        path = str(cfg.get("path", "/chat/completions"))
        self.url = self.base_url + (path if path.startswith("/") else "/" + path)
        self.model = str(cfg.get("model", "deepseek-chat"))
        self.api_key = str(cfg.get("api_key", "") or "").strip()
        self.timeout = float(cfg.get("timeout", 8.0))
        self.temperature = float(cfg.get("temperature", 0.0))

    def translate_many(self, text: str, direction: str, n: int = 1) -> list[str]:
        if direction == ZH2EN:
            system = _SYS_ZH2EN
        else:
            system = _SYS_EN2ZH
        if n > 1:
            system += _SYS_MANY.format(n=n)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "temperature": self.temperature,
            "stream": False,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
        )
        last_exc: Exception | None = None
        last_kind = "other"
        last_status: int | None = None
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8", "replace")
                obj = json.loads(body)
                msg = obj["choices"][0]["message"]["content"]
                return _split_variants(str(msg), n)
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                last_exc = RuntimeError(f"HTTP {exc.code} {exc.reason} {detail}")
                last_kind = "http"
                last_status = int(exc.code)
                if exc.code in (401, 403, 400, 402, 422):  # 认证/参数错重试没意义
                    break
            except Exception as exc:  # 网络抖动 -> 重试一次
                last_exc = exc
                # urllib 把连接阶段的失败包成 URLError（OSError 的子类），
                # 超时是 socket.timeout / TimeoutError，也都是 OSError。
                last_kind = "network" if isinstance(exc, OSError) else "parse"
            if attempt == 0:
                time.sleep(0.4)
        raise TranslateError(f"翻译请求失败：{last_exc}", kind=last_kind, status=last_status)


    def translate(self, text: str, direction: str) -> str:
        return self.translate_many(text, direction, 1)[0]


def _clean(text: str) -> str:
    text = text.strip()
    # 模型偶尔会自作主张加引号
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”‘’":
        text = text[1:-1].strip()
    return text.replace("\n", " ").strip()


def _split_variants(raw: str, n: int) -> list[str]:
    """把模型返回的多行/多句拆成候选列表。"""
    lines = [ln for ln in (_clean(l) for l in raw.splitlines()) if ln]
    # 去掉 "1. xxx" / "- xxx" / "1) xxx" 这类编号
    cleaned: list[str] = []
    for ln in lines:
        for sep in (". ", "、", ") ", "） "):
            head, found, tail = ln.partition(sep)
            if found and head.strip().isdigit() and tail.strip():
                ln = tail.strip()
                break
        ln = ln.lstrip("-•*· \t").strip()
        if ln:
            cleaned.append(ln)
    if not cleaned:
        return []
    seen: list[str] = []
    for item in cleaned:
        if item not in seen:
            seen.append(item)
    return seen[:n]


# --------------------------------------------------------------------------- #
# 门面
# --------------------------------------------------------------------------- #
class Translator:
    """带缓存的翻译门面。线程安全；用 stats 记账方便调试。"""

    def _build_provider(self, api_cfg: dict) -> None:
        """按 api.provider / api.api_key 选引擎，并把「为什么是离线」记下来。

        __init__ 和 reconfigure 共用：Web 控制板改了 key / provider 之后要能
        立刻换引擎，而不是等下次重启（第 108 轮）。
        """
        want = str(api_cfg.get("provider", "") or "").strip().lower()
        key = str(api_cfg.get("api_key", "") or "").strip()
        self.mock_reason = ""
        if want == "mock":
            self.provider: MockProvider | DeepSeekProvider = MockProvider()
            # 第 55 轮让 api.provider 真的起作用之后，「离线」有两种完全不同的
            # 原因：没配 key（被动退回），或者用户自己选了 mock（主动离线）。
            # 以前所有诊断都说成前一种，配了 key 的人会被告知「你没配 key」。
            self.mock_reason = "api.provider=mock，你主动选的离线词表"
        elif key:
            self.provider = DeepSeekProvider(api_cfg)
        else:
            self.provider = MockProvider()
            self.mock_reason = "没配 API key"

    def __init__(self, cfg: dict, verbose: bool = True):
        api_cfg = cfg.get("api", {})
        tr_cfg = cfg.get("translate", {})
        self.verbose = verbose
        self.cache = LRUCache(int(tr_cfg.get("cache_size", 1024)))
        self.max_length = int(tr_cfg.get("max_length", 280))
        self.enabled = bool(tr_cfg.get("enabled", True))
        self.variants = max(1, int(tr_cfg.get("variants", 4)))
        self._build_provider(api_cfg)
        self.lock = threading.Lock()
        self.stats = {
            "requests": 0,
            "cache_hits": 0,
            "errors": 0,
            "last_error": "",
            # 第 117 轮：把「为什么失败」也记下来，别让下游从错误字符串里猜。
            "last_error_kind": "",
            "last_error_status": None,
        }

    def reconfigure(self, cfg: dict) -> list[str]:
        """热更新 API / 翻译开关（Web 控制板保存后立刻生效，不用重启）。

        返回一句人话说明「换了什么」，让面板能显示出来。缓存必须整个丢掉：
        缓存键里只有方向和原文，不含模型 / 地址 / key —— 换了引擎还读旧缓存，
        用户会看到「改了 key，译文一模一样」。
        """
        api_cfg = cfg.get("api", {})
        tr_cfg = cfg.get("translate", {})
        old_enabled = self.enabled
        old_engine = "mock" if self.is_mock else "online"
        old_size = len(self.cache)

        self.max_length = int(tr_cfg.get("max_length", 280))
        self.enabled = bool(tr_cfg.get("enabled", True))
        self.variants = max(1, int(tr_cfg.get("variants", 4)))
        self.cache = LRUCache(int(tr_cfg.get("cache_size", 1024)))
        with self.lock:
            self.stats["errors"] = 0
            self.stats["last_error"] = ""
            self.stats["last_error_kind"] = ""
            self.stats["last_error_status"] = None
        self._build_provider(api_cfg)
        new_engine = "mock" if self.is_mock else "online"

        notes = []
        if old_enabled != self.enabled:
            notes.append(f"翻译已{'打开' if self.enabled else '关闭'}")
        if old_engine != new_engine:
            notes.append(f"引擎 {old_engine} → {new_engine}")
        elif not self.is_mock:
            notes.append("引擎参数已更新")
        if old_size:
            notes.append(f"旧缓存 {old_size} 条已清空")
        return notes

    @property
    def is_mock(self) -> bool:
        return isinstance(self.provider, MockProvider)

    @property
    def error_kind(self) -> str:
        """上一次失败的种类（network / http / parse / other）；没失败过是空串。

        第 117 轮：`--check` 拿它来区分「这台机器没网」和「你配置写错了」——
        这两种情况的修法完全相反，而以前它们长得一模一样。
        """
        return str(self.stats.get("last_error_kind") or "")

    @property
    def error_status(self) -> int | None:
        """上一次 HTTP 失败的响应码（不是 HTTP 失败就是 None）。"""
        v = self.stats.get("last_error_status")
        return int(v) if isinstance(v, int) else None

    def translate_many(self, text: str, direction: str, n: int | None = None) -> list[str]:
        """同步翻译出若干候选；失败返回空列表。"""
        n = self.variants if n is None else max(1, int(n))
        text = (text or "").strip()
        if not text or not self.enabled:
            return []
        if len(text) > self.max_length:
            text = text[: self.max_length]

        key = f"{direction}\x00{n}\x00{text}"
        hit = self.cache.get(key)
        if hit is not None:
            with self.lock:
                self.stats["cache_hits"] += 1
            return hit

        with self.lock:
            self.stats["requests"] += 1
        try:
            out = self.provider.translate_many(text, direction, n)
        except Exception as exc:
            with self.lock:
                self.stats["errors"] += 1
                self.stats["last_error"] = str(exc)
                self.stats["last_error_kind"] = str(getattr(exc, "kind", "") or "other")
                self.stats["last_error_status"] = getattr(exc, "status", None)
            if self.verbose:
                print(f"[translate] 失败：{exc}")
            return []
        out = [x for x in (out or []) if x]
        if out:
            self.cache.put(key, out)
        return out

    def translate(self, text: str, direction: str) -> str | None:
        """只取最佳译法；失败返回 None。"""
        got = self.translate_many(text, direction, 1)
        return got[0] if got else None

    def describe(self) -> str:
        if self.is_mock:
            mode = f"mock（{self.mock_reason or '没配 API key'}）"
        else:
            mode = self.provider.name
        # translate.enabled=false 时引擎一次都不会被调用，可只报 provider 会让人
        # 以为「翻译在跑，只是走了离线词表」—— 实际上它根本没开工（第 78 轮）。
        # 所以关闭状态放在最前面：这句话的主语是「翻译关着」，引擎信息只是补充。
        head = "翻译已关闭（translate.enabled=false）· " if not self.enabled else ""
        with self.lock:
            s = dict(self.stats)
        return (
            f"{head}"
            f"provider={mode} 缓存={len(self.cache)} "
            f"请求={s['requests']} 命中={s['cache_hits']} 失败={s['errors']}"
        )
