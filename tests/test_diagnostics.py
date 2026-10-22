# -*- coding: utf-8 -*-
"""``main.py`` 那几个「查问题用」的开关本身有没有毛病。

为什么单独一个测试：用户以为程序坏了的时候，唯一能自己拿到结论的路就是
``--doctor`` / ``--tray-check`` / ``--hook-check`` / ``--autostart-status`` /
``--show-config`` / ``--check``。这些命令**不是**给开发者用的，是给「双击了没反应」
的人用的 —— 所以它们自己出问题（跑不起来、退出码胡说、修饰参数被吃掉）比功能坏了
更严重：用户会照着它们的结论去修一个不存在的问题。

这一轮真机撞到的第一件事就很典型：``python main.py --doctor-quick``（照着 README
第 387 行「加 --doctor-quick 会跳过三项」的说法，把 ``--doctor`` 省掉了）——
那一行**谁都不认领**，于是 argparse 解析完、所有分支都不匹配，流程一路走到最后，
**程序当场当常驻实例跑了起来**：它去试装托盘（低完整性下失败）、打印
「不影响打字，Ctrl+Alt+Q 仍可退出」，然后占着控制台永远不退出。用户想体检，
得到的是一个输入法进程。同族的还有 ``--hook-check-seconds`` / ``--demo-seconds`` /
``--doctor-seconds`` 单独出现 —— 全都会被静默忽略然后「照常启动」。

覆盖的东西：

  1. ``--doctor-quick`` 单独出现就进体检（退出码 0、两秒内、不走「启动实例」那条路）。
  2. ``--doctor`` / ``--doctor-seconds N`` 也都进体检，且 ``--doctor-quick`` 真的把
     probe_tray / probe_hook / pipeline 三项关掉（用打桩的 ``run_doctor`` 看实参，
     不去真跑那三项要动系统的检查）。
  3. ``--hook-check-seconds`` / ``--demo-seconds`` 单独出现 → 退出码 2 + 一句人话，
     而不是被忽略后启动实例。
  4. ``--tray-check``：本会话是 Low 完整性，退出码必须是 2（不是 0 也不是 1），
     报告要说明「完整性级别偏低 / UIPI」，并写出 ``tray-check.txt``。
  5. ``--hook-check --hook-check-seconds 1``：没人敲键时必须如实说「没收到真实按键」
     并退出码 6（不是 0 —— 0 代表「看到你敲的键了」）。
  6. ``--autostart-status`` / ``--show-config`` / ``--check`` 三个只读开关能跑完、
     输出里有关键结论。

直接跑：``python tests/test_diagnostics.py``（约 10 秒，会短暂弹一个钩子自检窗口）
"""
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from t_ime.console import fix_console_encoding  # noqa: E402

fix_console_encoding()

OK = "[OK]  "
BAD = "[FAIL]"
results: list[tuple[bool, str]] = []


def check(cond, label, detail=""):
    results.append((bool(cond), label))
    print(f"{OK if cond else BAD} {label}" + (f"  — {detail}" if detail else ""))
    return cond


def run_main(args: list[str], timeout: float = 120.0):
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "main.py"] + args,
        cwd=str(PROJ), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        stdin=subprocess.PIPE, timeout=timeout,
    )
    return proc, time.time() - t0


DOCTOR_REPORT = PROJ / "translate-ime-doctor.txt"
TRAY_REPORT = PROJ / "tray-check.txt"
for stale in (DOCTOR_REPORT, TRAY_REPORT):
    if stale.exists():
        stale.unlink()

# ============================================================
print("=" * 74)
print("1. --doctor-quick 单独出现（README 就是这么写的，不许变成「启动实例」）")
print("=" * 74)
proc, dt = run_main(["--doctor-quick"])
out, err = proc.stdout or "", proc.stderr or ""
joined = out + err
check(proc.returncode == 0, "退出码 0（有 FAIL 才是 1；真启动实例的话它不会退出）",
      f"exit={proc.returncode}")
check(dt < 60.0, "60 秒内跑完（README 承诺「两秒内出结果」）", f"{dt:.1f}s")
check("跳过端到端" in joined, "--doctor-quick 真的跳过了 [7] 端到端")
check("跳过真装托盘" in joined or "跳过" in joined and "托盘" in joined,
      "跳过了试装托盘那一项")
