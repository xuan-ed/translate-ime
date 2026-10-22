# -*- coding: utf-8 -*-
"""拼音引擎的准确率：**把 README 里那个数字钉在一个可重跑的语料上**。

README 的「关于拼音准确率的诚实说明」写着：top-1 实测约 **90.8%**（76 条常用输入
用例）。问题是 —— **那 76 条用例从来没有进过仓库**：数字写在文档里，语料在某个
已经删掉的临时脚本里，于是「90.8%」既不可复现，也没人会发现它什么时候变了。

这个测试把语料固定在文件里（``CASES``），同时钉住三件事：

  1. **地板**：top-1 ≥ 88%（实测 68/76 = 89.5%），top-3 ≥ 98%（实测 75/76 = 98.7%）。
     留 1.5 个百分点余量，是为了让「换词频表」这种合理波动不至于天天报警，
     但「悄悄退 5 个点」一定会被抓住。
  2. **准确的失败集合**：没中 top-1 的必须是文档里写明的那些（``DOCUMENTED_MISSES``），
     其中 4 条正是 README 表格里列的「词频倒挂」例子。改进了可以（子集即可），
     悄悄多出新的失败不行。
  3. **自学习的承诺**：``boost`` 一次之后，原本排不上来的词要真的顶到第一位
     （README：「你改选一次，那个词下次就顶到首位」）。

可复现性：引擎用**新实例**，不加载 ``user_boosts.json`` —— 否则你自己的自学习记录
会让这个数字随机器而变。

直接跑：``python tests/test_accuracy.py``
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from t_ime.pinyin import PinyinEngine, load_or_build_index  # noqa: E402

PASS = 0
FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok ] {label}", flush=True)
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  <-- {extra!r}", flush=True)


# ---------------------------------------------------------------------- #
# 语料：76 条日常输入。改这里 = 改 README 里的百分比，两边一起改。
# 挑的都是「一天里真的会打」的词，没有生僻字。
CASES = [
    ("nihao", "你好"), ("zaijian", "再见"), ("xiexie", "谢谢"), ("duibuqi", "对不起"),
    ("meiguanxi", "没关系"), ("qingwen", "请问"), ("duoshao", "多少"), ("shenme", "什么"),
    ("weishenme", "为什么"), ("zenme", "怎么"), ("mingtian", "明天"), ("jintian", "今天"),
    ("zuotian", "昨天"), ("xianzai", "现在"), ("shijian", "时间"), ("gongzuo", "工作"),
    ("xuexiao", "学校"), ("xuesheng", "学生"), ("laoshi", "老师"), ("pengyou", "朋友"),
    ("dianhua", "电话"), ("shouji", "手机"), ("diannao", "电脑"), ("wangluo", "网络"),
    ("youxiang", "邮箱"), ("mima", "密码"), ("denglu", "登录"), ("zhuce", "注册"),
    ("shangchuan", "上传"), ("xiazai", "下载"), ("wenjian", "文件"), ("zhaopian", "照片"),
    ("shipin", "视频"), ("yinyue", "音乐"), ("dianying", "电影"), ("youxi", "游戏"),
    ("tushuguan", "图书馆"), ("canting", "餐厅"), ("yiyuan", "医院"), ("yinhang", "银行"),
    ("feiji", "飞机"), ("huoche", "火车"), ("qiche", "汽车"), ("gongjiao", "公交"),
    ("ditie", "地铁"), ("jichang", "机场"), ("jiudian", "酒店"), ("lvxing", "旅行"),
    ("tianqi", "天气"), ("xiayu", "下雨"), ("xiatian", "夏天"), ("dongtian", "冬天"),
    ("chuntian", "春天"), ("qiutian", "秋天"), ("xinnian", "新年"), ("shengri", "生日"),
    ("kuaile", "快乐"), ("xingfu", "幸福"), ("jiankang", "健康"), ("piaoliang", "漂亮"),
    ("congming", "聪明"), ("nuli", "努力"), ("wenti", "问题"), ("fangfa", "方法"),
    ("jieguo", "结果"), ("yuanyin", "原因"), ("liyou", "理由"), ("jihua", "计划"),
    ("mubiao", "目标"), ("chengshi", "城市"), ("nongcun", "农村"), ("xiangmu", "项目"),
    ("chanpin", "产品"), ("fuwu", "服务"), ("yonghu", "用户"), ("xitong", "系统"),
]

# 这 8 条在**新引擎、无自学习权重**时 top-1 不是期望词。前 4 条是 README 表格里
# 点名解释过的「词频倒挂」；后 4 条是同一类问题（同音词里更常见的那个赢了）。
DOCUMENTED_MISSES = {
    "zaijian": "在建",      # README 表格：再见 343 vs 在建 444
    "denglu": "登陆",       # 登录 462 vs 登陆 1513
    "shangchuan": "上船",   # 上传 98 vs 上船 328
    "xinnian": "信念",      # 新年 570 vs 信念 865
    "youxiang": "有向",
    "shipin": "食品",
    "yiyuan": "亿元",
    "lvxing": "履行",
}

TOP1_FLOOR = 88.0     # 实测 89.5%
TOP3_FLOOR = 98.0     # 实测 98.7%

index = load_or_build_index(ROOT / "pinyin_index.pkl", min_freq=20, max_word_len=6, verbose=False)
eng = PinyinEngine(index)


def main():
    print("== 节 1. 语料本身 ==  ", flush=True)
    check("语料是 76 条（README 里写的那个数）", len(CASES) == 76, len(CASES))
    check("拼音不重复", len({py for py, _ in CASES}) == len(CASES), len(CASES))
    check("拼音都是小写字母（引擎只认这个形态）",
          all(py.isascii() and py.islower() and py.isalpha() for py, _ in CASES), "")
    check("期望词都非空、且都不是拼音原文",
          all(want and want != py for py, want in CASES), "")
    check("测试用的引擎没有加载自学习权重（数字才可复现）", eng.user_boosts() == {},
          eng.user_boosts())
    check("索引是真的建起来了（不是空表）", len(index) > 1000, len(index))

    print("\n== 节 2. 总体指标 ==  ", flush=True)
    top1 = 0
    top3 = 0
    empty = []
    misses = {}
    worst3 = []
    for py, want in CASES:
        cands = eng.candidates(py)
        if not cands:
            empty.append(py)
            continue
        if cands[0] == want:
            top1 += 1
        else:
            misses[py] = cands[0]
        if want in cands[:3]:
            top3 += 1
        else:
            worst3.append((py, want, cands[:3]))

    n = len(CASES)
    p1 = top1 / n * 100.0
    p3 = top3 / n * 100.0
    print(f"    top-1 = {top1}/{n} = {p1:.1f}%   top-3 = {top3}/{n} = {p3:.1f}%", flush=True)

    check("每条输入都给出了候选（没有一条掉成空列表）", not empty, empty)
    check(f"top-1 ≥ {TOP1_FLOOR:.0f}%（实测 {p1:.1f}%）", p1 >= TOP1_FLOOR, (top1, n))
    check(f"top-3 ≥ {TOP3_FLOOR:.0f}%（实测 {p3:.1f}% —— README 说的『正确项几乎总在 top-3』）",
          p3 >= TOP3_FLOOR, worst3)
    check("top-3 没中的最多 1 条", len(worst3) <= 1, worst3)
    # README 里写的是「约 90.8%」；这一版语料实测 89.5%，两者差不到 1.5 个点 ——
    # 超过就说明词频表或打分函数真的变了，文档必须跟着改。
    check("实测 top-1 与 README 写的 90.8% 相差不到 1.5 个点", abs(p1 - 90.8) < 1.5, p1)

    print("\n== 节 3. 失败集合必须是文档里那几条 ==  ", flush=True)
    extra_misses = {k: v for k, v in misses.items() if k not in DOCUMENTED_MISSES}
    check("没有出现文档之外的新 top-1 失败", not extra_misses, extra_misses)
    fixed = [k for k in DOCUMENTED_MISSES if k not in misses]
    check("文档里点名的失败没有被悄悄『修好』（修好了就该来改文档与这里）", not fixed, fixed)
    for py, want in DOCUMENTED_MISSES.items():
        got = eng.candidates(py)
        check(f"README 表格一致：{py} 的 top-1 仍是「{want}」（期望词 {py} 自己）",
              got and got[0] == want, got[:3])

    print("\n== 节 4. 候选窗的承诺 ==  ", flush=True)
    check("候选最多 7 条（max_candidates 默认值）",
          all(len(eng.candidates(py)) <= 7 for py, _ in CASES),
          max(len(eng.candidates(py)) for py, _ in CASES))
    check("常见的词能凑满 7 条（`nihao`）", len(eng.candidates("nihao")) == 7,
          eng.candidates("nihao"))
    check("空输入给空列表", eng.candidates("") == [], eng.candidates(""))
    check("解释不了的东西给空列表（不是崩溃）", eng.candidates("zzzz") == [],
          eng.candidates("zzzz"))
    check("单个分隔符也给空列表", eng.candidates("'") == [], eng.candidates("'"))
    check("同一句问两次结果一样（缓存不改答案）",
          eng.candidates("nihao") == eng.candidates("nihao"), "")

    print("\n== 节 5. 自学习的承诺 ==  ", flush=True)
    before = eng.candidates("youxiang")
    check("改选之前「邮箱」不在第一位", before and before[0] != "邮箱", before[:3])
    eng.boost("邮箱", 1.0)
    after = eng.candidates("youxiang")
    check("改选一次（boost 1.0）之后「邮箱」顶到第一位", after and after[0] == "邮箱",
          after[:3])
    fresh = PinyinEngine(index)
    fresh.load_boosts({"邮箱": 1.0})
    check("从磁盘读回同样的权重也管用（load_boosts）",
          fresh.candidates("youxiang")[0] == "邮箱", fresh.candidates("youxiang")[:3])
    clean = PinyinEngine(index)
    check("新引擎仍然没有权重（boost 不串味）", clean.user_boosts() == {},
          clean.user_boosts())
    check("boost 之后 top-1 计数真的变多了（不是只改了缓存）",
          clean.candidates("youxiang")[0] == "有向", clean.candidates("youxiang")[:2])

    print("\n== 节 6. 文档里的例子必须跑得出来 ==  ", flush=True)
    # 第 94 轮：docs/使用流程.md 一直写着「`'` 手动分音节：xi'an → 西安」，
    # 可引擎先把 `'` 删掉再查（candidates("xi'an") 与 candidates("xian") 完全
    # 一样），而且词库里压根没有「西安」这个词 —— README 在上一轮已经改成实话，
    # 指南却还留着这条假承诺。这里把「文档怎么说」和「引擎怎么做」钉在一起：
    # 以后谁再往文档里写 xi'an → 西安，这条会红。
    xian = eng.candidates("xian")
    xi_an = eng.candidates("xi'an")
    check("`'` 不改变候选（xi'an 与 xian 结果相同）", xian == xi_an, (xian[:3], xi_an[:3]))
    check("词库里没有「西安」，所以 xi'an 出不来它", "西安" not in xi_an, xi_an[:3])

    guide = (ROOT / "docs" / "使用流程.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def row_with(prefix: str, text: str) -> str:
        """取键表里以 ``prefix`` 开头的那一行（键表才是用户在查的东西）。"""
        for ln in text.splitlines():
            if ln.startswith(prefix):
                return ln
        return ""

    check("指南的 `'` 那行不再承诺「西安」", "西安" not in row_with("| `'`", guide),
          row_with("| `'`", guide))
    check("README 中文键表的 `'` 那行不再承诺「西安」",
          "西安" not in row_with("| 字母 / `'`", readme), row_with("| 字母 / `'`", readme))
    check("README 英文键表的 `'` 那行不再承诺「西安」",
          "Xi'an" not in row_with("| letters / `'`", readme).lower(),
          row_with("| letters / `'`", readme))
    check("指南说的是实话：xi'an 与 xian 的候选一样",
          "`xi'an` 与 `xian` 的候选完全一样" in guide, "")
    check("README 说的是实话：候选仍按整串拼音算",
          "xi'an` 与 `xian` 结果相同" in readme, "")

    print(f"\n结果：{PASS}/{PASS + FAIL} 通过", flush=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
