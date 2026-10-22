# -*- coding: utf-8 -*-
"""真实启动一次 App（含全局键盘钩子、Tk 主循环、拼音索引、退出路径）。

会真的装上全局键盘钩子、真的创建一个 Tk 悬浮窗，4 秒后自动触发退出热键路径。
期间**不要打字**，否则会被这个临时输入法接管。

直接跑：``python tests/test_app_smoke.py``
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()  # 输出被重定向时切 UTF-8；真控制台保持原样

from t_ime.app import App  # noqa: E402
from t_ime.config import load  # noqa: E402

cfg, _ = load()
# 用工作区里的缓存，别在测试里重建
app = App(cfg, ROOT, verbose=True, install_hook=True)

# 活着的时候抓一份「钩子统计」和「状态文件」——退出之后钩子线程就没了，抓不到。
snap: dict = {}
seen: dict = {}


def _grab() -> None:
    if app.hook is not None:
        snap.update(app.hook.stats())
    from t_ime import daemon
    seen.update(daemon.read_status(ROOT) or {})


app.overlay.after(1500, _grab)
app.overlay.after(4000, lambda: setattr(app.core, "quit_requested", True))
code = 0
try:
    app.start()
except Exception as exc:
    print(f"[smoke] start() 抛异常: {exc!r}")
    code = 1

print(f"[smoke] 钩子曾安装成功 = {app.hook is not None}")
print(f"[smoke] 钩子统计 = {snap}")
if app.hook is not None:
    if not (snap.get("installed") and snap.get("alive") is True):
        print("[smoke] 钩子统计不对：installed/alive 应该是 True ❌")
        code = 1
    if int(snap.get("total", 0)) != int(snap.get("real", 0)) + int(snap.get("injected", 0)):
        print("[smoke] 钩子统计账目不平 ❌")
        code = 1
    if int(snap.get("real", 0)) == 0:
        print("[smoke] 提示：这 4 秒里没收到任何真实按键（real=0），"
              "想验「真按键能不能进钩子」就趁运行时敲几个字母再跑一次。")
hk = (seen.get("hook_info") or {})
if not hk:
    print("[smoke] 状态文件里没有 hook_info —— 运行中实例没把钩子统计写出来 ❌")
    code = 1
else:
    print(f"[smoke] 状态文件里的钩子统计 = {hk}")
print(f"[smoke] 退出后 quit_requested = {app.core.quit_requested}")
print(f"[smoke] 自学习权重数 = {len(app.core.engine.user_boosts())}")
print(f"[smoke] 翻译统计 = {app.translator.describe()}")
print("[smoke] " + ("启动/退出链路正常 ✅" if app.core.quit_requested else "没走到退出 ❌"))
sys.exit(code)