check("[app]" not in joined,
      "没有走到「启动实例」那条路（输出里不该出现 App 的 [app] 日志）",
      joined.split("[app]")[1][:80].strip() if "[app]" in joined else "")
check("报告已写入" in joined, "写下了体检报告", DOCTOR_REPORT.name)
check(DOCTOR_REPORT.exists() and DOCTOR_REPORT.stat().st_size > 200,
      "translate-ime-doctor.txt 真的落盘了（不是只说一句）",
      f"{DOCTOR_REPORT.stat().st_size if DOCTOR_REPORT.exists() else 0} 字节")
if DOCTOR_REPORT.exists():
    rep = DOCTOR_REPORT.read_text(encoding="utf-8", errors="replace")
    check("体检" in rep or "doctor" in rep.lower(), "报告里有标题")

# ============================================================
print()
print("=" * 74)
print("2. 三个修饰参数都认领到 run_doctor，且 --doctor-quick 关掉三项要动系统的探测")
print("=" * 74)
import main as M  # noqa: E402

calls: list[dict] = []
real_run_doctor = M.run_doctor


def fake_doctor(cfg, root_dir, W, **kw):
    calls.append(kw)
    return 0


M.run_doctor = fake_doctor
try:
    for argv, want_probe in ((["--doctor"], True),
                             (["--doctor-quick"], False),
                             (["--doctor-seconds", "5"], True)):
        calls.clear()
        rc = M.main(argv)
        kw = calls[0] if calls else {}
        check(rc == 0 and bool(calls), f"{' '.join(argv)} 会进体检", f"exit={rc} calls={len(calls)}")
        check(kw.get("probe_tray") is want_probe and kw.get("probe_hook") is want_probe
              and kw.get("pipeline") is want_probe,
              f"{' '.join(argv)} 的 probe_tray/probe_hook/pipeline 都是 {want_probe}",
              str({k: kw.get(k) for k in ("probe_tray", "probe_hook", "pipeline")}))
    calls.clear()
    rc = M.main(["--doctor-seconds", "5"])
    check(calls and calls[0].get("seconds") == 5.0, "--doctor-seconds 5 真的传成了 5.0 秒",
          str(calls[0].get("seconds") if calls else None))

    for argv, flag in ((["--hook-check-seconds", "5"], "--hook-check-seconds"),
                       (["--demo-seconds", "5"], "--demo-seconds")):
        calls.clear()
        rc = M.main(argv)
        check(rc == 2, f"{' '.join(argv)} 单独出现时退出码 2（不是 0 也不是 3）", f"exit={rc}")
        check(not calls, f"{' '.join(argv)} 没有偷偷跑体检")
finally:
    M.run_doctor = real_run_doctor

proc, dt = run_main(["--hook-check-seconds", "5"])
check(proc.returncode == 2, "（子进程复核）--hook-check-seconds 单独出现退出码 2",
      f"exit={proc.returncode}")
msg = (proc.stdout or "") + (proc.stderr or "")
check("--hook-check-seconds" in msg and "--hook-check" in msg,
      "（子进程复核）说清了它该跟谁一起用")
check(dt < 15.0, "（子进程复核）立刻退出，没有变成常驻实例", f"{dt:.1f}s")

# ============================================================
print()
print("=" * 74)
print("3. --tray-check：低完整性下必须给出 [2] 和结论，不能谎报成功")
print("=" * 74)
proc, dt = run_main(["--tray-check"])
out, err = proc.stdout or "", proc.stderr or ""
joined = out + err
check(proc.returncode in (0, 2, 3, 4), "退出码是文档里那几种之一",
      f"exit={proc.returncode}")
check("完整性级别" in joined, "报告了进程完整性级别")
# 第 120 轮：这一行以前只写「pid」，读的人会以为是输入法实例的进程号；
# 它其实是「跑这条检查的进程」，每次跑都换。
check("诊断进程 pid" in joined,
      "[1] 的 pid 行写明是「诊断进程」（不是输入法实例）")
check("不是输入法实例" in joined or "跑这条检查的进程" in joined,
      "并解释了它和常驻实例的区别")
