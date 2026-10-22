# -*- coding: utf-8 -*-
"""拼音索引缓存（`pinyin_index.pkl`）自己从没被测试过 —— 而它决定「第二次启动多快」。

`t_ime/pinyin.py` 的 `load_or_build_index` 干三件事：命中就直接读、过期就重建、
读不出来就重建。这三条路以前只被「所有测试都调它」间接踩到，**缓存坏了会怎样、
依赖换了会怎样**一次都没验过。第 42 轮顺手发现一个真问题：tag 里只有「版本号 +
参数」，于是 `pip install -U jieba`（词频表变了）或换一份 `data/dict.txt` 之后，
旧缓存依旧算命中，引擎会用旧词频排候选，而 README 的准确率是拿新依赖量的。

修法：`_dep_signature()` 把 jieba 词库的路径/大小/mtime 与 pypinyin 版本塞进 tag。
代价是升级依赖后第一次启动会重建一次索引（本机约 10 秒），之后照旧命中。

用小参数跑（`min_freq=20000, max_word_len=2`，一次 0.6 秒），只验缓存语义，
不碰仓库根那份真索引。
"""
from __future__ import annotations

import contextlib
import io
import pickle
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
sys.path.insert(0, str(PROJ))

import t_ime.pinyin as P  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
MIN_FREQ = 20000
MAX_LEN = 2


def fix_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    mark = "ok  " if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not ok:
        line += f"  ({detail})"
    print(line, flush=True)


def call(cache: Path, *, verbose: bool = True, **kw):
    """调 load_or_build_index，把这个进程里它打印的东西抓下来。"""
    argv = {"min_freq": kw.pop("min_freq", MIN_FREQ),
            "max_word_len": kw.pop("max_word_len", MAX_LEN)}
    argv.update(kw)
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        t0 = time.time()
        index = P.load_or_build_index(cache, verbose=verbose, **argv)
        dt = time.time() - t0
    finally:
        sys.stdout = old
    return index, buf.getvalue(), dt


