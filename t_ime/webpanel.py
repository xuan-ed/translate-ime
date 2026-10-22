"""本地 Web 控制板：在浏览器里改热键 / API key，保存后通知常驻实例热重载。

为什么单独做一个 Web 页面，而不是继续扩控制台菜单：

* 快捷键、API key、base_url 这类东西要**对着键盘**填，控制台里一行行问
  ``quit 热键（现在 ctrl+alt+q）：`` 很容易把现有值覆盖掉；
* 「按一下你想要的组合键」这种交互在终端里做不了，在网页里可以做（``/api/detect``
  装一个临时钩子，把下一个按下的组合键回填到输入框；那一下会被**吞掉**，否则
  常驻实例会顺手把它当成真热键执行 —— 第 109 轮踩过；第 112 轮把这里过时的
  「不拦按键」说法改掉）；
* 用户可能正在打字，改配置不该重启进程 —— 所以保存后往收件箱投一条
  ``reload``，实例自己重读配置、重注册热键（第 108 轮）。

安全边界（刻意做的取舍）：

* 只监听 ``127.0.0.1``，并且校验 ``Host`` 头 —— 防 DNS rebinding 把页面暴露给
  外网；
* **写操作还要带一张随机门票** ``X-T-IME-Token``（每个进程启动时生成，写进页面）。
  只有 ``Host`` 校验是不够的：别的网站可以用 ``text/plain`` 发一个「简单请求」
  往 ``127.0.0.1`` POST，浏览器根本不发预检，读不到响应但**动作已经执行**
  —— 比如替你 ``quit`` 掉输入法，或者把 ``api.base_url`` 改到它自己的服务器
  上把译文摸走。带上自定义头就一定会触发预检，而没有 CORS 头的本站会拒掉
  预检，于是跨站请求根本发不出来（第 108 轮实测踩到，旧注释还写着「同源策略
  就够挡第三方站点」—— 那句话是错的）；
* 只认 GET/POST，不做跨域（没有 CORS 头）；
* API key **不会**原样回传给浏览器：GET 只给 ``********`` 和首尾各 4 位的提示，
  POST 时留空表示「不改」，要清空得显式带上 ``"api_key_clear": true``；
* 没有登录口令 —— 它是给「这台机器上的你」用的本地工具，不是网络服务。
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import config as C
from . import daemon

DEFAULT_PORT = 8765
BIND_HOST = "127.0.0.1"
MASK = "********"
#: 控制板允许触发的动作。白名单而不是「原样转发」：收件箱里的字符串会直接
#: 进 ``App._handle_tray_action``，不该让网页写进任何别的东西。
ALLOWED_ACTIONS = (
    "quit",
    "reload",
    "toggle_enabled",
    "toggle_default",
    "toggle_direct",
    "compose",
)
#: 这些键改完要重启才生效 —— 保存后告诉用户，别让他以为没保存成功。
RESTART_KEYS = {
    "pinyin.min_freq": "拼音索引要重建（下次启动会重建，第一次会慢几秒）",
    "pinyin.max_word_len": "拼音索引要重建",
    "pinyin.dict_cache": "索引缓存路径变了，下次启动重建",
    "output.log_file": "日志文件位置变了，要重启才换文件",
    "overlay": "窗口外观（字体/配色/宽度）要重启才生效",
}


def ui_path() -> Path:
    return Path(__file__).with_name("webui.html")


TOKEN_HEADER = "X-T-IME-Token"


def request_token_ok(headers: Any, expected: str) -> bool:
    """写操作的随机门票校验（``headers`` 可以是 dict 或 http.client 的 Message）。

    用固定时间比较，避免有人靠计时猜门票；空 ``expected`` 一律拒绝（启动时
    一定生成，为空说明代码被改坏了，宁可不让写）。
    """
    if not expected:
        return False
    got = ""
    items = headers.items() if hasattr(headers, "items") else ()
    for name, value in items:
        if str(name).lower() == TOKEN_HEADER.lower():
            got = str(value or "")
            break
    return bool(got) and secrets.compare_digest(got, expected)


def mask_config(cfg: dict) -> dict:
    """把配置换成能安全发给浏览器的样子（API key 只露首尾）。"""
    out = json.loads(json.dumps(cfg, ensure_ascii=False))
    api = out.setdefault("api", {})
    key = str(api.get("api_key") or "")
    api["api_key"] = MASK if key else ""
    api["api_key_set"] = bool(key)
    api["api_key_hint"] = (key[:4] + "…" + key[-4:]) if len(key) >= 12 else ("已设置" if key else "")
    return out


def _merge_secret(new_cfg: dict, old_cfg: dict) -> None:
    """浏览器里留空的 api_key = 「别动原来那个」。"""
    new_api = new_cfg.setdefault("api", {})
    raw = str(new_api.get("api_key") or "")
    clear = bool(new_api.pop("api_key_clear", False))
    if clear:
        new_api["api_key"] = ""
        return
    if raw in ("", MASK):
        new_api["api_key"] = str((old_cfg.get("api") or {}).get("api_key") or "")
    new_api.pop("api_key_set", None)
    new_api.pop("api_key_hint", None)


def restart_notes(new_cfg: dict, old_cfg: dict) -> list[str]:
    """哪些改动要重启才生效（说得具体，别让用户以为没保存上）。"""
    notes: list[str] = []
    for dotted, why in RESTART_KEYS.items():
        if dotted == "overlay":
            if (new_cfg.get("overlay") or {}) != (old_cfg.get("overlay") or {}):
                notes.append(why)
            continue
        group, key = dotted.split(".", 1)
        if (new_cfg.get(group) or {}).get(key) != (old_cfg.get(group) or {}).get(key):
            notes.append(f"{dotted}：{why}")
    return notes


def validate_config(cfg: dict) -> tuple[dict, list[str], list[str]]:
    """校验 + 补默认值。返回 (补好的配置, 硬错误, 提醒)。

    硬错误 = 存进去实例也用不了（类型错到补不回来、热键写法不对）。
    提醒 = 能存，但用户该知道（不认识的键、要重启的项）。这里复用
    ``t_ime.config.sanitize`` 和 ``t_ime.hotkey.spec_problem``，和启动时同一套
    判据 —— 否则「面板说没问题、启动时又被拒」就成了第 83 轮那种假过。
    """
    problems = C.sanitize(cfg)
    errors = [p for p in problems if "应该" in p or "只能是" in p]
    warnings = [p for p in problems if p not in errors]

    from . import winapi as W
    from .hotkey import spec_problem

    for name, spec in (cfg.get("hotkeys") or {}).items():
        text = str(spec or "")
        if not text:
            continue
        try:
            W.parse_hotkey(text)
        except Exception as exc:
            # ``spec_problem`` 只回答「会不会误触」，解析不了的写法它返回空串
            # （那时候还没法判误触）。这里必须自己先解析一遍，否则
            # ``ctrl+alt+`` 这种残缺写法会被面板当成合法值存进去 —— 存完实例
            # 拒绝注册，面板上却写着「已保存」（第 108 轮实测）。
            errors.append(f"hotkeys.{name}：{exc}")
            continue
        why = spec_problem(text, W.parse_hotkey)
        if why:
            errors.append(f"hotkeys.{name}：{why}")

    unknown = C.unknown_keys(cfg)
    if unknown:
        warnings.append("不认识的键（会原样保留，但实例不读）：" + "、".join(unknown))
    dup = _duplicate_hotkeys(cfg)
    if dup:
        errors.append(dup)
    return cfg, errors, warnings


def _duplicate_hotkeys(cfg: dict) -> str:
    """两个动作绑同一个组合键 = 永远只有一个能生效，直接拦下来。"""
    from . import winapi as W

    seen: dict[tuple, str] = {}
    for name, spec in (cfg.get("hotkeys") or {}).items():
        text = str(spec or "")
        if not text:
            continue
        try:
            combo = W.parse_hotkey(text)
        except Exception:
            continue
        if combo in seen:
            return f"hotkeys.{name} 和 hotkeys.{seen[combo]} 是同一个组合键"
        seen[combo] = name
    return ""


def load_panel_config(cfg_path: Path) -> dict:
    cfg, _p = C.load(Path(cfg_path))
    return cfg


def _same_file(a: Path | str, b: Path | str) -> bool:
    """两个配置路径是不是同一份（Windows 上大小写不敏感，也要容忍相对/绝对混用）。"""
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except OSError:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


def instance_config_path(root_dir: Path) -> str:
    """正在跑的实例用的是哪份配置文件（没实例或读不到时返回空串）。"""
    try:
        st = daemon.read_status(Path(root_dir)) or {}
    except Exception:
        return ""
    return str(st.get("config") or "")


def save_panel_config(cfg_path: Path, submitted: dict, root_dir: Path) -> dict:
    """校验 → 落盘 → 投 reload。返回给浏览器的结果（含实例回执）。"""
    cfg_path = Path(cfg_path)
    old = load_panel_config(cfg_path) if cfg_path.exists() else dict(C.DEFAULTS)
    if cfg_path.exists() and C.LAST_LOAD_ERROR:
        # 现有文件读不懂时**不许**拿「默认值 + 这次改动」覆盖它：用户可能只是
        # 在编辑器里存到一半，覆盖下去等于把他原来那份配置删了。让他先修好
        # 或删掉那个文件（第 108 轮实测踩到）。
        return {
            "ok": False,
            "errors": [f"现有配置文件读不懂（{C.LAST_LOAD_ERROR}）：{cfg_path}。"
                       f"先把那个文件修好或删掉，再回来保存 —— 现在直接保存会覆盖掉它。"],
            "warnings": [],
        }
    new_cfg = json.loads(json.dumps(submitted, ensure_ascii=False))
    _merge_secret(new_cfg, old)
    new_cfg, errors, warnings = validate_config(new_cfg)
    # 「要重启才生效」的这些项单独留一份：它们在 warnings 里只是一种提醒，可如果
    # 这次改的**只有**这些项，那「已同步、不用重启」就是假话 —— 实例确实重读了，
    # 可重读也换不掉已经建好的索引 / 已经定下的窗口尺寸（第 113 轮实测：只改
    # pinyin.min_freq，回执写 synced=true 且「不用重启」，日志里却是「没有需要说明
    # 的变化」，用户的改动一点没生效）。
    restart = restart_notes(new_cfg, old)
    warnings.extend(restart)
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings, "restart_required": restart}
    payload = json.dumps(new_cfg, ensure_ascii=False, indent=2) + "\n"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_name(cfg_path.name + ".web-tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(cfg_path)
    running = daemon.is_running()
    inst_cfg = instance_config_path(root_dir) if running else ""
    # 实例重读的是**它自己那份** --config，不是这里保存的路径。两边指的不是同一份
    # 文件时，发 reload 只会让它重读旧文件，改动永远不生效 —— 那就不能宣布「已同步」
    # （第 109 轮实测：面板说已重读、config_reloads 也 +1，可热键一个都没变）。
    mismatch = bool(running and inst_cfg and not _same_file(inst_cfg, cfg_path))
    unknown = bool(running and not inst_cfg)
    if mismatch:
        warnings = list(warnings) + [
            f"正在跑的实例看的是另一份配置（{inst_cfg}）—— 它重读的是那一份，"
            f"这次写进 {cfg_path} 的改动不会生效。两种改法：把控制板也指向 "
            f"{inst_cfg} 再保存，或者退出实例后用 --config {cfg_path} 重启。",
        ]
        found = {
            "path": str(cfg_path),
            "instance_config": inst_cfg,
            "running": True,
            "reload_sent": False,
            "synced": False,
            "restart_required": restart,
            "note": f"已写入 {cfg_path}，但常驻实例用的是 {inst_cfg} —— 这次的改动不会生效。",
        }
        return {"ok": True, "errors": [], "warnings": warnings, **found}
    if unknown:
        warnings = list(warnings) + [
            "读不到正在跑的实例用的是哪份配置（可能是旧版本），没法确认这次改动会不会被它读到 —— "
            "如果热键没变，退出实例后重启一次。",
        ]
        found = {
            "path": str(cfg_path),
            "instance_config": "",
            "running": True,
            "reload_sent": False,
            "synced": False,
            "restart_required": restart,
            "note": "已写入；读不到实例的配置路径，没法确认同步。",
        }
        return {"ok": True, "errors": [], "warnings": warnings, **found}
    sent = daemon.send_command(root_dir, "reload") if running else False
    if restart:
        # 有要重启的项时，先说实话，再让页面把清单单独列出来。
        note = (f"已写入；其中 {len(restart)} 项要重启实例才生效（见下面的清单）"
                + ("—— 其余改动实例已经重读了。" if sent else "。"))
    else:
        note = ("已写入，并通知常驻实例立刻重读（不用重启）" if sent
                else "已写入；现在没有常驻实例，下次启动时会用它")
    return {
        "ok": True,
        "errors": [],
        "warnings": warnings,
        "path": str(cfg_path),
        "instance_config": inst_cfg,
        "running": running,
        "reload_sent": bool(sent),
        "synced": bool(sent),
        "restart_required": restart,
        "note": note,
    }


def capture_combo(timeout: float = 12.0, W=None) -> str:
    """等用户按下一个组合键，返回 ``Ctrl+Alt+Q`` 这种写法（超时返回空串）。

    两个细节都是第 109 轮真机踩出来的：

    * ``KeyboardHook.start()`` 返回的是 ``bool``（错误原因在 ``hook.error``），
      不是 ``(ok, err)`` 元组 —— 旧代码按元组解包，``/api/detect`` 每次都 500，
      「按一下」这个功能从上线起就没成功过一次；
    * 捕获到的这一下要**吞掉**（回调返回 True），否则实例自己的钩子随后照样
      处理：用户想改 ``quit``，按的正好是当前生效的 ``Ctrl+Alt+Q``，输入法当场
      就退出了。吞掉以后前台程序少收到一个按键，但这是个明确的「正在捕获」
      模式，可以接受。
    """
    if W is None:
        from . import winapi as W

    got: list[str] = []
    fired = threading.Event()

    def on_key(vk: int, is_down: bool, injected: bool) -> bool:
        if not is_down or injected:
            return False
        if W.is_modifier(int(vk)):
            return False
        try:
            main_vk = int(W.normalize_vk(int(vk)))
            mods = set(W.physical_mods())
        except Exception:
            return False
        if main_vk in mods:
            return False
        try:
            got.append(W.combo_text(mods, main_vk))
        except Exception:
            return False
        fired.set()
        return True  # 吞掉：别让常驻实例把它当成真的热键

    hook = W.KeyboardHook(on_key)
    if not bool(hook.start(timeout=3.0)):
        raise RuntimeError(f"钩子装不上：{getattr(hook, 'error', '') or '原因不明'}")
    try:
        fired.wait(timeout)
    finally:
        try:
            hook.stop()
        except Exception:
            pass
    return got[0] if got else ""


class PanelServer(ThreadingHTTPServer):
    """带配置路径的 HTTP 服务器（测试里用 ``port=0`` 拿随机端口）。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cfg_path: Path, root_dir: Path, port: int = DEFAULT_PORT,
                 host: str = BIND_HOST):
        self.cfg_path = Path(cfg_path)
        self.root_dir = Path(root_dir)
        #: 每个进程一张随机门票：写进页面，写操作必须带回来（防跨站「简单请求」）。
        self.token = secrets.token_hex(16)
        super().__init__((host, int(port)), _Handler)
        self.host = host

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/"