if "低" in joined:
    check(proc.returncode == 2, "完整性低（Low）时退出码正好是 2（不是 0）",
          f"exit={proc.returncode}")
    check("UIPI" in joined or "explorer" in joined,
          "说明了低完整性 / UIPI 这条真实原因（不是只说「失败了」）")
check(TRAY_REPORT.exists(), "写出了 tray-check.txt 报告", TRAY_REPORT.name)
if TRAY_REPORT.exists():
    rep = TRAY_REPORT.read_text(encoding="utf-8", errors="replace")
    check("托盘" in rep or "tray" in rep.lower(), "报告里有托盘相关内容")
    check(len(rep) > 100, "报告不是空壳", f"{len(rep)} 字")

# ============================================================
print()
print("=" * 74)
print("3b. 托盘线程根本没起来时：[3] 不许说 start() 返回 True，[4] 也不许甩锅给 UIPI")
print("=" * 74)
# 第 74 轮：以前 [3] 把「start() 返回」写成固定字符串 "True"，而 start() 在新版
# 里如实返回「图标到底装上没有」。托盘线程起不来时这两行会一起说谎，把用户引去
# 查完整性级别 / UIPI —— 方向全错。这里用打桩把线程起不来那一刻复现出来。
import contextlib
import io

import main as main_mod
from t_ime import config as config_mod
from t_ime import tray as tray_mod
from t_ime import winapi as W  # noqa: E402

_cfg, _ = config_mod.load()
_orig_start = tray_mod.TrayIcon.start


def _fake_start(self):
    # 模拟「托盘线程根本没起来」：NIM_ADD 从未执行，start() 明确返回 False
    self._last_error = "（测试桩）托盘线程起来失败"
    self._thread = None
    self._started = True
    return False


tray_mod.TrayIcon.start = _fake_start
try:
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        _rc = main_mod.run_tray_check(_cfg, PROJ, W)
    _text = _buf.getvalue()
finally:
    tray_mod.TrayIcon.start = _orig_start

check(_rc == 4, "线程没起来时退出码是 4（不是把锅甩给完整性级别）", f"exit={_rc}")
_start_line = next((ln for ln in _text.splitlines() if "start() 返回" in ln), "")
check("False" in _start_line, "start() 返回 报的是实测 False，不是写死的 True",
      _start_line.strip())
_verdict = _text.split("[4] 结论")[-1]
check("托盘线程" in _verdict, "结论先指出托盘线程没起来", _verdict.strip().splitlines()[:1])
check("UIPI 会拦下" not in _verdict and "低于 explorer" not in _verdict,
      "结论没有甩锅给 UIPI / 完整性级别", _verdict.strip().splitlines()[:1])

# ============================================================
print()
print("=" * 74)
print("3c. output.tray=false 时：[0] 先说这是你自己关的，别试 NIM_ADD、别怪 UIPI")
print("=" * 74)
# 第 103 轮：托盘关着的时候程序启动时根本不装图标，可 --tray-check 以前照样
# 试一次 NIM_ADD，在低完整性会话里打出 ACCESS_DENIED，再下结论「这不是程序的
# 问题」—— 用户是自己关的，却被引去查 UIPI。体检（第 82 轮）修过同一个坑，
# 这条命令当时漏了。
import copy
import shutil

_cfg_off = copy.deepcopy(_cfg)
_cfg_off["output"]["tray"] = False
_tmp_root = PROJ / ".tmp" / "traycheck"
_tmp_root.mkdir(parents=True, exist_ok=True)
_orig_start2 = tray_mod.TrayIcon.start
_start_calls: list[int] = []


def _fake_start2(self):
    _start_calls.append(1)
    return False


tray_mod.TrayIcon.start = _fake_start2
try:
    _buf2 = io.StringIO()
    with contextlib.redirect_stdout(_buf2):
        _rc2 = main_mod.run_tray_check(_cfg_off, _tmp_root, W)
    _text2 = _buf2.getvalue()
finally:
    tray_mod.TrayIcon.start = _orig_start2

check(_rc2 == 0, "托盘是用户关的 → 退出码 0（不是 2）", f"exit={_rc2}")
check("output.tray" in _text2 and "你自己" in _text2,
      "[0] 先说「托盘是你自己关的」", _text2.splitlines()[3:6])
