# -*- coding: utf-8 -*-
"""本地 Web 控制板：脱敏、校验、落盘、HTTP 路由，以及「保存后实例真的重读了」。

为什么值得单测：控制板是这个工具里**唯一一个能改 API key 的地方**，而且它跨了
三个进程边界（浏览器 → HTTP 服务 → 命令文件 → 常驻实例）。这条链上任何一段
撒谎，用户都会得到「面板上写着 Ctrl+Alt+F9、按下去却没反应」这种最难查的故障。
另外「key 不能原样回传给浏览器」是安全约定，不能只靠肉眼 review。

直接跑：``python tests/test_webpanel.py``
"""
import json
import shutil
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

from t_ime import daemon, webpanel  # noqa: E402
from t_ime import winapi as W  # noqa: E402
from t_ime.app import App  # noqa: E402
from t_ime.config import DEFAULTS, load  # noqa: E402

PASS = 0
FAIL = 0
PROBLEMS: list[str] = []

TMP = ROOT / ".tmp" / "test_webpanel"


def check(label: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"[PASS] {label}" + (f"  —— {extra}" if extra else ""))
    else:
        FAIL += 1
        PROBLEMS.append(label)
        print(f"[FAIL] {label}" + (f"  —— {extra}" if extra else ""))


cfg, _ = load()
saved_status = daemon.status_path(ROOT).read_bytes() if daemon.status_path(ROOT).exists() else None
daemon.clear_status(ROOT)
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
SECRET = "sk-live-SECRET-1234567890"


def _cfg_with(**over) -> dict:
    out = json.loads(json.dumps(DEFAULTS, ensure_ascii=False))
    for dotted, value in over.items():
        group, key = dotted.split(".", 1)
        out.setdefault(group, {})[key] = value
    return out


# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  1. 脱敏：api_key 不许原样回给浏览器")
print("=" * 72)

masked = webpanel.mask_config(_cfg_with(**{"api.api_key": SECRET}))
check("mask_config 把 key 换成 ********", masked["api"]["api_key"] == webpanel.MASK, masked["api"]["api_key"])
check("原配置没被改掉（深拷贝）", _cfg_with(**{"api.api_key": SECRET})["api"]["api_key"] == SECRET)
check("带 api_key_set 标记", masked["api"]["api_key_set"] is True)
hint = masked["api"]["api_key_hint"]
check("hint 只露首尾各 4 位", hint.startswith("sk-l") and hint.endswith("7890") and SECRET not in hint, hint)
check("没配 key 时 hint 为空", webpanel.mask_config(_cfg_with())["api"]["api_key_hint"] == "")
check("整个响应里搜不到完整 key", SECRET not in json.dumps(masked, ensure_ascii=False))


# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  2. 校验：坏热键 / 撞车 / 打字键 不许存进去")
print("=" * 72)

good = _cfg_with(**{"hotkeys.quit": "ctrl+alt+f9"})
_c, errs, _w = webpanel.validate_config(good)
check("合法组合键能过", errs == [], str(errs))

bad_shift = _cfg_with(**{"hotkeys.quit": "shift+q"})
_c, errs, _w = webpanel.validate_config(bad_shift)
check("shift+q 被拦（打大写字母会误触）", bool(errs) and "Shift" in errs[0], str(errs[:1]))

bad_bare = _cfg_with(**{"hotkeys.quit": "q"})
_c, errs, _w = webpanel.validate_config(bad_bare)
check("裸字母 q 被拦", bool(errs) and "修饰键" in errs[0], str(errs[:1]))

bad_syntax = _cfg_with(**{"hotkeys.quit": "ctrl+alt+"})
_c, errs, _w = webpanel.validate_config(bad_syntax)
check("残缺写法被拦", bool(errs), str(errs[:1]))