class _Handler(BaseHTTPRequestHandler):
    server: PanelServer
    server_version = "translate-ime-panel/1.0"

    # -- 小工具 --------------------------------------------------------- #
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # 默认实现往 stderr 打每条请求；Web 面板开着的时候这会把后台日志刷满。
        return

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host in ("127.0.0.1", "localhost", "[::1]", "::1", "")

    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self) -> None:
        try:
            body = ui_path().read_bytes()
        except OSError as exc:
            self._json({"ok": False, "error": f"找不到页面文件 webui.html：{exc}"}, 500)
            return
        # 把本进程的随机门票写进页面：页面里的 fetch 带着它，别的站点带不上
        # 自定义头（会触发预检，本站没有 CORS 头 → 预检被拒，请求根本发不出去）。
        body = body.replace(b"__T_IME_TOKEN__", self.server.token.encode("ascii"))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 4 * 1024 * 1024))
        return json.loads(raw.decode("utf-8"))

    # -- 路由 ----------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json({"ok": False, "error": "只接受本机访问"}, 403)
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._html()
        elif path == "/api/config":
            self._api_config()
        elif path == "/api/status":
            self._api_status()
        elif path == "/api/detect":
            self._api_detect()
        elif path == "/api/health":
            self._json({"ok": True, "app": "translate-ime-panel"})
        else:
            self._json({"ok": False, "error": "没有这个地址"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json({"ok": False, "error": "只接受本机访问"}, 403)
            return
        if not request_token_ok(self.headers, self.server.token):
            # 浏览器自己打开的页面会带上门票；别的站点发不出带自定义头的请求。
            self._json({"ok": False,
                        "error": "缺少控制板门票（X-T-IME-Token）—— 刷新页面再试；"
                                 "如果是脚本在调，从页面里的 meta[name=csrf-token] 取。"}, 403)
            return
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except Exception as exc:
            self._json({"ok": False, "error": f"请求体不是合法 JSON：{exc}"}, 400)
            return
        if path == "/api/config":
            cfg = body.get("config") if isinstance(body.get("config"), dict) else body
            try:
                result = save_panel_config(self.server.cfg_path, cfg, self.server.root_dir)
            except Exception as exc:
                self._json({"ok": False, "errors": [f"写入失败：{exc}"]}, 500)
                return
            self._json(result, 200 if result.get("ok") else 400)
        elif path == "/api/command":
            action = str(body.get("action") or "")
            if action not in ALLOWED_ACTIONS:
                self._json({"ok": False, "error": f"不认识的动作：{action}"}, 400)
                return
            if not daemon.is_running():
                self._json({"ok": False, "error": "现在没有常驻实例在跑"}, 409)
                return
            ok = daemon.send_command(self.server.root_dir, action)
            self._json({"ok": bool(ok), "action": action})
        else:
            self._json({"ok": False, "error": "没有这个地址"}, 404)

    # -- 具体接口 ------------------------------------------------------- #
    def _api_config(self) -> None:
        path = self.server.cfg_path
        try:
            cfg = load_panel_config(path)
            problems = C.LAST_PROBLEMS
            unknown = C.LAST_UNKNOWN
            retired = C.LAST_RETIRED
            # 文件整份读不懂时，load_panel_config() 是「警告 + 改用内置默认值」，
            # 上面那三个列表都是干净的 —— 于是页面会把默认值当成用户配置摆出来，
            # 一句提示都没有（第 114 轮实测）。这个字段就是那一句。（保存那边早就
            # 会拒绝覆盖坏文件，问题只在于用户被蒙着看到了一份假的「当前配置」。）
            file_error = str(getattr(C, "LAST_LOAD_ERROR", "") or "")
        except Exception as exc:
            self._json({"ok": False, "error": f"读配置失败：{exc}", "path": str(path)}, 500)
            return
        inst_cfg = instance_config_path(self.server.root_dir)
        running = daemon.is_running()
        self._json({
            "ok": True,
            "path": str(path),
            "exists": path.exists(),
            "config": mask_config(cfg),
            "defaults": mask_config(dict(C.DEFAULTS)),
            "problems": list(problems),
            "unknown": list(unknown),
            "retired": list(retired),
            "file_error": file_error,
            "status": daemon.read_status(self.server.root_dir),
            "running": running,
            "instance_config": inst_cfg,
            # 面板读的是 --config 给的那份；实例重读的是它自己那份。两边不一致时
            # 「保存 → 同步」是不可能的，页面必须先告诉用户（第 109 轮）。
            "config_match": bool(not running or (inst_cfg and _same_file(inst_cfg, path))),
            "port": self.server.server_address[1],
        })

    def _api_status(self) -> None:
        self._json({
            "ok": True,
            "running": daemon.is_running(),
            "status": daemon.read_status(self.server.root_dir),
        })

    def _api_detect(self) -> None:
        try:
            combo = capture_combo(12.0)
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 500)
            return
        if not combo:
            self._json({"ok": False, "error": "12 秒内没等到按键"}, 408)
            return
        self._json({"ok": True, "combo": combo})


def make_server(cfg_path: Path, root_dir: Path, port: int = DEFAULT_PORT,
                host: str = BIND_HOST) -> PanelServer:
    return PanelServer(cfg_path, root_dir, port=port, host=host)


def serve(cfg_path: Path, root_dir: Path, port: int = DEFAULT_PORT,
          open_browser: bool = True, verbose: bool = True) -> int:
    """起服务并阻塞。端口被占时自动往上找一个能用的（最多 20 个）。"""
    last_err = ""
    for candidate in range(int(port), int(port) + 20):
        try:
            server = make_server(cfg_path, root_dir, port=candidate)
        except OSError as exc:
            last_err = str(exc)
            continue
        url = server.url
        print()
        print("== Web 控制板 ==")
        print(f"  地址：{url}")
        print(f"  配置：{Path(cfg_path).resolve()}")
        print("  改完点「保存」→ 常驻实例会立刻重读（不用重启）；按 Ctrl+C 关掉这个页面服务。")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            print("\n[web] 收到 Ctrl+C，关掉控制板。")
        finally:
            server.server_close()
        return 0
    print(f"[web] 端口 {port}–{int(port) + 19} 都被占了：{last_err}", flush=True)
    return 1
