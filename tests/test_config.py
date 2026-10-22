# -*- coding: utf-8 -*-
"""配置层的回归测试：模板不漂移、深合并、来源标注、拼错的键、BOM。

这一层以前一条测试都没有，而它正好是用户唯一会亲手改的地方。三个真 bug
都是在这里被照出来的：

1. ``config.json`` 存成**带 BOM 的 UTF-8**（PowerShell 的
   ``Out-File -Encoding utf8``、某些编辑器）时，``json.loads`` 直接抛，
   于是**整份配置被忽略**、全部退回默认值 —— 用户看到的是「我改了配置却
   没反应」。现在统一用 ``utf-8-sig`` 读。
2. 键名拼错（``hotkey`` 写成单数）永远不报错，只是静静失效。
   ``unknown_keys()`` 把它揪出来。
3. ``--show-config`` 早期版本按「名字里含 key」打码，结果连
   ``hotkeys.compose`` 都被打码 —— 只看**最后一段**名字才对。

直接跑：``python tests/test_config.py``
不改仓库里的 config.json / config.example.json，全部写在临时目录里。
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

import main as main_mod  # noqa: E402
from t_ime import config as C  # noqa: E402

PASS = 0
FAIL = 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"[PASS] {name}" + (f"  —— {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"[FAIL] {name}" + (f"  —— {extra}" if extra else ""))


TMP = Path(tempfile.mkdtemp(prefix="t-ime-config-"))
# 收工把临时目录删掉：以前这里只建不删，跑一次回归就在仓库根目录留一个
# t-ime-config-xxxx/（实测攒了 22 个）。atexit 而不是结尾手写一行，是为了
# 中途断言失败 sys.exit(1) 时也能清干净。
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
ENV_NAMES = ("DEEPSEEK_API_KEY", "T_IME_API_KEY")
SAVED_ENV = {k: os.environ.get(k) for k in ENV_NAMES}


def clear_env():
    for name in ENV_NAMES:
        os.environ.pop(name, None)


def restore_env():
    for name, val in SAVED_ENV.items():
        if val is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = val


# ---------------------------------------------------------------------- #
print("=" * 72)
print("  1. config.example.json 不许和 DEFAULTS 漂移")
print("=" * 72)

example_text = C.EXAMPLE_PATH.read_text(encoding="utf-8-sig")
try:
    example = json.loads(example_text)
except Exception as exc:
    example = None
    check("config.example.json 能解析", False, repr(exc))
else:
    check("config.example.json 能解析", True)

check("config.example.json == DEFAULTS（不多不少不改值）",
      example == C.DEFAULTS,
      "" if example == C.DEFAULTS else
      f"缺 {len(C.missing_keys(example or {}))} 个 / 多 {len(C.unknown_keys(example or {}))} 个")

rebuilt = C.write_example(TMP / "rebuilt.example.json")
check("write_example() 能原样重建入库的模板（逐字节）",
      rebuilt.read_text(encoding="utf-8-sig") == example_text,
      "跑 `python -c \"from t_ime.config import write_example; write_example()\"` 可以修好")

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  2. 摊平 / 缺键 / 拼错的键")
print("=" * 72)

flat = C.flat_keys({"a": {"b": 1, "c": {"d": 2}}, "e": 3})
check("flat_keys 摊成点分名", flat == {"a.b": 1, "a.c.d": 2, "e": 3}, repr(flat))
check("flat_keys 遇到空 dict 不炸", C.flat_keys({}) == {})
check("flat_keys 中间层不是 dict 就按叶子算", C.flat_keys({"a": 1}) == {"a": 1})

all_keys = C.flat_keys(C.DEFAULTS)
check("missing_keys({}) == 全部选项", C.missing_keys({}) == sorted(all_keys),
      f"{len(all_keys)} 个选项")
check("missing_keys(DEFAULTS) == []（模板没缺东西）", C.missing_keys(C.DEFAULTS) == [])
check("missing_keys 只认真正缺的那些",
      C.missing_keys({"output": {"start_enabled": False}}) ==
      sorted(set(all_keys) - {"output.start_enabled"}))
check("unknown_keys({}) == []", C.unknown_keys({}) == [])
check("unknown_keys 抓顶层写错", C.unknown_keys({"hotkey": {}}) == ["hotkey"])
check("unknown_keys 抓嵌套写错", C.unknown_keys({"output": {"tray_icon": True}}) == ["output.tray_icon"])
check("unknown_keys 抓「本该是 dict 却写成标量」",
      C.unknown_keys({"output": True}) == ["output"])
check("unknown_keys 只报最具体的一层（不连父段一起报）",
      C.unknown_keys({"hotkey": {"compose": "x"}}) == ["hotkey.compose"])
check("unknown_keys 抓整段新写的配置",
      C.unknown_keys({"logging": {"rotate_mb": 5}}) == ["logging.rotate_mb"])
check("unknown_keys 抓空的错误段", C.unknown_keys({"logging": {}}) == ["logging"])
check("unknown_keys 不误伤空的合法段", C.unknown_keys({"output": {}}) == [])
check("unknown_keys 不误伤合法键",
      C.unknown_keys({"output": {"start_enabled": False}, "hotkeys": {"quit": "ctrl+alt+q"}}) == [])
check("get_dotted 取值", C.get_dotted({"a": {"b": 1}}, "a.b") == 1)
check("get_dotted 路径断了给 None", C.get_dotted({"a": {"b": 1}}, "a.c") is None)
check("get_dotted 中间层不是 dict 也给 None", C.get_dotted({"a": 1}, "a.b") is None)

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  3. read_raw：坏文件一律给 {}，不抛")
print("=" * 72)

missing = TMP / "nope.json"
raw, path = C.read_raw(missing)
check("文件不存在 -> ({}, 路径)", raw == {} and path == missing)

broken = TMP / "broken.json"
broken.write_text("{ 这不是 JSON", encoding="utf-8")
raw, _ = C.read_raw(broken)
check("坏 JSON -> {}", raw == {})

arr = TMP / "array.json"
arr.write_text("[1, 2, 3]", encoding="utf-8")
raw, _ = C.read_raw(arr)
check("根不是对象 -> {}", raw == {})

good = TMP / "good.json"
good.write_text(json.dumps({"output": {"tray": False}}), encoding="utf-8")
raw, path = C.read_raw(good)
check("正常文件原样读回", raw == {"output": {"tray": False}} and path == good)

bom = TMP / "bom.json"
bom.write_bytes(b"\xef\xbb\xbf" + json.dumps({"output": {"tray": False}}).encode("utf-8"))
raw, _ = C.read_raw(bom)
check("带 BOM 的文件也读得出来（utf-8-sig）", raw == {"output": {"tray": False}}, repr(raw))

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  4. load()：深合并、环境变量、BOM")
print("=" * 72)

clear_env()
part = TMP / "part.json"
part.write_text(json.dumps({"output": {"start_enabled": False}}), encoding="utf-8")
cfg, cfgp = C.load(part)
check("load 返回实际路径", cfgp == part)
check("写了的键被采用", cfg["output"]["start_enabled"] is False)
check("没写的兄弟键保留默认值", cfg["output"]["default_language"] == C.DEFAULTS["output"]["default_language"])
check("没写的整段保留默认值", cfg["pinyin"] == C.DEFAULTS["pinyin"])
check("DEFAULTS 本身没被改坏", C.DEFAULTS["output"]["start_enabled"] is True)

bomcfg = TMP / "bomcfg.json"
bomcfg.write_bytes(b"\xef\xbb\xbf" + json.dumps({"output": {"start_enabled": False}}).encode("utf-8"))
cfg, _ = C.load(bomcfg)
check("带 BOM 的配置**没有**被整份忽略（真 bug 的回归点）",
      cfg["output"]["start_enabled"] is False,
      "以前这里会退回默认值 True")

broken_cfg = TMP / "brokencfg.json"
broken_cfg.write_text("{{{", encoding="utf-8")
cfg, _ = C.load(broken_cfg)
check("坏 JSON 不炸，退回默认值", cfg["output"]["start_enabled"] is True)

with_key = TMP / "withkey.json"
with_key.write_text(json.dumps({"api": {"api_key": "sk-from-file"}}), encoding="utf-8")
cfg, _ = C.load(with_key)
check("没有环境变量时用文件里的 key", cfg["api"]["api_key"] == "sk-from-file")
os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
cfg, _ = C.load(with_key)
check("环境变量盖过文件", cfg["api"]["api_key"] == "sk-from-env")
check("env_api_key() 读到同一个值", C.env_api_key() == "sk-from-env")
os.environ.pop("DEEPSEEK_API_KEY")
os.environ["T_IME_API_KEY"] = "sk-from-alt"
check("备用环境变量 T_IME_API_KEY 也认", C.env_api_key() == "sk-from-alt")
clear_env()
check("清掉环境变量后 env_api_key() 为空", C.env_api_key() == "")

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  5. effective_rows：每个选项的来源")
print("=" * 72)

raw = {"output": {"start_enabled": False}, "hotkey": {"compose": "x"}}
cfg, _ = C.load(part)
rows = C.effective_rows(cfg, raw)
src = {k: s for k, s, _ in rows}
check("写了的 -> file", src.get("output.start_enabled") == "file")
check("没写的 -> default", src.get("output.tray") == "default")
check("拼错的 -> ignored", src.get("hotkey.compose") == "ignored")
check("ignored 的键不在「缺键」里重复出现",
      all(k in src for k in C.unknown_keys(raw)))
os.environ["DEEPSEEK_API_KEY"] = "sk-env-wins"
cfg_env, _ = C.load(with_key)
src_env = {k: s for k, s, _ in C.effective_rows(cfg_env, {"api": {"api_key": "sk-from-file"}})}
check("环境变量 -> env", src_env.get("api.api_key") == "env")
check("env 时值就是环境变量的值",
      dict((k, v) for k, _, v in C.effective_rows(cfg_env, {}))["api.api_key"] == "sk-env-wins")
clear_env()

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  6. 打码：密钥要遮住，热键不许被误遮")
print("=" * 72)

secret = "sk-1234567890abcdef"
masked = main_mod._fmt_cfg_val("api.api_key", secret)
check("api.api_key 被打码", secret not in masked and "***" in masked, masked)
check("api_key 为空时说清楚", "未配置" in main_mod._fmt_cfg_val("api.api_key", ""))
check("api_key 为 None 也不炸", "未配置" in main_mod._fmt_cfg_val("api.api_key", None))
for name in ("api.token", "api.secret", "db.password", "somekey"):
    check(f"{name} 被打码", "***" in main_mod._fmt_cfg_val(name, "abc") or "未配置" in main_mod._fmt_cfg_val(name, "abc"))
hotkey = main_mod._fmt_cfg_val("hotkeys.compose", "ctrl+alt+space")
check("hotkeys.compose **不**被打码（它含 key 但不是密钥）",
      hotkey == '"ctrl+alt+space"', hotkey)
check("普通值按 JSON 打出来", main_mod._fmt_cfg_val("output.start_enabled", False) == "false")
check("中文值不被转义", main_mod._fmt_cfg_val("overlay.font_family", "微软雅黑") == '"微软雅黑"')

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  7. init_config 不许覆盖已有配置")
print("=" * 72)

fresh = TMP / "fresh" / "config.json"
got = C.init_config(fresh)
check("没有配置时能生成", got == fresh and fresh.exists())
check("生成的内容 == 模板", fresh.read_text(encoding="utf-8-sig") == example_text)

keep = TMP / "keep.json"
keep.write_text(json.dumps({"output": {"start_enabled": False}}), encoding="utf-8")
C.init_config(keep)
check("已有配置原样不动（不会被模板盖掉）",
      json.loads(keep.read_text(encoding="utf-8-sig")) == {"output": {"start_enabled": False}})

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  8. --show-config 命令行")
print("=" * 72)


def run_show_config(path):
    proc = subprocess.run(
        [sys.executable, "main.py", "--config", str(path), "--show-config"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    return proc.returncode, proc.stdout + proc.stderr


code, out = run_show_config(part)
check("干净配置 exit=0", code == 0, f"exit={code}")
check("打印了生效配置段", "== 生效配置 ==" in out)
check("标注了来源（文件/默认）", "[文件]" in out and "[默认]" in out)
check("干净配置报「没有拼错的键」", "没有拼错的键" in out)
check("列出了没写进配置的选项", "正在用默认值的选项" in out and "output.tray" in out)
check("热键以明文出现（没被当成密钥）", "ctrl+alt+" in out)

typo_cfg = TMP / "typo.json"
typo_cfg.write_text(json.dumps({
    "hotkey": {"compose": "ctrl+alt+space"},
    "output": {"start_enabled": False, "tray_icon": True},
    "api": {"api_key": "sk-super-secret-value"},
}, ensure_ascii=False), encoding="utf-8")
code, out = run_show_config(typo_cfg)
check("拼错的键 -> exit=1（可以脚本化检查）", code == 1, f"exit={code}")
check("点名 hotkey", "! hotkey.compose" in out)
check("点名 output.tray_icon", "! output.tray_icon" in out)
check("说明写错的键会被静默忽略", "静静忽略" in out or "永远不生效" in out)
check("密钥没漏进输出", "sk-super-secret-value" not in out, "打码失败")
check("密钥被打了码", "***" in out)
check("文件里的非密钥值照常打印", '"ctrl+alt+space"' in out)

code, out = run_show_config(TMP / "nothing-here.json")
check("配置文件不存在也能跑（exit=0）", code == 0, f"exit={code}")
check("说清「全部使用内置默认值」", "全部使用内置默认值" in out)

help_out = subprocess.run([sys.executable, "main.py", "--help"], cwd=str(ROOT),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180).stdout
check("--help 里有 --show-config", "--show-config" in help_out)

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print("  9. --init-config 也要认 --config（别写进本目录）")
print("=" * 72)

# 第 61 轮：`--config D:\my\ime.json --init-config` 以前写死 init_config()，
# 模板只会落在 root\config.json —— 用户拿到一份「不是自己在用的」配置，
# 而实例 --config D:\my\ime.json 永远读不到它。
cli_cfg = TMP / "cli_cfg" / "mine.json"
root_cfg_before = (ROOT / "config.json").read_bytes()
proc = subprocess.run(
    [sys.executable, "main.py", "--init-config", "--config", str(cli_cfg)],
    cwd=str(ROOT), capture_output=True, text=True,
    encoding="utf-8", errors="replace", timeout=180,
)
out = proc.stdout + proc.stderr
check("--init-config --config 退出码 0", proc.returncode == 0, f"exit={proc.returncode}")
check("模板写到了 --config 指定的位置", cli_cfg.is_file(), str(cli_cfg))
check("写出来的内容 == 模板", cli_cfg.read_text(encoding="utf-8-sig") == example_text)
check("stdout 点名了那个绝对路径", str(cli_cfg.resolve()) in out, out.strip()[:200])
check("仓库本目录的 config.json 一个字节都没动",
      (ROOT / "config.json").read_bytes() == root_cfg_before)

cli_cfg.write_text('{"output": {"start_enabled": false}}', encoding="utf-8")
proc2 = subprocess.run(
    [sys.executable, "main.py", "--init-config", "--config", str(cli_cfg)],
    cwd=str(ROOT), capture_output=True, text=True,
    encoding="utf-8", errors="replace", timeout=180,
)
out2 = proc2.stdout + proc2.stderr
check("已存在的自定义配置不会被模板盖掉",
      json.loads(cli_cfg.read_text(encoding="utf-8-sig")) == {"output": {"start_enabled": False}})
check("并且明说「已存在，没动它」", "已存在" in out2, out2.strip()[:200])

proc3 = subprocess.run(
    [sys.executable, "main.py", "--init-config"],
    cwd=str(ROOT), capture_output=True, text=True,
    encoding="utf-8", errors="replace", timeout=180,
)
out3 = proc3.stdout + proc3.stderr
check("不带 --config 时仍然认本目录那份，且不覆盖它",
      proc3.returncode == 0 and (ROOT / "config.json").read_bytes() == root_cfg_before,
      f"exit={proc3.returncode}")

restore_env()

# ---------------------------------------------------------------------- #
print()
print("=" * 72)
print(f"  结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
sys.exit(1 if FAIL else 0)