dup = _cfg_with(**{"hotkeys.quit": "ctrl+alt+l"})       # 与默认的 toggle_default 撞车
_c, errs, _w = webpanel.validate_config(dup)
check("两个动作撞同一个键被拦", bool(errs) and "同一个组合键" in errs[0], str(errs[:1]))

_c, errs, warns = webpanel.validate_config({"translate": {"enabled": "maybe"}})
check("类型错的项报出问题（不静默吞掉）", bool(errs) or bool(warns), str((errs + warns)[:1]))

_c, errs, _w = webpanel.validate_config(_cfg_with(**{"pinyin.min_freq": 0, "pinyin.max_word_len": 12}))
check("合法数值能过", errs == [], str(errs))


# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  3. api_key 的合并规则：留空 = 别动、显式清空才删")
print("=" * 72)

old = _cfg_with(**{"api.api_key": SECRET})
new = _cfg_with(**{"api.api_key": ""})
webpanel._merge_secret(new, old)
check("留空保持旧 key", new["api"]["api_key"] == SECRET, new["api"]["api_key"])

new2 = _cfg_with(**{"api.api_key": webpanel.MASK})
webpanel._merge_secret(new2, old)
check("占位符 ******** 保持旧 key", new2["api"]["api_key"] == SECRET)

new3 = _cfg_with(**{"api.api_key": "sk-new-9999999999"})
webpanel._merge_secret(new3, old)
check("填了新 key 就换成新的", new3["api"]["api_key"] == "sk-new-9999999999")

new4 = _cfg_with(**{"api.api_key": "", "api.api_key_clear": True})
webpanel._merge_secret(new4, old)
check("显式 api_key_clear 才清空", new4["api"]["api_key"] == "")
check("api_key_clear 不会写进文件", "api_key_clear" not in new4["api"])


# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  4. 保存：原子写盘 + 回执说清楚「同步了没有」")
print("=" * 72)

save_path = TMP / "web.json"
res = webpanel.save_panel_config(save_path, _cfg_with(**{"hotkeys.quit": "ctrl+alt+f9"}), ROOT)
check("保存成功", res["ok"] is True, str(res.get("errors")))
check("文件真的写出来了", save_path.exists())
on_disk = json.loads(save_path.read_text(encoding="utf-8"))
check("盘上是改后的热键", on_disk["hotkeys"]["quit"] == "ctrl+alt+f9", on_disk["hotkeys"]["quit"])
check("没有实例时如实说「下次启动生效」", res["running"] is False and res["reload_sent"] is False, res["note"])
check("没留临时文件", not list(TMP.glob("*.web-tmp")))

res_bad = webpanel.save_panel_config(save_path, _cfg_with(**{"hotkeys.quit": "q"}), ROOT)
check("坏配置不落盘", res_bad["ok"] is False and json.loads(save_path.read_text(encoding="utf-8"))["hotkeys"]["quit"] == "ctrl+alt+f9")

warn_res = webpanel.save_panel_config(save_path, _cfg_with(**{"pinyin.min_freq": 0}), ROOT)
check("要重启的项会给出提醒", any("min_freq" in w for w in warn_res["warnings"]), str(warn_res["warnings"][:1]))

# 现有文件读不懂时**不许**拿默认值覆盖它（第 108 轮实测：用户编辑器存到一半，
# 一保存就把原来那份配置连同热键一起清成了默认值）。
broken_path = TMP / "broken.json"
broken_path.write_text('{"hotkeys": {"quit": ', encoding="utf-8")
broken_bytes = broken_path.read_bytes()
res_broken = webpanel.save_panel_config(broken_path, _cfg_with(), ROOT)
check("现有文件坏了时拒绝保存", res_broken["ok"] is False, str(res_broken.get("errors", [])[:1]))
check("拒绝时点明「会覆盖掉它」", "覆盖" in " ".join(res_broken["errors"]), str(res_broken["errors"][:1]))
check("坏文件一个字节都没动", broken_path.read_bytes() == broken_bytes)