def main() -> int:
    fix_console_encoding()
    print("== 第 42 轮：拼音索引缓存 ==", flush=True)
    root = Path(tempfile.mkdtemp(prefix="t-ime-cache-"))
    cache = root / "pinyin_index.pkl"
    try:
        # 节 1：tag 本身
        h = P._params_hash(MIN_FREQ, MAX_LEN)
        check("tag 是 12 位十六进制", len(h) == 12 and all(c in "0123456789abcdef" for c in h), h)
        check("同样的参数算出同样的 tag", P._params_hash(MIN_FREQ, MAX_LEN) == h)
        check("改了 min_freq 就算不同 tag", P._params_hash(MIN_FREQ + 1, MAX_LEN) != h)
        check("改了 max_word_len 就算不同 tag", P._params_hash(MIN_FREQ, MAX_LEN + 1) != h)

        sig = P._dep_signature()
        check("依赖指纹里有 jieba 词库", sig.startswith("dict:") or "|dict:" in sig, sig)
        check("依赖指纹里有 pypinyin 版本", "pypinyin:" in sig and len(sig.split("pypinyin:")[1]) > 0, sig)
        check("依赖指纹算两次一样（词库没动）", P._dep_signature() == sig)
        dict_path = P._jieba_dict_path()
        check("依赖指纹用的是真词库文件名",
              dict_path is not None and f"dict:{dict_path.name}:" in sig, f"{sig} / {dict_path}")
        check("依赖指纹换了词库就会变（词库大小/时间在指纹里）",
              "dict.txt" in sig and str(dict_path.stat().st_size) in sig)

        # 节 2：第一次建、第二次命中
        idx1, out1, dt1 = call(cache)
        check("第一次调用把缓存写出来了", cache.exists())
        check("第一次走的是「重建」", "索引已缓存到" in out1, out1[-200:])
        check("重建出来的索引非空", len(idx1) > 0, f"{len(idx1)} 键")
        check("重建花了可测量的时间（不是假的）", dt1 > 0.05, f"{dt1:.3f}s")

        idx2, out2, dt2 = call(cache)
        check("第二次报「命中缓存」", "命中缓存" in out2, out2)
        check("命中缓存比重建快得多", dt2 < max(dt1 / 3.0, 0.1), f"hit={dt2:.3f}s build={dt1:.3f}s")
        check("命中拿到的索引和重建的一模一样", idx2 == idx1)
        check("缓存文件里存的就是这个 tag", pickle.loads(cache.read_bytes()).get("tag") == h)

        # 节 3：缓存坏了必须重建，不能崩
        cache.write_bytes(b"this is not a pickle")
        idx3, out3, _ = call(cache)
        check("坏缓存报「缓存不可用」而不是抛异常", "缓存不可用" in out3, out3[-200:])
        check("坏缓存之后重建成功", idx3 == idx1)

        cache.write_bytes(b"")
        _, out4, _ = call(cache)
        check("空文件也能重建", "缓存不可用" in out4 and cache.stat().st_size > 0)

        cache.write_bytes(pickle.dumps([1, 2, 3], protocol=4))
        _, out5, _ = call(cache)
        check("pickle 出来的不是 dict 也能重建", "结果不可用" in out5 or "缓存不可用" in out5, out5[-200:])

        cache.write_bytes(pickle.dumps({"tag": "deadbeefdead", "index": {}}, protocol=4))
        _, out6, _ = call(cache)
        check("tag 不对时报「缓存已过期」", "缓存已过期" in out6, out6[-200:])

        cache.write_bytes(pickle.dumps({"tag": h}, protocol=4))
        _, out7, _ = call(cache)
        check("tag 对但缺 index 字段也能重建", "缓存不可用" in out7, out7[-200:])

        # 节 4：依赖指纹真的参与判断
        orig_sig = P._dep_signature
        try:
            P._dep_signature = lambda: "frozen-for-test"  # type: ignore[assignment]
            call(cache)
        finally:
            P._dep_signature = orig_sig  # type: ignore[assignment]
        frozen = pickle.loads(cache.read_bytes())
        check("「冻结指纹」下写出的 tag 与真指纹不同",
              frozen.get("tag") != P._params_hash(MIN_FREQ, MAX_LEN))
        _, out8, _ = call(cache)
        check("真指纹回来之后，冻结的缓存被判过期（依赖在 tag 里）",
              "缓存已过期" in out8, out8[-200:])

        # 节 5：写不进去也不能影响使用
        blocker = root / "a-file-in-the-way"
        blocker.write_text("x", encoding="utf-8")
        bad_cache = blocker / "deep" / "index.pkl"
        idx9, out9, _ = call(bad_cache)
        check("缓存目录建不出来时只警告不抛", "写不进去" in out9 and "下次启动" in out9, out9[-200:])
        # 第 89 轮：光在日志里说一句不够，--check / --doctor 要能把「没落盘、
        # 下次还得重建」当成失败报出来，所以模块得把原因留在 LAST_CACHE_ERROR 里。
        check("写不进去时 LAST_CACHE_ERROR 记下了原因", bool(P.LAST_CACHE_ERROR), P.LAST_CACHE_ERROR)
        check("写不进去也照样返回可用索引", idx9 and len(idx9) > 0)

        # 节 6：残留的 .tmp 不碍事，而且会被换掉
        nested = root / "sub" / "deep" / "index.pkl"
        leftover = nested.with_suffix(".tmp")
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_bytes(b"half-written garbage")
        _, out10, _ = call(nested)
        check("自动建出缺失的父目录", nested.exists())
        check("残留的 .tmp 没被误当成缓存", "索引已缓存到" in out10, out10[-200:])
        check("写完缓存后 .tmp 不留下", not leftover.exists())
        check("成功写缓存后 LAST_CACHE_ERROR 清空", P.LAST_CACHE_ERROR == "", P.LAST_CACHE_ERROR)

        # 节 7：安静模式真的安静
        quiet = root / "quiet" / "index.pkl"
        quiet.parent.mkdir(parents=True, exist_ok=True)
        call(quiet)
        _, out11, _ = call(quiet, verbose=False)
        check("verbose=False 时不打印任何东西", out11.strip() == "", repr(out11[:120]))

        # 节 7b（第 118 轮）：缺依赖时不许把好缓存说成「过期」
        # 用户拿系统的 python 跑 --check 时，依赖其实在 .venv 里，tag 退化成
        # dict:na|pypinyin:na，于是一份好缓存被算成「词库或参数变了」，把用户
        # 引去查词库版本。真实原因是「这台解释器读不到词库」。
        check("这台机器依赖齐全（missing_dependencies 为空）",
              P.missing_dependencies() == [], str(P.missing_dependencies()))
        stale = root / "stale" / "index.pkl"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(pickle.dumps({"tag": "deadbeefdead", "index": {}}, protocol=4))
        orig_missing = P.missing_dependencies
        orig_build = P.build_index

        def _boom(**kw):  # noqa: ANN003
            raise ModuleNotFoundError("No module named 'pypinyin'", name="pypinyin")

        buf = io.StringIO()
        old_out = sys.stdout
        try:
            P.missing_dependencies = lambda: ["pypinyin"]  # type: ignore[assignment]
            P.build_index = _boom  # type: ignore[assignment]
            sys.stdout = buf
            try:
                P.load_or_build_index(stale, min_freq=MIN_FREQ, max_word_len=MAX_LEN, verbose=True)
            except ModuleNotFoundError:
                pass
        finally:
            sys.stdout = old_out
            P.missing_dependencies = orig_missing  # type: ignore[assignment]
            P.build_index = orig_build  # type: ignore[assignment]
        out12 = buf.getvalue()
        check("缺依赖时说「缺依赖」", "缺依赖" in out12, out12[-220:])
        check("缺依赖时不说「缓存已过期」", "缓存已过期" not in out12, out12[-220:])
        check("缺依赖时点明「不是它过期，是环境不对」", "环境不对" in out12, out12[-220:])
        check("缺依赖时给出 .venv 的安装命令", ".venv\\Scripts\\python.exe" in out12, out12[-220:])
        # 依赖齐全时，tag 不对仍旧该说「过期」（别把老说法改没了）
        check("依赖齐全 + tag 不对，照旧报「缓存已过期」", "缓存已过期" in out6, out6[-120:])
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # 节 8：--build-index 写不进缓存时不能说「成功」（第 89 轮）
    print()
    print("=" * 74)
    print("节 8：--build-index 的活儿就是重建缓存，写不进去就得报失败")
    print("=" * 74)
    # 真机：pinyin.dict_cache 指到一个建不出来的路径时，这条命令照样打印
    # 「索引 56,413 个键，耗时 2.1s」并 rc 0 —— 用户以为缓存更新了，
    # 其实退出后什么都没留下，下次启动还得再建一遍。
    import main as main_mod  # noqa: E402

    cli_root = Path(tempfile.mkdtemp(prefix="idxcli-"))
    try:
        (cli_root / "blocker").write_text("我是文件", encoding="utf-8")
        bad_cfg = cli_root / "bad.json"
        good_cfg = cli_root / "good.json"
        common = '"output": {"tray": false, "log_file": "%s"}' % (
            (cli_root / "alt.log").as_posix(),
        )
        bad_cfg.write_text(
            '{"pinyin": {"min_freq": 20000, "max_word_len": 2, "dict_cache": "%s"}, %s}'
            % ((cli_root / "blocker" / "idx.pkl").as_posix(), common),
            encoding="utf-8",
        )
        good_cfg.write_text(
            '{"pinyin": {"min_freq": 20000, "max_word_len": 2, "dict_cache": "%s"}, %s}'
            % ((cli_root / "ok" / "idx.pkl").as_posix(), common),
            encoding="utf-8",
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_bad = main_mod.main(["--build-index", "--config", str(bad_cfg)])
        out_bad = buf.getvalue()
        check("★ 缓存写不进去时 --build-index 退出码 1", rc_bad == 1, f"rc={rc_bad}")
        # 第 123 轮起，这条命令改成「建到临时文件、成功才替换」：写不进去时
        # 说的不再是「退出后不会留下缓存」（那只描述了现象），而是「旧索引没动」。
        check("并说清「旧索引没动」", "旧索引没动" in out_bad, out_bad[-200:])
        check("不再先说「已删除旧索引」（旧缓存不许先被删掉）",
              "已删除旧索引" not in out_bad, out_bad[-200:])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_good = main_mod.main(["--build-index", "--config", str(good_cfg)])
        out_good = buf.getvalue()
        check("缓存写得进去时 rc 0", rc_good == 0, f"rc={rc_good} / {out_good[-120:]}")
        check("而且缓存文件真的出现了", (cli_root / "ok" / "idx.pkl").exists())
    finally:
        shutil.rmtree(cli_root, ignore_errors=True)

    # 节 9：--build-index 不许先把好缓存删掉（第 123 轮）
    print()
    print("=" * 74)
    print("节 9：--build-index 要先确认能建，再动旧缓存")
    print("=" * 74)
    # 真机：用系统那个 python（没装 jieba / pypinyin）跑 `python main.py --build-index`，
    # 输出是「已删除旧索引 pinyin_index.pkl」+ 一段 ModuleNotFoundError traceback，
    # 2 MB 的好缓存就这么没了 —— 用户只是想重建索引，结果连原来能用的都没了。
    cli_root2 = Path(tempfile.mkdtemp(prefix="idxcli2-"))
    try:
        cache_file = cli_root2 / "idx.pkl"
        cache_file.write_bytes(b"OLD-CACHE-SENTINEL")  # 一份「好缓存」的占位
        cfg = cli_root2 / "cfg.json"
        common = '"output": {"tray": false, "log_file": "%s"}' % (
            (cli_root2 / "alt.log").as_posix(),
        )
        cfg.write_text(
            '{"pinyin": {"min_freq": 20000, "max_word_len": 2, "dict_cache": "%s"}, %s}'
            % (cache_file.as_posix(), common),
            encoding="utf-8",
        )

        orig_missing = P.missing_dependencies  # type: ignore[assignment]
        try:
            P.missing_dependencies = lambda: ["pypinyin"]  # type: ignore[assignment]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc_missing = main_mod.main(["--build-index", "--config", str(cfg)])
            out_missing = buf.getvalue()
        finally:
            P.missing_dependencies = orig_missing  # type: ignore[assignment]
        check("★ 缺依赖时 --build-index 退出码 1（不是 traceback）",
              rc_missing == 1, f"rc={rc_missing}")
        check("缺依赖时不出现 traceback",
              "Traceback" not in out_missing, out_missing[-200:])
        check("根因是缺依赖：点名缺什么", "缺依赖" in out_missing and "pypinyin" in out_missing,
              out_missing[-240:])
        check("并给出项目解释器的安装命令",
              ".venv\\Scripts\\python.exe" in out_missing, out_missing[-240:])
        check("★ 缺依赖时旧缓存一个字节都没动",
              cache_file.read_bytes() == b"OLD-CACHE-SENTINEL", repr(cache_file.read_bytes()[:40]))
        check("也没留下临时文件", not cache_file.with_name(cache_file.name + ".rebuild").exists())

        # 参数把索引筛成空的（min_freq 太大）时：空索引换上去等于让打字没有候选，
        # 旧缓存必须留着，退出码 1 并说清是哪个参数。
        empty_cfg = cli_root2 / "empty.json"
        empty_cfg.write_text(
            '{"pinyin": {"min_freq": 100000000, "max_word_len": 2, "dict_cache": "%s"}, %s}'
            % (cache_file.as_posix(), common),
            encoding="utf-8",
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_empty = main_mod.main(["--build-index", "--config", str(empty_cfg)])
        out_empty = buf.getvalue()
        check("★ 建出空索引时退出码 1", rc_empty == 1, f"rc={rc_empty}")
        check("并点名 pinyin.min_freq 是嫌疑参数", "pinyin.min_freq" in out_empty,
              out_empty[-240:])
        check("★ 空索引不替换旧缓存", cache_file.read_bytes() == b"OLD-CACHE-SENTINEL",
              repr(cache_file.read_bytes()[:40]))
        check("也不留临时文件", not cache_file.with_name(cache_file.name + ".rebuild").exists())

        # 成功路径：临时文件建好 → 原子替换 → 收尾干净
        ok_cfg = cli_root2 / "ok.json"
        ok_cache = cli_root2 / "ok" / "idx.pkl"
        ok_cfg.write_text(
            '{"pinyin": {"min_freq": 20000, "max_word_len": 2, "dict_cache": "%s"}, %s}'
            % (ok_cache.as_posix(), common),
            encoding="utf-8",
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_ok = main_mod.main(["--build-index", "--config", str(ok_cfg)])
        out_ok = buf.getvalue()
        check("建成功时 rc 0", rc_ok == 0, f"rc={rc_ok} / {out_ok[-160:]}")
        check("并明说「已换上新的」", "已换上新的" in out_ok, out_ok[-160:])
        check("缓存文件真的出现了", ok_cache.exists())
        check("临时文件没留下", not ok_cache.with_name(ok_cache.name + ".rebuild").exists())
    finally:
        shutil.rmtree(cli_root2, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print()
    print(f"结果：{passed}/{len(RESULTS)} 通过", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
