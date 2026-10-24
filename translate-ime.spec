# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包脚本。

用法（在仓库根目录）::

    .venv\\Scripts\\python.exe -m pip install pyinstaller
    .venv\\Scripts\\pyinstaller.exe --noconfirm translate-ime.spec

产物在 ``dist/translate-ime/``：整个文件夹一起压缩发给别人，解压后双击
``translate-ime.exe`` 就行。

两个 exe 是有意的：

* ``translate-ime.exe`` —— **窗口版**（``console=False``）。双击用它，不会闪黑框。
  但窗口版进程没有控制台，``--check`` 打印的东西用户看不见。
* ``translate-ime-cli.exe`` —— **控制台版**。排障时才用：
  ``translate-ime-cli.exe --check`` / ``--doctor`` / ``--build-index``。

为什么是 onedir 而不是 onefile：onefile 每次启动都要把整个包（几十 MB）解压到
临时目录再起，冷启动明显更慢，杀软也更容易拦「一个 exe 往临时目录里丢一堆文件」。
onedir 是「解压一次、以后直接启动」。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH)

#: 要随包带走的数据文件：(源, 包内目录)。缺哪个就跳过，不因为本地少一个生成物
#: （比如 ``pinyin_index.pkl``）就把整个构建搞挂 —— 首次运行会自己重建索引。
_CANDIDATE_DATAS = [
    (ROOT / "t_ime" / "webui.html", "t_ime"),
    (ROOT / "t_ime" / "assets", "t_ime/assets"),
    (ROOT / "config.example.json", "."),
    (ROOT / "pinyin_index.pkl", "."),
]
datas = [(str(src), dst) for src, dst in _CANDIDATE_DATAS if src.exists()]

# jieba 只用来读它的词频词典 dict.txt —— 那是纯数据文件，必须显式收进来，
# 否则打包后 `jieba` 能 import、一查词频就报「词典不存在」。
datas += collect_data_files("jieba")

hiddenimports = [
    "jieba",
    "pypinyin",
    "tkinter",
    "tkinter.ttk",
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # tests/ 和打包无关，打进去只会变大（而且它们会 import 一堆只给开发用的东西）
    excludes=["tests", "pytest", "PIL", "numpy"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe_gui = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="translate-ime",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "t_ime" / "assets" / "tray.ico"),
)

exe_cli = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="translate-ime-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(ROOT / "t_ime" / "assets" / "tray.ico"),
)

coll = COLLECT(
    exe_gui,
    exe_cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="translate-ime",
)