check(not _start_calls, "★ 没有试装图标（不碰 NIM_ADD）", f"start 被调用 {len(_start_calls)} 次")
check("[3] 实际试一次 NIM_ADD" not in _text2, "[3] 整段被跳过")
_verdict2 = _text2.split("[4] 结论")[-1].strip()
check("UIPI 会拦下" not in _verdict2 and "低于 explorer" not in _verdict2
      and "不是故障" in _verdict2,
      "结论没有甩锅给 UIPI / 完整性级别（说的是「这不是故障」）",
      _verdict2.splitlines()[:1])
check((_tmp_root / "tray-check.txt").exists(), "报告照样写出来（写进指定的根目录）")
shutil.rmtree(_tmp_root, ignore_errors=True)

# ============================================================
print()
print("=" * 74)
print("3d. 已经有实例在跑时：--hook-check 要说「它挡在前面」，不许怪安全软件")
print("=" * 74)
# 第 104 轮：常驻实例的钩子比本检查先装上，字母键被它吞掉（回调返回非 0，
# 事件不再往下传）—— 所以本检查量到「真实按键 0」是它造成的，跟安全软件 /
# 提权窗口 / 远程桌面都没关系。以前这种情况照样走完 12 秒再把人引去查环境。
from t_ime import daemon as daemon_mod  # noqa: E402

_orig_running = daemon_mod.is_running
_orig_read_status = daemon_mod.read_status
daemon_mod.is_running = lambda: True
daemon_mod.read_status = lambda root_dir: {"pid": 2468}
try:
    _buf3 = io.StringIO()
    with contextlib.redirect_stdout(_buf3):
        _rc3 = main_mod.run_hook_check(_cfg, PROJ, W, seconds=1.0)
    _text3 = _buf3.getvalue()
finally:
    daemon_mod.is_running = _orig_running
    daemon_mod.read_status = _orig_read_status

check(_rc3 == 8, "退出码 8（「本次没做成」，不是「钩子坏了」）", f"exit={_rc3}")
check("正在常驻" in _text3 and "2468" in _text3,
      "[0] 说出有实例在跑，并报出它的进程号",
      [ln for ln in _text3.splitlines() if "[0]" in ln])
check("吞掉" in _text3 and "不能" in _text3,
      "说清字母会被它吞掉、这时候的数字不能用来判断环境")
check("退出它" in _text3, "给出该怎么办（先退出那个实例再跑）")
check("钩子安装" not in _text3 and "[1]" not in _text3,
      "★ 于是根本没有装钩子、没有弹小窗口（不用白等 12 秒）")

# ============================================================
print()
print("=" * 74)
print("4. --hook-check：没人敲键时必须如实说「没收到真实按键」")
print("=" * 74)

# 第 76 轮：「没收到」到底指谁的按键，必须写在句子里。自检按键是自己注入的、
# 一定会回来，以前同一屏既说「一个按键都没收到」又说「自检按键回来了」。
_said = main_mod._hook_nothing_text(8.0, 2)
check("没收到**真实**按键" in _said and "2 个自检按键" in _said,
      "只收到自检按键时说「没收到真实按键」，并说出自检的个数", _said.strip())
check("一个按键都没收到" not in _said,
      "不会一边说「一个按键都没收到」一边报自检按键的个数")
_said0 = main_mod._hook_nothing_text(8.0, 0)
check("一个按键都没收到" in _said0 and "连自检按键都没回来" in _said0,
      "连自检都没回来时才说「一个按键都没收到」", _said0.strip())

proc, dt = run_main(["--hook-check", "--hook-check-seconds", "1"])
out, err = proc.stdout or "", proc.stderr or ""
joined = out + err
check("钩子安装" in joined, "报告了钩子安装这一项")
check("自检按键" in joined, "做了自检按键（不是只装钩子就宣布成功）")
check(proc.returncode == 6, "没人敲键时退出码 6（0 代表「真看到你敲的键了」）",
      f"exit={proc.returncode}")
check("真实" in joined, "把「注入的」和「真实的」按键分开数了", f"{dt:.1f}s")
if "自检 通过" in joined:
    check("没收到**真实**按键" in joined,
          "自检通过时，结论说的是「没收到真实按键」而不是「一个按键都没收到」")
    check("一个按键都没收到" not in joined,
          "同一屏不再自相矛盾（自检按键明明回来了）")
