# -*- coding: utf-8 -*-
"""真 HTTP 那条路：DeepSeekProvider 的请求形状、重试规则、超时、坏 JSON。

为什么单独一个文件：`tests/test_translate.py` 全程用假 provider，
`tests/` 里**没有任何一行**碰过 `DeepSeekProvider` —— 也就是说真正联网上屏
那条路（配置了 API key 之后走的就是它）此前完全没被执行过。这里在
127.0.0.1 上起一个假服务器，让 provider 真的走 urllib 发 POST、真的
收 HTTP 状态码、真的超时，只看它怎么应对。

不联网、不需要 API key、不花 token。

直接跑：``python tests/test_provider_http.py``
"""
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

from t_ime.translator import (  # noqa: E402
    EN2ZH, ZH2EN, DeepSeekProvider, Translator, _SYS_EN2ZH, _SYS_MANY, _SYS_ZH2EN,
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


def sec(title):
    print(f"\n== {title} ==")


class _Handler(BaseHTTPRequestHandler):
    """按 state['plan'] 里排好的脚本回话；每条请求都记进 state['log']。"""

    state: dict = {}
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):   # 别往控制台喷访问日志
        pass

    def do_POST(self):
        st = self.state
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        st["log"].append({
            "t": time.monotonic(),
            "method": "POST",
            "path": self.path,
            "headers": dict(self.headers),
            "body": raw.decode("utf-8", "replace"),
        })
        plan = st["plan"]
        status, body, delay = plan.pop(0) if plan else (200, ok_body("ok"), 0.0)
        if delay:
            time.sleep(delay)
        data = body.encode("utf-8") if isinstance(body, str) else body
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


def ok_body(content: str) -> str:
    return json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False)


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.daemon_threads = True
    state = {"plan": [], "log": []}
    _Handler.state = state
    th = threading.Thread(target=httpd.serve_forever, name="fake-api", daemon=True)
    th.start()
    return httpd, state, httpd.server_address[1]


def plan(state, *items):
    state["plan"][:] = list(items)


def mk_provider(port: int, **over) -> DeepSeekProvider:
    cfg = {
        "base_url": f"http://127.0.0.1:{port}",
        "path": "/v1/chat",
        "model": "deepseek-chat",
        "api_key": "sk-test-key",
        "timeout": 1.5,
        "temperature": 0.0,
    }
    cfg.update(over)
    return DeepSeekProvider(cfg)


def raises(fn) -> Exception | None:
    try:
        fn()
    except Exception as exc:   # noqa: BLE001 —— 就是要接住
        return exc
    return None


def last_payload(state) -> dict:
    return json.loads(state["log"][-1]["body"])


# --------------------------------------------------------------------------- #
# 1. 请求长什么样
# --------------------------------------------------------------------------- #
def test_request_shape(port, state):
    sec("1. 请求形状：POST、路径、鉴权头、payload 字段、system 提示随方向与条数变")
    prov = mk_provider(port)
    plan(state, (200, ok_body("Hello"), 0.0))
    out = prov.translate_many("你好", ZH2EN, 1)
    check("200 时把 content 交回来", out == ["Hello"], out)

    log = state["log"]
    check("确实发了一次 POST", len(log) == 1 and log[-1]["method"] == "POST", log)
    check("打到了配置里的路径", log[-1]["path"] == "/v1/chat", log[-1]["path"])

    headers = {k.lower(): v for k, v in log[-1]["headers"].items()}
    check("带着 Bearer 鉴权头", headers.get("authorization") == "Bearer sk-test-key",
          headers.get("authorization"))
    check("声明 JSON 请求体", headers.get("content-type") == "application/json",
          headers.get("content-type"))

    payload = last_payload(state)
    check("payload 里有模型名", payload.get("model") == "deepseek-chat", payload.get("model"))
    check("payload 里 temperature 跟配置走", payload.get("temperature") == 0.0,
          payload.get("temperature"))
    check("payload 里明确不流式", payload.get("stream") is False, payload.get("stream"))
    msgs = payload.get("messages") or []
    check("两条消息：system + user", len(msgs) == 2, msgs)
    check("第一条是 system", msgs[0].get("role") == "system", msgs[0].get("role"))
    check("第二条是 user 且就是原文", msgs[1] == {"role": "user", "content": "你好"}, msgs[1])
    check("要一条时 system 就是基础提示（没有多条要求）",
          msgs[0].get("content") == _SYS_ZH2EN, msgs[0].get("content"))

    plan(state, (200, ok_body("一\n二\n三"), 0.0))
    prov.translate_many("你好", ZH2EN, 3)
    system3 = last_payload(state)["messages"][0]["content"]
    check("要三条时 system 末尾追加了「要 n 条」的说明",
          system3 == _SYS_ZH2EN + _SYS_MANY.format(n=3), system3[:-40])

    plan(state, (200, ok_body("Hello"), 0.0))
    prov.translate_many("Hello", EN2ZH, 1)
    system_en = last_payload(state)["messages"][0]["content"]
    check("方向反过来时用的是另一套 system 提示",
          system_en == _SYS_EN2ZH and system_en != _SYS_ZH2EN, system_en[:40])


