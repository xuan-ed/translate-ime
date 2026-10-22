# -*- coding: utf-8 -*-
"""``main.py --doctor`` 的集成测试。

体检脚本本身也是代码，也会坏 —— 而它是用户唯一能自己跑出一条结论的东西。
这里用 ``--doctor-quick``（跳过试装托盘 / 真装钩子 / 端到端）跑一遍子进程，
只验证「能跑完、判据正确、报告落盘」，不依赖托盘能不能装上。

直接跑：``python tests/test_doctor.py``
"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime import daemon  # noqa: E402
from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

OK = "[OK]  "
BAD = "[FAIL]"
results: list[tuple[bool, str]] = []


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"  — {detail}" if detail else ""))
    return cond


REPORT = PROJ / "translate-ime-doctor.txt"
if REPORT.exists():
    REPORT.unlink()          # 先删掉，否则分不清「这次写的」还是上次剩的

print("=" * 74)
print("跑 main.py --doctor --doctor-quick（子进程，最多等 180 秒）")
print("=" * 74)
proc = subprocess.run(
    [sys.executable, "main.py", "--doctor", "--doctor-quick"],
    cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8", errors="replace",
    timeout=180,
)
out = proc.stdout or ""
err = proc.stderr or ""

check(proc.returncode == 0, "--doctor-quick 退出码为 0（有 FAIL 才会是 1）",
      f"exit={proc.returncode}" + (f" stderr={err.strip()[:200]}" if proc.returncode else ""))
check("[FAIL]" not in out, "输出里没有 [FAIL]",
      "; ".join(ln.strip() for ln in out.splitlines() if "[FAIL]" in ln)[:300])

for section in ("[1] 进程身份与环境", "[2] shell 与托盘图标", "[3] 配置、词库与翻译",
                "[4] 日志文件", "[6] 候选窗能不能真的出现在屏幕上", "== 结论 =="):
    check(section in out, f"报告里有 {section}")

check("DPI 感知" in out and "缩放比" in out, "报告了 DPI 感知级别与缩放比")
check("api.provider" in out and "实际引擎" in out,
      "[3] 报出 api.provider 与「实际引擎」（不再只凭有没有 key 猜）")
check("开机自启" in out, "[1] 段带上了开机自启（objective ① 的可选项）")
check("跳过端到端" in out, "--doctor-quick 确实跳过了 [7] 端到端")

# ---------------------------------------------------------------------------
# 第 120 轮：体检报告里有两个 pid —— [0] 的「进程号」是常驻实例（不变），
# [1] 以前只写「pid」，其实是**正在跑体检的这个进程**（每次跑都换）。用户拿
# 两份报告一对比，会以为输入法在反复重启。现在必须写明是谁。
# ---------------------------------------------------------------------------
import re

check("诊断进程 pid" in out,
      "[1] 的 pid 行写明了「诊断进程」，不再和 [0] 的「进程号」抢同一个词")
check("跑这次体检的进程" in out and "常驻实例" in out,
      "诊断进程 pid 的解释里点明它不是常驻实例（两份报告对比不会误读）")
_bare_pid = [ln for ln in out.splitlines() if re.match(r"\s*\[ok \] pid\s+\d+\s*$", ln)]
check(not _bare_pid, "[1] 不再有光秃秃的「pid <数字>」行", str(_bare_pid[:2]))

# [6] 的三条硬判据：有窗口句柄、可见、落在显示器上
check("窗口句柄" in out and "IsWindowVisible" in out and "落在显示器上" in out,
      "[6] 报告了 句柄 / 可见性 / 是否落在显示器上")
check("否 —— 窗口在屏幕外" not in out, "[6] 没报「窗口在屏幕外」")

check(REPORT.exists(), f"报告落盘到 {REPORT.name}")
if REPORT.exists():
    text = REPORT.read_text(encoding="utf-8")
    check(len(text) > 500, "报告文件不是空的", f"{len(text)} 字符")
    check("== 结论 ==" in text, "报告文件里也有结论段")
    check("[1] 进程身份与环境" in text, "报告文件里有完整分段")

# ---------------------------------------------------------------------------
# 第二阶段：[0] 段遇到「上一个实例被强杀」留下的残留状态文件时，不能把死进程
# 报成活的、也不能去读它的字段（那些字段描述的是一个不存在的进程）。
# ---------------------------------------------------------------------------
print()
print("=" * 74)
print("现场布置：一个 pid 已经不存在的残留状态文件，再跑一次 --doctor-quick")
print("=" * 74)
DEAD_PID = 999999
daemon.write_status(PROJ, {"pid": DEAD_PID, "started": time.time() - 600,
                           "enabled": True, "direct": False, "active": "zh"})
try:
    proc2 = subprocess.run(
        [sys.executable, "main.py", "--doctor", "--doctor-quick"],
        cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180,
    )
    out2 = proc2.stdout or ""

    check(proc2.returncode == 0, "残留状态文件不会让 --doctor 判 FAIL（退出码仍是 0）",
          f"exit={proc2.returncode}")
    check("状态文件是残留的" in out2, "[0] 点明状态文件是残留的（没把死进程报成活的）",
          "; ".join(ln.strip() for ln in out2.splitlines() if "残留" in ln)[:200])
    check(str(DEAD_PID) in out2, "[0] 报出了那个已经不在了的进程号", f"pid={DEAD_PID}")

    head = out2.split("[1] 进程身份")[0]
    check("候选窗" not in head, "[0] 不再列活实例才有的字段（候选窗）",
          "; ".join(ln.strip() for ln in head.splitlines() if "候选窗" in ln)[:200])
    check("[FAIL]" not in out2, "残留状态文件这条路径上没有 [FAIL]")
finally:
    daemon.clear_status(PROJ)

# ---------------------------------------------------------------------------
# 第三阶段：候选窗「一次都没显示过」不是无条件故障。挂起（字母直通）、英文
# 直通（字母原样透传）、以及刚起完还没人敲过键 —— 这三种情况下它本来就不该
# 弹。把这些正常状态判成 FAIL，用户会照着去修一个没坏的东西（第 58 轮）。
# ---------------------------------------------------------------------------
print()
print("=" * 74)
print("现场布置：四种「活着的实例」，候选窗 shows 都是 0")
print("=" * 74)

sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
try:
    COMMON = {
        "pid": sleeper.pid, "started": time.time(), "enabled": True,
        "direct": False, "active": "zh", "tray": False, "hook": True,
        "hook_info": {"installed": True, "alive": True, "error": None,
                      "real": 0, "total": 0, "injected": 0, "self_injected": 0,
                      "foreign_injected": 0, "effective": 0, "swallowed": 0},
        "overlay": {"shows": 0, "visible": False, "mode": "hidden",
                    "on_monitor": False, "rect": None, "error": None},
        "tick_errors": 0,
    }

    def doctor_quick(status: dict) -> tuple[int, str]:
        daemon.write_status(PROJ, status)
        try:
            p = subprocess.run(
                [sys.executable, "main.py", "--doctor", "--doctor-quick"],
                cwd=str(PROJ), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180,
            )
        finally:
            daemon.clear_status(PROJ)
            REPORT.unlink(missing_ok=True)
        return p.returncode, p.stdout or ""

    def status_with(**over) -> dict:
        st = json.loads(json.dumps(COMMON))     # 深拷贝，别让用例之间串味
        st.update(over)
        return st

    def cand_line(out: str) -> str:
        return next((ln.strip() for ln in out.splitlines() if "候选窗" in ln), "")

    rc, out = doctor_quick(status_with())
    check(rc == 0 and "[FAIL]" not in out,
          "还没敲过键时 shows=0 不判 FAIL（退出码仍是 0）", f"exit={rc}")
    check("[warn]" in cand_line(out) and "还没收到过任何按键" in cand_line(out),
          "还没敲过键时只说「敲几个拼音字母再看这一行」", cand_line(out))

    rc, out = doctor_quick(status_with(
        enabled=False, hook_info=dict(COMMON["hook_info"], real=18)))
    check(rc == 0 and "[FAIL]" not in out,
          "挂起时 shows=0 不判 FAIL（挂起时字母直通）", f"exit={rc}")
    check("[info]" in cand_line(out) and "挂起" in cand_line(out)
          and "本来就不弹候选" in cand_line(out),
          "挂起时说明「本来就不弹候选」", cand_line(out))

    rc, out = doctor_quick(status_with(
        direct=True, hook_info=dict(COMMON["hook_info"], real=18)))
    check(rc == 0 and "[FAIL]" not in out,
          "英文直通时 shows=0 不判 FAIL（直通时字母原样透传）", f"exit={rc}")
    check("[info]" in cand_line(out) and "英文直通" in cand_line(out)
          and "本来就不弹候选" in cand_line(out),
          "英文直通时说明「本来就不弹候选」", cand_line(out))

    rc, out = doctor_quick(status_with(
        hook_info=dict(COMMON["hook_info"], real=18)))
    check(rc == 1 and "[FAIL]" in out,
          "真故障（拼音模式敲过键、候选一次没弹）仍然判 FAIL 且退出码 1",
          f"exit={rc}")
    check("确实敲过键" in cand_line(out) and "直接证据" in cand_line(out),
          "真故障时点明「确实敲过键」这个前提", cand_line(out))

    # 第 71 轮：装了改键器/宏/软键盘的机器上，钩子收到的每个键都带 INJECTED，
    # hook_info.real 永远是 0，但用户确实敲了字。诊断必须用「有效按键」
    # （real + foreign_injected），不然同一屏上钩子说收到了、候选窗说你没敲过。
    rc, out = doctor_quick(status_with(
        hook_info=dict(COMMON["hook_info"], real=0, foreign_injected=18)))
    check(rc == 1 and "[FAIL]" in out,
          "只有改键器转发的按键时，shows=0 也要判 FAIL（键确实进来了）",
          f"exit={rc}")
    check("有效按键 18 个" in cand_line(out) and "确实敲过键" in cand_line(out),
          "只有转发的按键时，候选窗那行不能再说「你还没敲过键」", cand_line(out))
    hook_line = next((ln.strip() for ln in out.splitlines() if "键盘钩子" in ln), "")
    check("[ok ]" in hook_line and "还没敲过键" not in hook_line,
          "钩子够用了就别再刷 warn（有效按键 18 个）", hook_line)

    rc, out = doctor_quick(status_with(
        hook_info=dict(COMMON["hook_info"], real=0, foreign_injected=18),
        overlay={"shows": 3, "visible": True, "mode": "candidates",
                 "on_monitor": True, "rect": [0, 0, 300, 100], "error": None}))
    check(rc == 0 and "[ok ]" in next(
        (ln.strip() for ln in out.splitlines() if "键盘钩子" in ln), ""),
          "有效按键 > 0 时键盘钩子那行是 ok，不是 warn", f"exit={rc}")

    # 第 86 轮：`overlay.shows` 连提示条（启动引导、托盘警告）也数，于是静置不敲键
    # 也会变成 shows=2。以前体检拿它当「候选窗出现过」的证据，只弹过提示的机器
    # 会被判成「候选窗 显示过 2 次 · ok」—— 正好替用户报的那个故障打了包票。
    rc, out = doctor_quick(status_with(
        hook_info=dict(COMMON["hook_info"], real=0, foreign_injected=18),
        overlay={"shows": 2, "cand_shows": 0, "visible": False, "mode": "hidden",
                 "on_monitor": False, "rect": [0, 0, 691, 251], "error": None}))
    check(rc == 1 and "[FAIL]" in cand_line(out),
          "只弹过提示（cand_shows=0）时候选窗那行必须 FAIL，哪怕 shows>0",
          f"exit={rc}")
    check("一次都没弹过" in cand_line(out) and "条是提示" in cand_line(out),
          "候选窗那行要说清「候选窗 0 次，shows 那些是提示」", cand_line(out))

    rc, out = doctor_quick(status_with(
        hook_info=dict(COMMON["hook_info"], real=0, foreign_injected=18),
        overlay={"shows": 5, "cand_shows": 2, "visible": False, "mode": "hidden",
                 "on_monitor": True, "rect": [0, 0, 300, 100], "error": None}))
    check(rc == 0 and "显示过 2 次" in cand_line(out) and "3 条是提示" in cand_line(out),
          "候选窗 2 次 + 3 条提示：体检分开报，不把提示算进候选窗",
          cand_line(out))

    # 第 60 轮：实例是带 --config 另一份配置起来的。[3] 下面这些行读的是
    # 本目录 config.json —— 不说清楚，用户会把那些热键/开关当成实例的现状。
    other_cfg = PROJ / ".tmp" / "doctor_other_config.json"
    other_cfg.parent.mkdir(parents=True, exist_ok=True)
    other_cfg.write_text('{"input": {}}', encoding="utf-8")
    try:
        rc, out = doctor_quick(status_with(config=str(other_cfg)))
        check(rc == 0 and "[FAIL]" not in out,
              "实例用的是另一份配置时不判 FAIL（那只是「说清楚」，不是故障）",
              f"exit={rc}")
        inst_line = next((ln.strip() for ln in out.splitlines() if "实例配置" in ln), "")
        check("[warn]" in inst_line and "不是它" in inst_line
              and "doctor_other_config" in inst_line,
              "[3] 说明「实例用的是这一份，下面这些行不是它」", inst_line)
        check(any("配置文件" in ln and "doctor_other_config" in ln
                  for ln in out.splitlines()),
              "[0] 也报出了实例实际用的配置路径")
    finally:
        other_cfg.unlink(missing_ok=True)

    rc, out = doctor_quick(status_with())
    check("（这个实例没报，可能是旧版本）" in out,
          "实例没报配置路径时，[0] 说「可能是旧版本」而不是瞎猜")

    # 第 62 轮：配了 key 但 api.provider=mock 时，[3] 必须说清「引擎是离线的、
    # 而且是你主动选的」—— 以前只说「API key 已配置」，用户会以为在线翻译在工作。
    mock_cfg = PROJ / ".tmp" / "doctor_forced_mock.json"
    mock_cfg.parent.mkdir(parents=True, exist_ok=True)
    mock_cfg.write_text(json.dumps({"api": {"provider": "mock",
                                            "api_key": "sk-doctor-test"}}),
                        encoding="utf-8")
    try:
        daemon.write_status(PROJ, status_with())
        p = subprocess.run(
            [sys.executable, "main.py", "--doctor", "--doctor-quick",
             "--config", str(mock_cfg)],
            cwd=str(PROJ), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
        outm = p.stdout or ""
        engine_line = next((ln.strip() for ln in outm.splitlines() if "实际引擎" in ln), "")
        check("api.provider" in outm, "[3] 报出 api.provider")
        check("已配置" in outm, "key 已配置这件事照样报出来")
        check("离线词表" in engine_line and "api.provider=mock" in engine_line,
              "provider=mock 时 [3] 说「实际引擎是离线词表」", engine_line)
        check("优先于 API key" in outm,
              "并且说清它优先于 key（配了 key 也不联网）")
    finally:
        mock_cfg.unlink(missing_ok=True)
        daemon.clear_status(PROJ)
        REPORT.unlink(missing_ok=True)

    # 第 63 轮：热键「能解析」不等于「按了有用」。动作名拼错（hotkeys.quitt）
    # 或两个动作撞同一个组合键时，真实例会把原因只写进日志、退出键被顶掉；
    # 体检以前照样给 [ok]，用户按「全部通过」去按退出键，按出来的却是挂起。
    bad_cfg = PROJ / ".tmp" / "doctor_bad_hotkeys.json"
    bad_cfg.parent.mkdir(parents=True, exist_ok=True)
    bad_cfg.write_text(json.dumps({"hotkeys": {
        "quitt": "ctrl+alt+f8",
        "toggle_enabled": "ctrl+alt+q",
        "quit": "ctrl+alt+q",
    }}), encoding="utf-8")
    try:
        daemon.write_status(PROJ, status_with())
        p = subprocess.run(
            [sys.executable, "main.py", "--doctor", "--doctor-quick",
             "--config", str(bad_cfg)],
            cwd=str(PROJ), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
        outb = p.stdout or ""
        quitt_line = next((ln.strip() for ln in outb.splitlines()
                           if "热键 quitt" in ln), "")
        quit_line = next((ln.strip() for ln in outb.splitlines()
                          if "热键 quit" in ln), "")
        check(p.returncode == 1, "热键配坏时 --doctor 退出码 1（不再「全部通过」）",
              f"exit={p.returncode}")
        check("[FAIL]" in quitt_line and "不认识" in quitt_line,
              "拼错的动作名被判 [FAIL]，并说清「不认识」", quitt_line)
        check("绑了同一个组合键" in quit_line and "实际生效的是" in quit_line,
              "撞车的那个动作被判 [FAIL]，并说清谁真的生效", quit_line)
    finally:
        bad_cfg.unlink(missing_ok=True)
        daemon.clear_status(PROJ)
        REPORT.unlink(missing_ok=True)

    # 第 65 轮：诊断必须分析**本次真正加载的那份配置**。以前 [3] 写死
    # root/config.json，`--doctor --config X` 时会出现「[warn] X 里有 1 个键
    # 不认识」紧接着「[ok] 配置拼写 没有不认识的键」这种自相矛盾，缺项统计也
    # 数的是没在用的那份。
    explicit_cfg = PROJ / ".tmp" / "doctor_explicit.json"
    explicit_cfg.parent.mkdir(parents=True, exist_ok=True)
    base_cfg = json.loads((PROJ / "config.json").read_text(encoding="utf-8"))
    base_cfg.setdefault("log", {})["path"] = "nope.log"   # DEFAULTS 不认识 log.path
    explicit_cfg.write_text(json.dumps(base_cfg, ensure_ascii=False), encoding="utf-8")
    try:
        daemon.write_status(PROJ, status_with())
        p = subprocess.run(
            [sys.executable, "main.py", "--doctor", "--doctor-quick",
             "--config", str(explicit_cfg)],
            cwd=str(PROJ), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
        oute = p.stdout or ""
        label = next((ln.strip() for ln in oute.splitlines()
                      if "doctor_explicit.json" in ln), "")
        spell = next((ln.strip() for ln in oute.splitlines()
                      if "配置拼写" in ln), "")
        check("doctor_explicit.json" in label,
              "[3] 描述的是 --config 指的那份文件，不是本目录 config.json", label)
        check("log.path" in spell and "不认识" in spell,
              "拼写检查确实针对那一份（log.path 被点名）", spell)
    finally:
        explicit_cfg.unlink(missing_ok=True)
        daemon.clear_status(PROJ)
        REPORT.unlink(missing_ok=True)

    rc, out = doctor_quick(status_with())
    check("[ok ] config.json" in out,
          "不带 --config 时，[3] 仍然描述本目录 config.json")

    # 第 66 轮：体检只做检查，不能顺手把被检查的东西造出来。以前 [4] 直接
    # `log.open("a")`（目录不在还先 mkdir），第一次跑 --doctor 就在项目根留下
    # 一个空的 translate-ime.log；结尾那句「日志：translate-ime.log」又是写死的，
    # 用 --log 指到别处时把用户指向错误的文件（[4] 明明已经报出真实路径）。
    print()
    print("=" * 74)
    print("现场布置：日志路径指向一个还不存在的目录，再指向一个真实目录")
    print("=" * 74)
    log_dir = PROJ / ".tmp" / "doctor-log"
    missing_dir = log_dir / "missing"
    probe_log = log_dir / "probe.log"
    log_dir.mkdir(parents=True, exist_ok=True)

    def doctor_with_log(path: Path) -> str:
        p = subprocess.run(
            [sys.executable, "main.py", "--doctor", "--doctor-quick",
             "--log", str(path)],
            cwd=str(PROJ), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
        out_ = p.stdout or ""
        return out_

    def line_with(out_: str, needle: str) -> str:
        return next((ln.strip() for ln in out_.splitlines() if needle in ln), "")

    def log_line(out_: str) -> str:
        """只要 [4] 那一行结论，别抓成段标题「[4] 日志文件」。"""
        return next((ln.strip() for ln in out_.splitlines()
                     if "日志文件" in ln
                     and any(t in ln for t in ("[ok ]", "[info]", "[warn]", "[FAIL]"))), "")

    out_missing = doctor_with_log(missing_dir / "probe.log")
    check("目录还不存在" in log_line(out_missing),
          "[4] 日志目录不存在时如实说「还不存在」，不自己建",
          log_line(out_missing))
    check(not missing_dir.exists(), "体检没有顺手把日志目录建出来")

    out_dir = doctor_with_log(probe_log)
    check("目录可写，日志还没生成" in log_line(out_dir),
          "[4] 目录在、日志还没生成时说「目录可写，日志还没生成」",
          log_line(out_dir))
    hint = line_with(out_dir, "日志：")
    check(str(probe_log) in hint and "translate-ime.log" not in hint,
          "结尾「日志：…」用的是 --log 指的那份，不是写死的 translate-ime.log",
          hint)
    check(not probe_log.exists(), "体检只探可写性，没有真的生成那个日志文件")
    check(not list(log_dir.glob(".*.doctor-*.tmp")), "临时探针文件用完就删，没留垃圾")

    probe_log.write_text("hello\n", encoding="utf-8", newline="")
    try:
        out_exist = doctor_with_log(probe_log)
        exist_line = log_line(out_exist)
        check("可写，当前 6 B" in exist_line,
              "[4] 已有日志时报出真实大小", exist_line)
        check(probe_log.read_text(encoding="utf-8") == "hello\n",
              "体检没有改动已有日志的内容")
    finally:
        probe_log.unlink(missing_ok=True)
finally:
    sleeper.terminate()
    try:
        sleeper.wait(timeout=10)
    except subprocess.TimeoutExpired:
        sleeper.kill()

REPORT.unlink(missing_ok=True)   # 跑完别在仓库根留下报告文件（第 39 轮发现）
shutil.rmtree(PROJ / ".tmp" / "doctor-log", ignore_errors=True)

# ------------------------------------------------------------------ #
print()
print("=" * 74)
print("translate.enabled=false：体检不许把「关掉了」说成「没配 key / 退回离线」")
print("=" * 74)
# 第 78 轮：翻译是用户自己关的，不是故障。以前 [3] 照样打
# `[warn] API key 未配置（翻译退回离线 mock 引擎）` + `[warn] 实际引擎 离线词表（没配 API key）`，
# 用户会去翻一个根本不存在的 key，而真正的事实是「翻译没开工」。
_no_tr_dir = PROJ / ".tmp" / "doctor-notr"
_no_tr_dir.mkdir(parents=True, exist_ok=True)
_no_tr_cfg = _no_tr_dir / "alt.json"
_no_tr_cfg.write_text('{"translate": {"enabled": false}, "output": {"tray": false, "log_file": ".tmp/doctor-notr/alt.log"}}',
                      encoding="utf-8")
try:
    _p = subprocess.run(
        [sys.executable, "main.py", "--doctor", "--doctor-quick", "--config", str(_no_tr_cfg)],
        cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )
    _out = _p.stdout or ""
    check(_p.returncode == 0, "关掉翻译时 --doctor-quick 退出码 0（合法配置不是故障）",
          f"exit={_p.returncode}")
    _eng = next((ln.strip() for ln in _out.splitlines() if "实际引擎" in ln), "")
    _key = next((ln.strip() for ln in _out.splitlines() if "API key" in ln and "配置" in ln), "")
    check("翻译已关闭" in _eng and "translate.enabled=false" in _eng,
          "[3] 实际引擎直说「翻译已关闭」", _eng)
    check("离线词表（没配 API key）" not in _eng,
          "不再说「离线词表（没配 API key）」", _eng)
    check("用不上" in _key, "API key 那行说清「这项用不上」，而不是 warn 让人去配 key", _key)
finally:
    shutil.rmtree(_no_tr_dir, ignore_errors=True)
    REPORT.unlink(missing_ok=True)

# ------------------------------------------------------------------ #
print()
print("=" * 74)
print("索引 0 键：体检的「拼音索引」不许打绿，候选那行要明说跳过")
print("=" * 74)
# 第 79 轮：pinyin.min_freq 设太大时索引是 0 个键，体检照样
# `[ok ] 拼音索引 0 键 / 0.4s`，候选 nihao 那一项直接消失 ——
# 一份「一切正常」的报告，配一台打字不出候选的输入法。
_empty_dir = PROJ / ".tmp" / "doctor-emptyidx"
_empty_dir.mkdir(parents=True, exist_ok=True)
_empty_cfg = _empty_dir / "alt.json"
_empty_cfg.write_text(
    '{"pinyin": {"min_freq": 100000000, "dict_cache": ".tmp/doctor-emptyidx/idx.pkl"},'
    ' "output": {"tray": false, "log_file": ".tmp/doctor-emptyidx/alt.log"}}',
    encoding="utf-8",
)
try:
    _p = subprocess.run(
        [sys.executable, "main.py", "--doctor", "--doctor-quick", "--config", str(_empty_cfg)],
        cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )
    _out = _p.stdout or ""
    _idx = next((ln.strip() for ln in _out.splitlines() if "拼音索引" in ln), "")
    _cand = next((ln.strip() for ln in _out.splitlines() if "候选 nihao" in ln), "")
    check(_p.returncode != 0, "索引 0 键时体检退出码非 0（不能报一切正常）",
          f"exit={_p.returncode}")
    check("[FAIL]" in _idx and "0 键" in _idx, "「拼音索引」那行是 FAIL 并说出 0 键", _idx)
    check("min_freq" in _idx, "并指出嫌疑参数 pinyin.min_freq", _idx)
    check("跳过" in _cand and "索引是空的" in _cand,
          "候选 nihao 那行明说「跳过」，不是静默消失", _cand)
finally:
    shutil.rmtree(_empty_dir, ignore_errors=True)
    REPORT.unlink(missing_ok=True)

# ------------------------------------------------------------------ #
print()
print("=" * 74)
print("output.tray=false：体检不许把「你自己关的」说成「没装上 / UIPI 拦的」")
print("=" * 74)
# 第 82 轮真机：tray 关掉的机器上，[0] 说「没装上（不影响打字）」、[1] 说
# 「托盘图标会被 UIPI 拦掉」、结论还说「这里装不上托盘图标是 UIPI 导致的」——
# 用户于是去查完整性级别，可他本来就把托盘关了。用户自己关掉的功能不是故障。
_notray_dir = PROJ / ".tmp" / "doctor-notray"
_notray_dir.mkdir(parents=True, exist_ok=True)
_notray_cfg = _notray_dir / "alt.json"
_notray_cfg.write_text(
    '{"output": {"tray": false, "log_file": ".tmp/doctor-notray/alt.log"}}',
    encoding="utf-8",
)
try:
    _p = subprocess.run(
        [sys.executable, "main.py", "--doctor", "--doctor-quick", "--config", str(_notray_cfg)],
        cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )
    _out = _p.stdout or ""
    check(_p.returncode == 0, "托盘关掉时体检退出码 0（合法配置不是故障）",
          f"exit={_p.returncode}")
    check("output.tray=false" in _out and "这一项跳过（不是装不上）" in _out,
          "[2] 明说 output.tray=false、这一项跳过")
    # 已经有实例在跑时，[0] 会照实报那个实例的托盘状态（本机 Low 完整性 →
    # NIM_ADD 被拒 → 「没装上（不影响打字）」）。那是**实例**的状态，不是这次
    # tray=false 体检的结论 —— 拿整篇输出去断言会把正常状态误判成回归。
    # 只盯这次配置真正决定的那几行（[1] / [2] / 结论）。
    if daemon.is_running():
        print("  [SKIP] 不再把「你自己关的」写成「没装上 / UIPI 拦的」"
              " —— 已经有实例在跑，[0] 会照实报它的托盘状态")
    else:
        check("没装上（不影响打字）" not in _out and "托盘图标会被 UIPI 拦掉" not in _out,
              "不再把「你自己关的」写成「没装上 / UIPI 拦的」")
    check("托盘图标你已经关了" in _out, "[1] 的低完整性后果也说清托盘是关着的")
    check("装不上托盘图标是 UIPI 导致的" not in _out, "结论不再把托盘算成 UIPI 故障")
finally:
    shutil.rmtree(_notray_dir, ignore_errors=True)
    REPORT.unlink(missing_ok=True)

# ------------------------------------------------------------------ #
print()
print("=" * 74)
print("索引缓存写不进去：体检要说「每次启动都要重建」，不许只报 [ok ] 拼音索引")
print("=" * 74)
# 第 89 轮真机：pinyin.dict_cache 指到一个建不出来的路径（父路径是个文件）时，
# 索引每次启动都在内存里重建（约 2 秒），而体检照样 `[ok ] 拼音索引 56,413 键 / 2.1s`
# 并在一堆绿行里藏一句「写缓存失败（不影响使用）」—— 那句「不影响使用」是假话。
_cache_dir = PROJ / ".tmp" / "doctor-idxcache"
shutil.rmtree(_cache_dir, ignore_errors=True)
_cache_dir.mkdir(parents=True, exist_ok=True)
_cache_cfg = _cache_dir / "alt.json"
_cache_cfg.write_text(
    '{"pinyin": {"dict_cache": ".tmp/doctor-idxcache/blocker/idx.pkl"},'
    ' "output": {"tray": false, "log_file": ".tmp/doctor-idxcache/alt.log"},'
    ' "translate": {"enabled": false}}',
    encoding="utf-8",
)
(_cache_dir / "blocker").write_text("我是个文件", encoding="utf-8")
try:
    _p = subprocess.run(
        [sys.executable, "main.py", "--doctor", "--doctor-quick", "--config", str(_cache_cfg)],
        cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )
    _out = _p.stdout or ""
    _cache_line = next(
        (ln.strip() for ln in _out.splitlines()
         if "索引缓存" in ln and ("[FAIL]" in ln or "[ok ]" in ln or "[info]" in ln)),
        "",
    )
    check(_p.returncode != 0, "索引缓存写不进去时体检退出码非 0", f"exit={_p.returncode}")
    check("[FAIL]" in _cache_line and "写不进去" in _cache_line,
          "「索引缓存」那行是 FAIL 并说清写不进去", _cache_line)
    check("重建索引" in _cache_line, "并说清代价：每次启动都要重建索引", _cache_line)
    check("不影响使用" not in _out, "不再出现「不影响使用」这种假话")
finally:
    shutil.rmtree(_cache_dir, ignore_errors=True)
    REPORT.unlink(missing_ok=True)

print()
print("=" * 74)
print("体检和 --check 对同一份配置必须给同一个结论（第 100 轮）")
print("=" * 74)
# 第 100 轮真机：`--doctor --doctor-quick --config .tmp\r100\nope.json`（那份文件
# 不存在）返回 0、[0] 还打一行 `[ok ] 配置文件 …nope.json`；而同一行命令换成
# `--check` 是 rc 1 + `[FAIL] 配置文件 …不存在`。用户拿哪份报告都行，可两份互相
# 打脸。同理，值写错（整段类型不对 → 退回默认值）在 --check 是 FAIL，体检却只报
# 一句「配置拼写 没有不认识的键」。
check("[ok ] 写错的选项" in out and "没有写错的值" in out,
      "干净配置下体检报「写错的选项：没有写错的值」")

_miss_dir = PROJ / ".tmp" / "doctor-missingcfg"
shutil.rmtree(_miss_dir, ignore_errors=True)
_miss_dir.mkdir(parents=True, exist_ok=True)
_missing_cfg = _miss_dir / "nope.json"
try:
    _p = subprocess.run(
        [sys.executable, "main.py", "--doctor", "--doctor-quick",
         "--config", str(_missing_cfg)],
        cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180,
    )
    _out = _p.stdout or ""
    _cfg_line = next(
        (ln.strip() for ln in _out.splitlines()
         if "nope.json" in ln and ("[FAIL]" in ln or "[ok ]" in ln or "[info]" in ln)),
        "",
    )
    check(_p.returncode == 1, "指定的配置读不到时体检退出码 1（和 --check 一致）",
          f"exit={_p.returncode}")
    check("[FAIL]" in _cfg_line and "读不到" in _cfg_line,
          "那一行是 FAIL 并说清「你指定了这一份，但读不到」", _cfg_line)
    check("下面体检的是内置默认值" in _out, "并说明下面体检的是内置默认值")
finally:
    shutil.rmtree(_miss_dir, ignore_errors=True)
    REPORT.unlink(missing_ok=True)

_bad_dir = PROJ / ".tmp" / "doctor-badtype"
shutil.rmtree(_bad_dir, ignore_errors=True)
_bad_dir.mkdir(parents=True, exist_ok=True)
_bad_cfg = _bad_dir / "types.json"
_bad_cfg.write_text('{"hotkeys": 5}', encoding="utf-8")
try:
    _p = subprocess.run(
        [sys.executable, "main.py", "--doctor", "--doctor-quick",
         "--config", str(_bad_cfg)],
        cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180,
    )
    _out = _p.stdout or ""
    _prob_line = next(
        (ln.strip() for ln in _out.splitlines()
         if "写错的选项" in ln and ("[FAIL]" in ln or "[ok ]" in ln or "[info]" in ln)),
        "",
    )
    check(_p.returncode == 1, "配置里有写错的值时体检退出码 1", f"exit={_p.returncode}")
    check("[FAIL]" in _prob_line and "hotkeys" in _prob_line
          and "现在是 5" in _prob_line,
          "「写错的选项」那行点名哪一项、现在是什么值", _prob_line)
    # 只拿文件里的原始 dict 去 sanitize 时，没写进文件的键全是 None，会报出一片
    # 「现在是 null」的假错 —— 所以这里专门盯一句：默认配置下不许出现 null 的指控。
    check("现在是 null" not in _out, "没写进文件的键不会被报成「现在是 null」")
finally:
    shutil.rmtree(_bad_dir, ignore_errors=True)
    REPORT.unlink(missing_ok=True)

# 第 116 轮：JSON 写坏（「读不懂」）和「值写错」是两件事 —— config.load 遇到坏
# JSON 不抛异常，而是警告一句、整份改用内置默认值。体检以前没有一项盯这个分支，
# 于是拿默认值去校验，报出「没有 DEFAULTS 不认识的键」「没有写错的值」、退出码 0，
# 而 --check 对同一份配置是 [FAIL] 配置能读。两份报告互相打脸。
_broken_dir = PROJ / ".tmp" / "doctor-broken"
shutil.rmtree(_broken_dir, ignore_errors=True)
_broken_dir.mkdir(parents=True)
_broken_cfg = _broken_dir / "broken.json"
_broken_cfg.write_text('{"hotkeys": {"quit": "ctrl+alt+q",},}', encoding="utf-8")
try:
    _p = subprocess.run(
        [sys.executable, "main.py", "--doctor", "--doctor-quick",
         "--config", str(_broken_cfg)],
        cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180,
    )
    _out = _p.stdout or ""
    _all = _out + (_p.stderr or "")
    _read_line = next((ln.strip() for ln in _out.splitlines() if "配置能读" in ln), "")
    _spell_line = next((ln.strip() for ln in _out.splitlines() if "配置拼写" in ln), "")
    check(_p.returncode == 1, "配置读不懂时体检退出码 1（和 --check 的结论一致）",
          f"exit={_p.returncode}")
    check("[FAIL]" in _read_line and "读不懂" in _read_line and "Expecting" in _read_line,
          "「配置能读」那行点名坏在哪（把解析错误原样带上）", _read_line)
    check("[info]" in _spell_line and "没检查" in _spell_line,
          "读不懂时明说「配置拼写没检查」，不假装查过", _spell_line)
    check("没有写错的值" not in _out,
          "读不懂时不再说「没有写错的值」（那是在夸默认值）")
    check(_all.count("[config] 读取") == 1,
          "同一句「读取失败」只念一遍（报告里不重复）",
          str(_all.count("[config] 读取")))
finally:
    shutil.rmtree(_broken_dir, ignore_errors=True)
    REPORT.unlink(missing_ok=True)

print()
print("=" * 74)
passed = sum(1 for ok, _ in results if ok)
print(f"结果：{passed}/{len(results)} 通过")
for ok, label in results:
    if not ok:
        print(f"  未通过：{label}")
print("=" * 74)
sys.exit(0 if passed == len(results) else 1)