else:
    check("跳过" in joined or "一个按键都没收到" in joined,
          "自检没做成时如实说没做成（或干脆说一个都没收到）")

# ============================================================
print()
print("=" * 74)
print("4b. --hook-check 的说明不许说「不注入」（自检按键就是它注入的），退出码五档都要有出处")
print("=" * 74)
# 第 96 轮：窗口和 [3] 那两行原来写着「只观察，不吞键、不注入」，可同一屏的
# [2b] 是「自检按键回来了」、[4] 是「我们自己注入的 N 个」—— 自检按键就是
# hook.probe() 注入的。改后必须承认它会注入一个。
_help = main_mod._hook_check_help_text(3.0)
check("自检按键" in _help, "说明里承认开头会注入一个自检按键（它就是 hook.probe() 发的）",
      _help.strip()[:60])
check("不注入" not in _help, "说明里不再说「不注入」")
_src = (PROJ / "main.py").read_text(encoding="utf-8")
check("不注入" not in _src, "整个 main.py 里再也没有「不注入」这种假话")
check("return 5" in _src, "钩子装不上时确实返回 5（README 必须写这一档）")


class _FakeHook:
    """装不上的钩子：start() 直接 False，原因放在 error 里。"""

    def __init__(self, *args, **kwargs):
        self.error = "（测试桩）安全软件拦了 WH_KEYBOARD_LL"

    def start(self, timeout: float = 5.0) -> bool:
        return False


class _FakeW:
    @staticmethod
    def integrity_level():
        return (0x1000, "Low（低）")

    KeyboardHook = _FakeHook


import contextlib  # noqa: E402
import io  # noqa: E402

_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    _rc5 = main_mod.run_hook_check({}, PROJ, _FakeW, seconds=1, log_path=PROJ / "no-such.log")
_cap = _buf.getvalue()
check(_rc5 == 5, "钩子装不上时退出码 5（既不是 0 也不是 6）", f"rc={_rc5}")
check("装不上" in _cap and "WH_KEYBOARD_LL" in _cap,
      "退出码 5 这条路把原因说清楚了", _cap.strip()[-70:])
_readme = (PROJ / "README.md").read_text(encoding="utf-8")
check("固定的五档" in _readme and "`5` 连全局键盘钩子都装不上" in _readme,
      "README 的退出码清单写到了 5（以前写「四档」，漏了这一档）")
check("all five are covered" in _readme, "英文 README 同样写全五档")

# ============================================================
print()
print("=" * 74)
print("5. 三个只读开关：--autostart-status / --show-config / --check")
print("=" * 74)
proc, _ = run_main(["--autostart-status"])
out = (proc.stdout or "") + (proc.stderr or "")
check(proc.returncode == 0, "--autostart-status 退出码 0", f"exit={proc.returncode}")
check(any(w in out for w in ("未开启", "已开启")), "说出了开机自启的当前状态", out.strip()[-60:])

proc, _ = run_main(["--show-config"])
out = (proc.stdout or "") + (proc.stderr or "")
check(proc.returncode == 0, "--show-config 退出码 0", f"exit={proc.returncode}")
check("api_key" in out and "hotkeys" in out, "打印出了关键配置项")
check("来源" in out or "默认" in out, "说明了每个值的来源（帮助用户找「我改了没生效」）")

proc, _ = run_main(["--check"])
out = (proc.stdout or "") + (proc.stderr or "")
check(proc.returncode == 0, "--check 退出码 0", f"exit={proc.returncode}")
check("全部通过" in out, "--check 给出了结论句", out.strip().splitlines()[-1][:60] if out.strip() else "")

# ============================================================
for f in (DOCTOR_REPORT, TRAY_REPORT):
    if f.exists():
        f.unlink()

print()
print("=" * 74)
passed = sum(1 for ok, _ in results if ok)
total = len(results)
print(f"结果：{passed}/{total} 通过")
print("=" * 74)
for ok, label in results:
    if not ok:
        print(f"{BAD} {label}")
sys.exit(0 if passed == total else 1)