# --------------------------------------------------------------------------- #
# 2. 模型给什么，就算什么（清洗与截断）
# --------------------------------------------------------------------------- #
def test_response_cleaning(port, state):
    sec("2. 模型回的东西：多行拆开、编号剥掉、条数截断、引号剥掉、空内容")
    prov = mk_provider(port)

    plan(state, (200, ok_body("1. Hello\n2. Hi there\n- Greetings"), 0.0))
    out = prov.translate_many("你好", ZH2EN, 3)
    check("多行 + 编号被拆成三条干净候选", out == ["Hello", "Hi there", "Greetings"], out)

    plan(state, (200, ok_body("1. Hello\n2. Hi there\n3. Greetings"), 0.0))
    out2 = prov.translate_many("你好", ZH2EN, 2)
    check("要两条时只留前两条", out2 == ["Hello", "Hi there"], out2)

    plan(state, (200, ok_body('"Hello"'), 0.0))
    check("模型自作主张加的成对引号会被剥掉",
          prov.translate_many("你好", ZH2EN, 1) == ["Hello"],
          prov.translate_many("你好", ZH2EN, 1))

    plan(state, (200, ok_body(""), 0.0))
    check("content 是空串时返回空列表（不是 [\"\"]）",
          prov.translate_many("你好", ZH2EN, 1) == [], "非空")

    plan(state, (200, ok_body("Hello"), 0.0))
    check("translate() 只要一条字符串", prov.translate("你好", ZH2EN) == "Hello",
          prov.translate("你好", ZH2EN))


# --------------------------------------------------------------------------- #
# 3. 错误码：哪些重试、哪些不重试
# --------------------------------------------------------------------------- #
def test_http_errors(port, state):
    sec("3. HTTP 错误码：认证/参数错不重试，服务端错重试一次")
    prov = mk_provider(port)

    for code, why in ((401, "没认证"), (403, "被拒"), (400, "参数错"), (402, "余额不足"), (422, "无法处理")):
        plan(state, (code, '{"error":"nope"}', 0.0))
        before = len(state["log"])
        exc = raises(lambda: prov.translate_many("你好", ZH2EN, 1))
        sent = len(state["log"]) - before
        check(f"{code}（{why}）只发一次请求就放弃", sent == 1, sent)
        check(f"{code} 抛的是 RuntimeError", isinstance(exc, RuntimeError), type(exc))
        check(f"{code} 的报错里带着状态码与响应体",
              exc is not None and f"HTTP {code}" in str(exc) and "nope" in str(exc),
              str(exc)[:120] if exc else None)

    plan(state, (500, "boom", 0.0), (500, "boom", 0.0))
    before = len(state["log"])
    exc = raises(lambda: prov.translate_many("你好", ZH2EN, 1))
    sent = len(state["log"]) - before
    check("500 会重试一次（总共两次请求）", sent == 2, sent)
    check("500 两次都失败后抛 RuntimeError", isinstance(exc, RuntimeError), type(exc))
    check("500 的报错里带着「翻译请求失败」", exc is not None and "翻译请求失败" in str(exc),
          str(exc)[:120] if exc else None)

    plan(state, (200, ok_body("Hello"), 0.0))
    before = len(state["log"])
    check("500 之后重试成功了就正常返回（重试是有意义的）",
          prov.translate_many("你好", ZH2EN, 1) == ["Hello"], "")
    check("这次只发了一次（第二次就成功了）", len(state["log"]) - before == 1, "")


# --------------------------------------------------------------------------- #
# 4. 坏响应、超时、连不上
# --------------------------------------------------------------------------- #
def test_transport_problems(port, state):
    sec("4. 坏 JSON / 缺字段 / 超时 / 连不上")
    prov = mk_provider(port, timeout=1.5)

    plan(state, (200, "not json at all", 0.0), (200, "not json at all", 0.0))
    before = len(state["log"])
    exc = raises(lambda: prov.translate_many("你好", ZH2EN, 1))
    check("坏 JSON 会重试一次", len(state["log"]) - before == 2, len(state["log"]) - before)
    check("坏 JSON 最终抛 RuntimeError", isinstance(exc, RuntimeError), type(exc))
    check("坏 JSON 的报错里带着「翻译请求失败」", exc is not None and "翻译请求失败" in str(exc),
          str(exc)[:120] if exc else None)

    plan(state, (200, '{"choices": []}', 0.0), (200, '{"choices": []}', 0.0))
    before = len(state["log"])
    exc = raises(lambda: prov.translate_many("你好", ZH2EN, 1))
    check("choices 为空也重试一次", len(state["log"]) - before == 2, len(state["log"]) - before)
    check("choices 为空最终抛 RuntimeError", isinstance(exc, RuntimeError), type(exc))
    check("做大了的响应体不会让错误信息带上整个 body（截到 300 字）",
          len(str(exc)) < 1000, len(str(exc)))

    slow = (200, ok_body("太慢了"), 1.0)
    plan(state, slow, slow)
    prov_fast = mk_provider(port, timeout=0.25)
    t0 = time.monotonic()
    exc = raises(lambda: prov_fast.translate_many("你好", ZH2EN, 1))
    dt = time.monotonic() - t0
    check("超时按配置的 timeout 走（0.25 秒，没等满服务器那 1 秒）", dt < 1.6, f"{dt:.2f}s")
    check("超时也会重试一次", exc is not None and dt >= 0.4, f"{dt:.2f}s")
    check("超时抛 RuntimeError 且带「翻译请求失败」",
          isinstance(exc, RuntimeError) and "翻译请求失败" in str(exc), str(exc)[:120] if exc else None)

    # 找一个确定没人听的端口
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()
    prov_dead = mk_provider(dead_port, timeout=0.5)
    t0 = time.monotonic()
    exc = raises(lambda: prov_dead.translate_many("你好", ZH2EN, 1))
    dt = time.monotonic() - t0
    check("连不上时也会重试一次（0.4 秒的间隔）", 0.3 <= dt < 3.0, f"{dt:.2f}s")
    check("连不上最终抛 RuntimeError", isinstance(exc, RuntimeError), type(exc))