# 残缺写法（ctrl+alt+）也得在保存前拦住：解析不了 ≠ 「没有误触风险」
_c, errs, _w = webpanel.validate_config(_cfg_with(**{"hotkeys.quit": "ctrl+alt+"}))
check("残缺写法 ctrl+alt+ 被拦", bool(errs) and "缺少主键" in errs[0], str(errs[:1]))


# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  5. HTTP 路由：页面、接口、白名单、Host 防护")
print("=" * 72)

srv = webpanel.make_server(save_path, ROOT, port=0)
thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
thread.start()
BASE = srv.url.rstrip("/")


def _get(path: str, headers: dict | None = None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(path: str, obj: dict, token: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token is not False:
        headers["X-T-IME-Token"] = srv.token if token is None else token
    req = urllib.request.Request(BASE + path, data=json.dumps(obj).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


try:
    code, body = _get("/")
    page = body.decode("utf-8")
    check("GET / 返回页面", code == 200 and "translate-ime 控制板" in page, str(code))
    check("页面引用了保存接口", "/api/config" in page)
    check("页面里没有任何脚本外链（离线可用）", "<script src=" not in page and "<link" not in page)

    code, body = _get("/api/config")
    data = json.loads(body.decode("utf-8"))
    check("GET /api/config 能读", code == 200 and data["ok"] is True)
    check("接口返回的 key 是打码的", SECRET not in body.decode("utf-8"))
    check("报告文件是否存在", data["exists"] is True)

    code, res = _post("/api/config", {"config": _cfg_with(**{"hotkeys.quit": "ctrl+alt+f10"})})
    check("POST /api/config 能存", code == 200 and res["ok"] is True, str(res.get("errors")))
    check("存完盘上就是新值", json.loads(save_path.read_text(encoding="utf-8"))["hotkeys"]["quit"] == "ctrl+alt+f10")

    code, res = _post("/api/config", {"config": _cfg_with(**{"hotkeys.quit": "shift+q"})})
    check("坏配置返回 400 + 原因", code == 400 and res["ok"] is False and res["errors"], str(res.get("errors", [])[:1]))

    code, res = _post("/api/command", {"action": "format-c:"})
    check("命令白名单拦下奇怪动作", code == 400 and res["ok"] is False)

    code, res = _post("/api/command", {"action": "reload"})
    check("没有实例时 reload 返回 409（不是假装成功）", code == 409 and res["ok"] is False, str(res.get("error")))

    code, body = _get("/api/health")
    check("健康检查", code == 200 and json.loads(body.decode("utf-8"))["ok"] is True)

    code, body = _get("/api/config", headers={"Host": "evil.example.com"})
    check("Host 不是本机就拒绝（防 DNS rebinding）", code == 403, str(code))

    # ---- 第 108 轮：写操作必须带随机门票（防跨站「简单请求」）----
    # 别的站点可以用 text/plain 往 127.0.0.1 POST，浏览器不发预检、读不到响应，
    # 但动作已经执行（quit / 改 api.base_url）—— 只校验 Host 头挡不住。
    check("页面里带着门票占位符被替换掉", "__T_IME_TOKEN__" not in page and srv.token in page)
    check("页面里的 fetch 都带门票头", 'X-T-IME-Token' in page and page.count("AUTH") >= 5)

    code, res = _post("/api/config", {"config": _cfg_with()}, token=None)
    check("带门票的写请求能过", code == 200 and res["ok"] is True, str(res.get("errors")))

    code, res = _post("/api/command", {"action": "reload"}, token=False)
    check("不带门票的写请求 403", code == 403 and res["ok"] is False, str(code))
    check("403 时说明缺的是什么", "门票" in str(res.get("error")), str(res.get("error"))[:80])

    code, res = _post("/api/config", {"config": _cfg_with(**{"hotkeys.quit": "ctrl+alt+f7"})},
                      token="deadbeef" * 4)
    check("门票不对也 403", code == 403 and res["ok"] is False, str(code))

    check("request_token_ok 认大小写不敏感的门票",
          webpanel.request_token_ok({"x-t-ime-token": srv.token}, srv.token) is True)
    check("request_token_ok 拒绝空期望值",
          webpanel.request_token_ok({"X-T-IME-Token": ""}, "") is False)

    code, body = _get("/api/nope")
    check("未知路径 404", code == 404, str(code))
finally:
    srv.shutdown()
    srv.server_close()


# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  6. 实例侧：重读配置后热键真的换了（不用重启）")
print("=" * 72)

live_path = TMP / "live.json"
live_path.write_text(json.dumps(_cfg_with(**{"hotkeys.quit": "ctrl+alt+q"},
                                         **{"output.tray": False},
                                         **{"translate.enabled": False}),
                               ensure_ascii=False, indent=2), encoding="utf-8")
app = App(cfg, ROOT, verbose=False, install_hook=False, config_path=live_path)
try:
    app.core.cfg = app.cfg
    before = app.core.registered_hotkeys.get("quit")
    check("启动时注册的是初始热键", before == "Ctrl+Alt+Q", str(before))

    live_path.write_text(json.dumps(_cfg_with(**{"hotkeys.quit": "ctrl+alt+f9"},
                                             **{"output.tray": False},
                                             **{"translate.enabled": False}),
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    ok = app.reload_config(source="测试")
    check("reload_config 返回 True", ok is True)
    check("热键表换成了新组合键", app.core.registered_hotkeys.get("quit") == "Ctrl+Alt+F9",
          str(app.core.registered_hotkeys.get("quit")))
    check("旧组合键没留在表里（Ctrl+Alt+Q 不再退出）",
          (frozenset({0x11, 0x12}), 0x51) not in app.core.hotkeys)
    check("重读次数 +1", app._config_reloads == 1, str(app._config_reloads))
    st = daemon.read_status(ROOT) or {}
    check("状态文件里能看到重读次数", int(st.get("config_reloads") or 0) >= 1, str(st.get("config_reloads")))
    check("状态文件里 quit_hotkey 跟着变", st.get("quit_hotkey") == "Ctrl+Alt+F9", str(st.get("quit_hotkey")))

    # 第 115 轮：候选窗底部那格「换语言」也得跟着热键一起换 —— 它以前读配置原文，
    # 连被拒绝注册的写法都会照抄上去。
    check("底部「换语言」用的是注册后的写法",
          "Ctrl+Alt+L 换语言" in app.overlay.hint, app.overlay.hint)
    live_path.write_text(json.dumps(_cfg_with(**{"hotkeys.quit": "ctrl+alt+f9"},
                                             **{"hotkeys.toggle_default": "ctrl+alt+f10"},
                                             **{"output.tray": False},
                                             **{"translate.enabled": True}),
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    ok4 = app.reload_config(source="测试")
    check("改了 toggle_default 也能重载", ok4 is True)
    check("★ 底部提示跟着换成新键（不留旧键）",
          "Ctrl+Alt+F10 换语言" in app.overlay.hint and "Ctrl+Alt+L 换语言" not in app.overlay.hint,
          app.overlay.hint)

    # 第 111 轮：上一版配置留下的「提示」在配置换过之后可能早就不成立了 —— 最典型的是
    # 用户按提示把翻译打开、在网页上保存，重载成功了，状态里却还挂着「翻译关着……
    # 切不到译文」，面板和网页（第 105 / 110 轮都加了展示）就一起继续念那句假话。
    app.core.status = "翻译关着（translate.enabled=false）—— 切不到译文"
    app._publish_status(force=True)
    stale = daemon.read_status(ROOT) or {}
    check("先造一句「旧配置留下的提示」",
          str(stale.get("status") or "").startswith("翻译关着"), str(stale.get("status")))
    live_path.write_text(json.dumps(_cfg_with(**{"hotkeys.quit": "ctrl+alt+f9"},
                                             **{"output.tray": False},
                                             **{"translate.enabled": True}),
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    ok3 = app.reload_config(source="测试")
    check("配置（按要求）重载成功", ok3 is True)
    check("★ 重载成功后旧提示作废（不然面板继续念「切不到译文」）",
          app.core.status == "", repr(app.core.status))
    st3 = daemon.read_status(ROOT) or {}
    check("★ 状态文件里的旧提示也一起清掉", st3.get("status") is None, repr(st3.get("status")))

    # 坏文件：不许崩，也不许假装成功
    app.core.status = "这句说的是当前配置，还要留着"
    live_path.write_text('{"hotkeys": {"quit": ', encoding="utf-8")
    ok2 = app.reload_config(source="测试")
    check("坏配置返回 False", ok2 is False)
    check("坏配置留下错误原因", "读" in app._last_reload_error, app._last_reload_error)
    check("坏配置时旧提示原样保留（旧配置还在生效）",
          app.core.status == "这句说的是当前配置，还要留着", repr(app.core.status))
    check("坏配置时旧热键还在用（没有回退成默认/空）",
          app.core.registered_hotkeys.get("quit") == "Ctrl+Alt+F9", str(app.core.registered_hotkeys.get("quit")))
    st2 = daemon.read_status(ROOT) or {}
    check("状态文件报出重载失败", "读" in str(st2.get("config_reload_error") or ""), str(st2.get("config_reload_error")))
finally:
    try:
        app.shutdown()
    except Exception:
        pass


# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  7. 面板和实例用的不是同一份配置：不许宣布「已同步」（第 109 轮）")
print("=" * 72)

import types as _types  # noqa: E402

real_daemon = webpanel.daemon
panel_cfg = TMP / "panel.json"
other_cfg = TMP / "instance.json"
panel_cfg.write_text(json.dumps(_cfg_with(), ensure_ascii=False, indent=2), encoding="utf-8")
calls: list[str] = []


def _stub_daemon(inst_path) -> object:
    """把 webpanel 眼里的 daemon 换掉：实例在跑、用的是 inst_path。"""
    return _types.SimpleNamespace(
        is_running=lambda: True,
        read_status=lambda root: ({"config": str(inst_path)} if inst_path else {}),
        send_command=lambda root, action: (calls.append(action), True)[1],
    )


try:
    # 实例重读的是它自己那份 --config；面板保存的是另一份 → 改动永远不生效
    calls.clear()
    webpanel.daemon = _stub_daemon(other_cfg)
    res = webpanel.save_panel_config(panel_cfg, _cfg_with(**{"hotkeys.quit": "ctrl+alt+f10"}), ROOT)
    check("两边不是同一份时，不许说已经同步", res.get("reload_sent") is False and res.get("synced") is False,
          f'synced={res.get("synced")} reload_sent={res.get("reload_sent")}')
    check("明说实例用的是另一份", str(other_cfg) in str(res.get("note")), str(res.get("note"))[:120])
    check("warnings 里有「另一份配置」和两种改法",
          any("另一份配置" in w and "重启" in w for w in res.get("warnings") or []),
          str(res.get("warnings"))[:160])
    check("也没有往实例投 reload（投了它也只是重读旧文件）", calls == [], str(calls))
    check("文件照样写下去了（不丢用户的改动）",
          json.loads(panel_cfg.read_text(encoding="utf-8"))["hotkeys"]["quit"] == "ctrl+alt+f10")

    webpanel.daemon = _stub_daemon(panel_cfg)
    res2 = webpanel.save_panel_config(panel_cfg, _cfg_with(**{"hotkeys.quit": "ctrl+alt+f11"}), ROOT)
    check("同一份配置时照旧同步", res2.get("synced") is True and calls == ["reload"],
          f'synced={res2.get("synced")} calls={calls}')

    # 第 113 轮：只改了「要重启才生效」的项时，回执不许说「不用重启」
    calls.clear()
    sub_restart = _cfg_with(**{"pinyin.min_freq": 5000})
    res_r = webpanel.save_panel_config(panel_cfg, sub_restart, ROOT)
    check("要重启的改动：note 里明说要重启、不许出现「不用重启」",
          "重启" in str(res_r.get("note")) and "不用重启" not in str(res_r.get("note")),
          str(res_r.get("note"))[:160])
    check("要重启的改动：单独给出 restart_required（页面才列得出来）",
          any("min_freq" in s for s in res_r.get("restart_required") or []),
          str(res_r.get("restart_required"))[:160])
    check("要重启的改动：warnings 里仍留着原因（脚本调用者也能看到）",
          any("min_freq" in s for s in res_r.get("warnings") or []),
          str(res_r.get("warnings"))[:160])
    check("页面上有一块专门列「要重启才生效」", "要重启实例才生效" in page and "restart_required" in page,
          "页面缺 restart_required 渲染")
    # 反过来：只改热键时不该冒出「要重启」的清单（min_freq 跟上一次落盘的值，
    # 否则「改回默认值」本身也是要重建索引的改动）
    calls.clear()
    res_hk = webpanel.save_panel_config(
        panel_cfg, _cfg_with(**{"pinyin.min_freq": 5000, "hotkeys.quit": "ctrl+alt+f12"}), ROOT)
    check("只改热键时：没有 restart_required，note 仍是立刻生效",
          not res_hk.get("restart_required") and "不用重启" in str(res_hk.get("note")),
          f'restart={res_hk.get("restart_required")} note={str(res_hk.get("note"))[:80]}')

    # 读不到实例的配置路径（旧版本）时也不许吹牛
    calls.clear()
    webpanel.daemon = _stub_daemon(None)
    res3 = webpanel.save_panel_config(panel_cfg, _cfg_with(), ROOT)
    check("读不到实例用的是哪份时，说「没法确认」", res3.get("synced") is False and "没法确认" in str(res3.get("note")),
          str(res3.get("note"))[:120])

    # 页面要能在保存之前就看到「不同步」
    webpanel.daemon = _stub_daemon(other_cfg)
    srv2 = webpanel.make_server(panel_cfg, ROOT, port=0)
    t2 = threading.Thread(target=srv2.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t2.start()
    try:
        with urllib.request.urlopen(srv2.url.rstrip("/") + "/api/config", timeout=10) as r:
            d2 = json.loads(r.read().decode("utf-8"))
        check("GET /api/config 报 config_match False", d2.get("config_match") is False, str(d2.get("config_match")))
        check("并且告诉页面实例用的是哪份", d2.get("instance_config") == str(other_cfg), str(d2.get("instance_config")))
    finally:
        srv2.shutdown()
        srv2.server_close()
finally:
    webpanel.daemon = real_daemon


# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  8. 网页上「按一下」抓组合键：不许 500，也不许顺手触发常驻实例（第 109 轮）")
print("=" * 72)


class _FakeHook:
    def __init__(self, cb, *, ok: bool, error: str = "") -> None:
        self.cb = cb
        self._ok = ok
        self.error = error
        self.stopped = False

    def start(self, timeout: float = 5.0) -> bool:
        return self._ok

    def stop(self) -> None:
        self.stopped = True


class _FakeW:
    """只实现 capture_combo 用到的那几个 API，好把键盘交互搬进单测。"""

    def __init__(self, *, ok: bool = True, error: str = "", mods=(0x11, 0x12)) -> None:
        self.ok = ok
        self.error = error
        self.mods = set(mods)
        self.hook: _FakeHook | None = None

    def is_modifier(self, vk: int) -> bool:
        return int(vk) in (0x10, 0x11, 0x12, 0x5B, 0x5C)

    def normalize_vk(self, vk: int) -> int:
        return int(vk)

    def physical_mods(self):
        return set(self.mods)

    def combo_text(self, mods, main: int) -> str:
        return W.combo_text(mods, main)

    def KeyboardHook(self, cb):  # noqa: N802 - 模拟 winapi 的类名
        self.hook = _FakeHook(cb, ok=self.ok, error=self.error)
        return self.hook


# ① 钩子装不上：报出原因，而不是 "cannot unpack non-iterable bool object"
fake = _FakeW(ok=False, error="SetWindowsHookExW 失败，GetLastError=5（ACCESS_DENIED）")
try:
    webpanel.capture_combo(0.2, fake)
    check("钩子装不上时要报错", False, "没抛异常")
except RuntimeError as exc:
    check("钩子装不上时报的是真原因（旧代码在这里 TypeError 500）",
          "钩子装不上" in str(exc) and "ACCESS_DENIED" in str(exc), str(exc))
except Exception as exc:  # noqa: BLE001
    check("钩子装不上时报的是真原因（旧代码在这里 TypeError 500）", False, f"{type(exc).__name__}: {exc}")

# ② 抓到了：这一下必须吞掉，否则常驻实例照旧执行（按当前 quit 键会直接退出）
fake = _FakeW(ok=True)
got: dict = {}


def _capture() -> None:
    got["combo"] = webpanel.capture_combo(3.0, fake)


_cap = threading.Thread(target=_capture, daemon=True)
_cap.start()
for _ in range(40):
    if fake.hook is not None:
        break
    time.sleep(0.05)
swallow_mod = fake.hook.cb(0x11, True, False)      # 修饰键：不记、不吞
swallow_injected = fake.hook.cb(0x51, True, True)  # 注入键：不理
swallow_main = fake.hook.cb(0x51, True, False)     # Ctrl+Alt+Q：记下来并吞掉
_cap.join(5.0)
check("组合键被记下来了", got.get("combo") == "Ctrl+Alt+Q", str(got.get("combo")))
check("主键那一下被吞掉（实例不会把它当热键执行）", swallow_main is True, str(swallow_main))
check("修饰键不吞（否则 Shift/Ctrl 的按下状态会错）", swallow_mod is False, str(swallow_mod))
check("注入的按键不参与捕获", swallow_injected is False, str(swallow_injected))
check("钩子在抓完以后被停掉", bool(fake.hook and fake.hook.stopped))

page_has_detect = "/api/detect" in page
check("页面上确实有「按一下」这个入口", page_has_detect)

# 第 112 轮：第 109 轮把捕获改成「吞掉这一下」，实现和模块说明都跟着改了，**页面上的
# 提示语没改**，还写着「只观察、不吞键」—— 用户照着这句话会以为按下的键还会传给前台
# 程序（尤其担心按 Ctrl+W 会不会关掉标签页）。文案属于行为承诺，得跟行为一起钉住。
_src_panel = (ROOT / "t_ime" / "webpanel.py").read_text(encoding="utf-8")
check("★ 页面的「按一下」说明不再承诺「只观察、不吞键」",
      "不吞键" not in page and "只观察" not in page,
      page.split("请按下你想要的组合键", 1)[-1][:80])
check("★ 页面明说那一下会被接住（不传给前台、也不触发动作）",
      ("接住" in page or "吞掉" in page) and "不会触发输入法动作" in page,
      page.split("请按下你想要的组合键", 1)[-1][:80])
check("★ webpanel 模块说明也不再写「只观察、不吞键」", "只观察、不吞键" not in _src_panel)


# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  9. 实例的「拒绝理由」要摆到页面上（第 110 轮）")
print("=" * 72)

# 第 105 轮把控制台面板补上了「实例提示」，可网页这边一直在自己拼状态：
# 实例明明回了「翻译关着，切不到译文」，页面上点完按钮看起来就是「什么都没发生」。
check("页面上有专门放「实例提示」的位置", 'id="inst-note"' in page)
check("页面会显示实例说的那句话", "实例提示：" in page)
check("页面读的是状态文件里的 status 字段", "st.status" in page)

# 接口必须原样把那句话透出去（页面才有东西可显示）
srv3 = webpanel.make_server(save_path, ROOT, port=0)
thread3 = threading.Thread(target=srv3.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
thread3.start()
try:
    base3 = srv3.url.rstrip("/")
    daemon.write_status(ROOT, {
        "pid": 0, "updated": 0, "enabled": True, "hook": True,
        "active": "zh",
        "status": "翻译关着（translate.enabled=false）—— 切不到译文",
    })
    with urllib.request.urlopen(base3 + "/api/status", timeout=10) as r:
        status_payload = json.loads(r.read().decode("utf-8"))
    got_status = (status_payload.get("status") or {}).get("status")
    check("GET /api/status 把拒绝理由透出来了",
          got_status == "翻译关着（translate.enabled=false）—— 切不到译文",
          repr(got_status))
finally:
    srv3.shutdown()
    srv3.server_close()


# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  10. 配置文件整份读不懂：页面不许把默认值当成用户的配置（第 114 轮）")
print("=" * 72)

broken_cfg = TMP / "broken.json"
broken_cfg.write_text('{"hotkeys": {"quit": "ctrl+alt+q",}}', encoding="utf-8")  # 多一个逗号
srv_b = webpanel.make_server(broken_cfg, ROOT, port=0)
tb = threading.Thread(target=srv_b.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
tb.start()
try:
    with urllib.request.urlopen(srv_b.url.rstrip("/") + "/api/config", timeout=10) as r:
        db = json.loads(r.read().decode("utf-8"))
    check("接口把「文件整份读不懂」单独报出来", bool(db.get("file_error")), str(db.get("file_error"))[:140])
    check("这时 problems 是空的（默认值本身没毛病，靠它报不出来）",
          db.get("problems") == [], str(db.get("problems")))
    page_b = urllib.request.urlopen(srv_b.url.rstrip("/") + "/", timeout=10).read().decode("utf-8")
    check("页面读到 file_error 这个字段", "file_error" in page_b, "页面没读这个字段")
    check("页面明说「下面显示的是程序默认值」", "程序默认值" in page_b, "页面没说清显示的是什么")
    check("页面在坏文件时把「保存并同步」先关掉", "saveBtn.disabled = !!fileError" in page_b, "按钮没被关")
    res_b = webpanel.save_panel_config(broken_cfg, _cfg_with(), ROOT)
    check("保存仍然被拒（老行为不许退化）",
          res_b.get("ok") is False and any("读不懂" in e for e in res_b.get("errors") or []),
          str(res_b.get("errors"))[:140])
finally:
    srv_b.shutdown()
    srv_b.server_close()

good_cfg = TMP / "good.json"
good_cfg.write_text(json.dumps(_cfg_with(), ensure_ascii=False, indent=2), encoding="utf-8")
srv_g = webpanel.make_server(good_cfg, ROOT, port=0)
tg = threading.Thread(target=srv_g.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
tg.start()
try:
    with urllib.request.urlopen(srv_g.url.rstrip("/") + "/api/config", timeout=10) as r:
        dg = json.loads(r.read().decode("utf-8"))
    check("正常文件时 file_error 是空的（不许多报）", dg.get("file_error") in ("", None),
          str(dg.get("file_error"))[:120])
finally:
    srv_g.shutdown()
    srv_g.server_close()


# ---------------------------------------------------------------------- #
try:
    (ROOT / "config.json").exists()
    shutil.rmtree(TMP, ignore_errors=True)
    daemon.clear_status(ROOT)
    if saved_status is not None:
        daemon.status_path(ROOT).write_bytes(saved_status)
except OSError:
    pass

print()
print("=" * 72)
print(f"  结果：{PASS} 通过 / {FAIL} 失败")
if PROBLEMS:
    print("  失败项：")
    for item in PROBLEMS:
        print(f"    - {item}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
