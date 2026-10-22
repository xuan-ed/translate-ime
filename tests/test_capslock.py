# -*- coding: utf-8 -*-
"""CapsLock 开着的时候，敲 a 必须打出 A，而不是「啊」。

用户反馈的第一条就是「开着 IME 打不出大写英文」。真凶：``winapi.py`` 里声明了
``VK_CAPITAL = 0x14``，而 ``core.py`` 从头到尾**一次都没读过它** —— 开着 CapsLock
敲字母，钩子照样把键吞掉、喂进拼音缓冲。

这个文件盯四件事：

  1. **事件流是权威**：钩子看得见 VK_CAPITAL 的按下，那就以它为准；系统那份
     （``GetKeyState``）**不是随时都准**（锁屏时必然是假 0；别人刚改过状态、而我们
     还没处理过键盘消息时可能读到旧值），要是每次判断都拿它覆盖，用户刚按下的
     CapsLock 会被当场抹掉（探针里抓到过）；
  2. 系统那份只在第一次判断 / 重装钩子时用来**对表**（``sync_caps``）；
  3. 大小写是 Shift 与 CapsLock 的**异或**：开 CapsLock 再按 Shift = 小写；
  4. 别的键不受影响（缓冲、标点、编辑键的老行为一个都不能坏）。

用法：``python tests/test_capslock.py``（有实例在跑时跳过面板那一节）
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime import daemon  # noqa: E402
from t_ime import winapi as W  # noqa: E402
from t_ime.app import App  # noqa: E402
from t_ime.config import load as load_config  # noqa: E402
from t_ime.console import fix_console_encoding  # noqa: E402
from t_ime.core import IMECore  # noqa: E402
from t_ime.pinyin import PinyinEngine, load_or_build_index  # noqa: E402
from t_ime.translator import Translator  # noqa: E402

fix_console_encoding()

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  —— {detail}" if detail else ""))
    return bool(ok)


def skip(label: str, why: str) -> None:
    results.append((True, label))
    print(f"  [SKIP] {label}  —— {why}")


def release_mutex(name: str = "translate-ime") -> bool:
    """放掉本进程占着的单实例锁（面板那一节借过它）。"""
    handle = W._MUTEX_HANDLES.pop(name, None)          # noqa: SLF001 —— 测试里就是要放锁
    if not handle:
        return False
    return bool(W.kernel32.CloseHandle(ctypes.c_void_p(handle)))


print("=" * 74)

cfg, _cfg_path = load_config()
index = load_or_build_index(PROJ / "pinyin_index.pkl", min_freq=20,
                            max_word_len=6, verbose=False)
eng = PinyinEngine(index, beam_width=24, max_candidates=7)
tr = Translator(cfg, verbose=False)


def build(**kw):
    commits: list[str] = []
    core = IMECore(cfg, eng, tr, verbose=False,
                   emit=lambda ev: None,
                   committer=lambda text: (commits.append(text), True)[1],
                   mods_source=lambda: frozenset(), **kw)
    return core, commits


def tap(core, vk: int) -> None:
    core.handle_key(vk, True, False)
    core.handle_key(vk, False, False)


# ---------------------------------------------------------------------- #
# 1. winapi 那一层：接口在、类型对、读不到就返回 None（不许抛）
# ---------------------------------------------------------------------- #
print("\n-- 1. winapi.caps_lock_on 的形状 --")
check(W.VK_CAPITAL == 0x14, "VK_CAPITAL 还是 0x14", f"{W.VK_CAPITAL:#x}")
check(list(W.user32.GetKeyState.argtypes or []) == [ctypes.c_int],
      "GetKeyState 的 argtypes 声明成 c_int（64 位下不传错参数）",
      str(W.user32.GetKeyState.argtypes))
check(W.user32.GetKeyState.restype is ctypes.c_short,
      "GetKeyState 的 restype 是 short（高八位是按下、低八位是切换态）",
      str(W.user32.GetKeyState.restype))
_live = W.caps_lock_on()
check(_live is None or isinstance(_live, bool),
      "caps_lock_on() 只会给出 True/False/None，绝不抛异常", repr(_live))
raw_caps = int(W.user32.GetKeyState(W.VK_CAPITAL))
check(isinstance(raw_caps, int), "原始 GetKeyState 读得出整数",
      f"raw={raw_caps}（低八位就是开关状态：0=关、1=开）")
_live_again = [W.caps_lock_on() for _ in range(5)]
check(len(set(_live_again)) == 1,
      "连问 5 次答案一致（读的是开关状态，不是一次性的闩）", f"{_live_again}")
check(_live is None or _live == bool(raw_caps & 0x01),
      "读得到的时候取的就是 GetKeyState 低八位（真机探针验证过只有这一位准；"
      "GetAsyncKeyState 那位是「自上次调用以来按下过」，连问会掉成 0）",
      f"caps_lock_on()={_live} raw&1={raw_caps & 0x01}")

# ---------------------------------------------------------------------- #
# 2. 事件流是权威：系统说「关着」，但用户按过 CapsLock
# ---------------------------------------------------------------------- #
print("\n-- 2. 事件流：系统那份读不到（本会话的常态）也要能打出大写 --")
core, commits = build(caps_source=lambda: False)
tap(core, W.VK_CAPITAL)
check(core.caps_from_events == 1, "钩子看见了一次 CapsLock 按下", f"{core.caps_from_events}")
check(core.caps_lock() is True, "于是我们记的 CapsLock 是「开」", f"_caps={core._caps}")
tap(core, 0x41)
check(commits == ["A"] and core.buffer == "",
      "再敲 a → 直接上屏大写 A，没进拼音缓冲",
      f"上屏={commits!r} 缓冲={core.buffer!r}")
check(core._caps_inited is True,
      "事件之后不再去问系统（否则系统那句 0 会把记录抹掉）")

print("\n-- 2b. 反向：再按一次 CapsLock 就回到拼音 --")
tap(core, W.VK_CAPITAL)
tap(core, 0x41)
check(core.caps_from_events == 2 and core.caps_lock() is False,
      "第二次按下把它切回「关」", f"events={core.caps_from_events} caps={core.caps_lock()}")
check(commits == ["A"] and core.buffer == "a",
      "敲 a 又进拼音缓冲了（老行为没坏）",
      f"上屏={commits!r} 缓冲={core.buffer!r}")

# ---------------------------------------------------------------------- #
# 3. 系统那份只在第一次 / 重装时对表，不能覆盖事件流
# ---------------------------------------------------------------------- #
print("\n-- 3. 系统那份的权限：只能对表，不能覆盖 --")
core, commits = build(caps_source=lambda: True)
check(core.caps_lock() is True and core.caps_from_system == 1,
      "第一次判断时对了一次表（系统说开着）",
      f"caps={core.caps_lock()} from_system={core.caps_from_system}")
tap(core, W.VK_CAPITAL)          # 事件说：关了
check(core.caps_lock() is False, "事件流推翻系统（用户刚按过，以他为准）",
      f"caps={core.caps_lock()}")
check(core.caps_from_system == 1,
      "而且没有再去问系统（问一次就被抹一次）", f"from_system={core.caps_from_system}")
tap(core, 0x41)
check(core.buffer == "a" and commits == [],
      "敲 a 进拼音缓冲 —— 系统那句「开着」没能把它变成大写 A",
      f"缓冲={core.buffer!r} 上屏={commits!r}")
core.sync_caps()                 # 模拟重装钩子时的对表
check(core.caps_lock() is True and core.caps_from_system == 2,
      "重装钩子时对表生效（那段时间可能漏掉按下）",
      f"caps={core.caps_lock()} from_system={core.caps_from_system}")

# ---------------------------------------------------------------------- #
# 4. 大小写 = Shift 与 CapsLock 的异或
# ---------------------------------------------------------------------- #
print("\n-- 4. Shift 与 CapsLock 的异或表 --")


def press_letter(caps: bool, shift: bool) -> tuple[list[str], str]:
    core, commits = build(caps_source=lambda: caps)
    if shift:
        core.handle_key(W.VK_SHIFT, True, False)
    tap(core, 0x41)
    if shift:
        core.handle_key(W.VK_SHIFT, False, False)
    return commits, core.buffer


_c, _b = press_letter(False, False)
check(_c == [] and _b == "a", "CapsLock 关 + 没按 Shift → 进拼音缓冲", f"{_c!r}/{_b!r}")
_c, _b = press_letter(False, True)
check(_c == ["A"] and _b == "", "CapsLock 关 + 按着 Shift → 大写 A 上屏", f"{_c!r}/{_b!r}")
_c, _b = press_letter(True, False)
check(_c == ["A"] and _b == "", "CapsLock 开 + 没按 Shift → 大写 A 上屏", f"{_c!r}/{_b!r}")
_c, _b = press_letter(True, True)
check(_c == ["a"] and _b == "", "CapsLock 开 + 按着 Shift → 小写 a（异或）", f"{_c!r}/{_b!r}")

# ---------------------------------------------------------------------- #
# 5. 系统那份读不到（None）→ 退回事件流记的那一份
# ---------------------------------------------------------------------- #
print("\n-- 5. 读不到就退回事件流（不许猜） --")
core, commits = build(caps_source=lambda: None)
check(core.sync_caps() is None and core.caps_fallbacks == 1,
      "sync_caps() 如实返回 None 并记账",
      f"fallbacks={core.caps_fallbacks}")
check(core.caps_lock() is False, "读不到就当「关」（保持老行为）")
tap(core, W.VK_CAPITAL)
tap(core, 0x41)
check(commits == ["A"], "事件流记下的 CapsLock 照样能打出大写 A", f"{commits!r}")
core2, _ = build(caps_source=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
check(core2.sync_caps() is None and core2.caps_fallbacks == 1,
      "caps_source 抛异常也当「读不到」，不许把打字搞崩")
check(core2.caps_lock() is False, "异常之后仍然给出一个确定的答案")

# ---------------------------------------------------------------------- #
# 6. 缓冲里已经有拼音时，大写要先落缓冲
# ---------------------------------------------------------------------- #
print("\n-- 6. 先落缓冲，再上屏大写（顺序不能乱） --")
core, commits = build(caps_source=lambda: False)
tap(core, 0x4E)   # n
tap(core, 0x49)   # i
check(core.buffer == "ni", "n i 进了缓冲", repr(core.buffer))
tap(core, W.VK_CAPITAL)   # 用户按下 CapsLock → 之后是大写
tap(core, 0x41)   # A
check(commits == ["你", "A"] and core.buffer == "",
      "敲大写 A → 先把「你」上屏，再上屏 A",
      f"上屏={commits!r} 缓冲={core.buffer!r}")

# ---------------------------------------------------------------------- #
# 7. 别的键的老行为一个都不能坏
# ---------------------------------------------------------------------- #
print("\n-- 7. CapsLock 开着时，编辑键/标点照旧 --")
core, commits = build(caps_source=lambda: False)
tap(core, 0x4E)
tap(core, 0x49)
tap(core, W.VK_CAPITAL)
tap(core, W.VK_BACK)
check(core.buffer == "n", "退格还是删一个字符", repr(core.buffer))
tap(core, W.VK_SPACE)
check(commits == ["n"] and core.buffer == "",
      "空格把剩下的「n」上屏（单字母不是拼音，原样落盘）",
      f"上屏={commits!r} 缓冲={core.buffer!r}")
core, commits = build(caps_source=lambda: False)
tap(core, 0x4E)
tap(core, 0x49)
tap(core, W.VK_CAPITAL)
core.handle_key(W.VK_SHIFT, True, False)
_core_ret = core.handle_key(0x31, True, False)     # Shift+1 → 「!」透传给系统
core.handle_key(W.VK_SHIFT, False, False)
check(_core_ret is False, "Shift+数字 仍然透传（系统快捷键/符号不受影响）")
check(commits == ["你"], "但挂起的候选先落了盘", f"上屏={commits!r}")
core, _ = build(caps_source=lambda: False)
tap(core, W.VK_CAPITAL)   # 事件切到「开」
snap = core.snapshot()
check(isinstance(snap, dict) and snap.get("caps") is True,
      "snapshot() 里带着 caps 字段（跟着事件走）", repr(snap.get("caps")))

# ---------------------------------------------------------------------- #
# 8. App 接线：caps 进状态文件、变了立刻算「状态变了」
# ---------------------------------------------------------------------- #
print("\n-- 8. App 把 caps 写进状态文件，并让它参与变更检测 --")
status_p = daemon.status_path(PROJ)
ctl_p = daemon.ctl_path(PROJ)
backup: dict[Path, bytes | None] = {}
for p in (status_p, ctl_p):
    try:
        backup[p] = p.read_bytes()
    except OSError:
        backup[p] = None

app = None
try:
    daemon.take_commands(PROJ)
    app = App(cfg, PROJ, verbose=False, install_hook=False)
    app.core._caps_inited = True          # noqa: SLF001 —— 不碰真系统，直接摆状态
    app.core._caps = False                # noqa: SLF001
    app._publish_status(force=True)
    st_off = daemon.read_status(PROJ) or {}
    sig_off = str(app._status_sig)
    check(st_off.get("caps") is False, "CapsLock 关着时状态文件里 caps=False",
          repr(st_off.get("caps")))
    app.core._caps = True                 # noqa: SLF001
    app._publish_status()
    st_on = daemon.read_status(PROJ) or {}
    check(st_on.get("caps") is True, "开着时 caps=True（面板就靠它提示）",
          repr(st_on.get("caps")))
    check("caps" in str(app._status_sig) and str(app._status_sig) != sig_off,
          "caps 参与变更检测 —— 用户一切换就立刻重写状态文件")
finally:
    if app is not None:
        try:
            app.overlay.destroy()
        except Exception:
            pass
    for p, data in backup.items():
        try:
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(data)
        except OSError:
            pass
    daemon.discard_commands(PROJ)

# ---------------------------------------------------------------------- #
# 8b. 启动那次对表不许藏在 `if self.install_hook:` 里面
#
# 真机踩到的：CapsLock 明明开着，用 `--no-hook` 起常驻，状态文件却写 caps=False ——
# 因为 sync_caps() 当时在 if 里面，`--no-hook` 压根没调，默认的 False 就当了谎话。
# 这里是形状检查（源码层面）：start() 里那一次调用必须落在方法体这一层。
# ---------------------------------------------------------------------- #
print("\n-- 8b. start() 的启动对表不在 --no-hook 之外 --")
src_lines = (PROJ / "t_ime" / "app.py").read_text(encoding="utf-8").splitlines()
start_i = next(i for i, ln in enumerate(src_lines)
               if ln.startswith("    def start("))
body: list[tuple[int, str]] = []
for n, ln in enumerate(src_lines[start_i + 1:], start=start_i + 2):
    # 到下一个方法（`    def ...`）或下一个顶层声明就停 —— 别把别的方法里的
    # sync_caps()（`_reinstall_hook` 那次重装钩子的对表）也数进来。
    if ln.startswith("    def ") or (ln and not ln[0].isspace()):
        break
    body.append((n, ln))
sync_lines = [(n, ln) for n, ln in body if "self.core.sync_caps()" in ln]
check(len(sync_lines) == 1, "start() 里正好调用一次 sync_caps()（起来就跟系统对表）",
      str([n for n, _ in sync_lines]))
if sync_lines:
    n, ln = sync_lines[0]
    indent = len(ln) - len(ln.lstrip())
    check(indent == 8, "那次对表在方法体这一层 —— 不在 `if self.install_hook:` 里面"
                       "（否则 --no-hook 起常驻会谎报「大写锁定 关」）",
          f"app.py:{n} 缩进 {indent} 空格")
    check("--no-hook" in "\n".join(ln2 for _, ln2 in body),
          "注释里写明了为什么必须放在 if 外面（--no-hook）")

# ---------------------------------------------------------------------- #
# 9. 面板那一行（借单实例锁让 main.py 走「已经在跑」的分支）
# ---------------------------------------------------------------------- #
print("\n-- 9. 控制面板会明说「大写锁定 开」 --")
if W.instance_exists("translate-ime"):
    skip("控制面板的「大写锁定」提示行", "已经有实例在跑，不打扰它")
else:
    held = W.single_instance("translate-ime")
    check(bool(held) or W.instance_exists(), "本进程拿到了单实例互斥体（面板分支才会走）")
    fake = {
        "pid": os.getpid(), "enabled": True, "direct": False, "active": "zh",
        "caps": True, "tick_errors": 0, "hook_reinstalls": 0, "commit_failures": 0,
        "overlay": {"shows": 1, "rect": [0, 0, 240, 60], "on_monitor": True},
        "hook": {"installed": True, "alive": True},
        "hook_info": {"total": 0, "real": 0, "foreign_injected": 0},
    }
    daemon.write_status(PROJ, fake)
    try:
        proc = subprocess.run([sys.executable, "main.py"], cwd=str(PROJ),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=90)
        out = (proc.stdout or "") + (proc.stderr or "")
        check("大写锁定" in out and "开" in out,
              "面板打印了「大写锁定 开」那一行",
              next((ln.strip() for ln in out.splitlines() if "大写锁定" in ln), "（没找到）"))
        check("不弹候选" in out or "CapsLock" in out,
              "并且说清楚它会怎么影响打字")
    finally:
        check(release_mutex(), "交还单实例锁")
        try:
            daemon.status_path(PROJ).unlink(missing_ok=True)
        except OSError:
            pass

print()
print("=" * 74)
passed = sum(1 for ok, _ in results if ok)
print(f"结果：{passed}/{len(results)} 通过")
for ok, label in results:
    if not ok:
        print(f"  未通过：{label}")
print("=" * 74)
sys.exit(0 if passed == len(results) else 1)