def test_retry_spacing(port, state):
    sec("5. 重试之间真的等了一下（不是原地连发）")
    prov = mk_provider(port)
    plan(state, (503, "later", 0.0), (503, "later", 0.0))
    before = len(state["log"])
    raises(lambda: prov.translate_many("你好", ZH2EN, 1))
    log = state["log"][before:]
    check("两次请求都到了", len(log) == 2, len(log))
    if len(log) == 2:
        gap = log[1]["t"] - log[0]["t"]
        check("两次请求之间隔了大约 0.4 秒", 0.30 <= gap <= 1.5, f"{gap:.2f}s")


# --------------------------------------------------------------------------- #
# 6. 接进 Translator：有 key 就走真 provider，失败不许影响打字
# --------------------------------------------------------------------------- #
def test_translator_with_real_provider(port, state):
    sec("6. 接进 Translator：有 key 才走 HTTP，失败只记账、不上抛")
    cfg = {
        "api": {
            "base_url": f"http://127.0.0.1:{port}",
            "path": "/v1/chat",
            "model": "deepseek-chat",
            "api_key": "sk-test-key",
            "timeout": 1.5,
        },
        "translate": {"variants": 4, "max_length": 280, "cache_size": 16, "enabled": True},
    }
    tr = Translator(cfg, verbose=False)
    check("配了 key 之后用的就是 DeepSeekProvider（不是 mock）",
          not tr.is_mock and tr.provider.name == "deepseek", tr.provider.name)
    check("describe() 里报的也是 deepseek", "provider=deepseek" in tr.describe(), tr.describe())

    plan(state, (200, ok_body("Hello\nHi there"), 0.0))
    out = tr.translate_many("你好", ZH2EN, 2)
    check("真 HTTP 拿到的译文进得来", out == ["Hello", "Hi there"], out)

    before = len(state["log"])
    again = tr.translate_many("你好", ZH2EN, 2)
    check("第二次同样输入走缓存、不发请求", len(state["log"]) == before, len(state["log"]) - before)
    check("缓存命中计数 +1", tr.stats["cache_hits"] == 1, tr.stats)
    check("两次拿到的内容一样", again == out, again)

    plan(state, (500, "boom", 0.0), (500, "boom", 0.0))
    got = tr.translate_many("别的话", ZH2EN, 1)
    check("API 挂了时翻译返回空列表（不是抛异常）", got == [], got)
    check("失败计数 +1", tr.stats["errors"] == 1, tr.stats)
    check("失败原因记在 stats 里", "HTTP 500" in tr.stats["last_error"], tr.stats["last_error"][:80])

    plan(state, (401, '{"error":"bad key"}', 0.0))
    got2 = tr.translate_many("第三句", ZH2EN, 1)
    check("401 一样只回空列表，不把异常抛给打字线程", got2 == [], got2)
    check("错误计数变成 2", tr.stats["errors"] == 2, tr.stats)
    check("401 的响应体也进了 last_error", "bad key" in tr.stats["last_error"],
          tr.stats["last_error"][:80])

    plan(state, (200, ok_body("Works"), 0.0))
    check("API 好了之后又能翻了", tr.translate_many("再试一句", ZH2EN, 1) == ["Works"], "")

    empty = Translator({"api": {"api_key": ""}, "translate": {"variants": 4}}, verbose=False)
    check("没有 key 时仍然是 mock（不会拿空 key 去打网络）", empty.is_mock, empty.provider.name)
    check("describe() 说清楚是 mock（未配置 API key）", "mock" in empty.describe(), empty.describe())


def main() -> int:
    httpd, state, port = start_server()
    try:
        test_request_shape(port, state)
        test_response_cleaning(port, state)
        test_http_errors(port, state)
        test_transport_problems(port, state)
        test_retry_spacing(port, state)
        test_translator_with_real_provider(port, state)
    finally:
        httpd.shutdown()
        httpd.server_close()
    print(f"\n  结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
