# -*- coding: utf-8 -*-
"""控制台编码：让中文和符号在任何输出目标上都不炸、也不糊。

这件事有个反直觉的地方 —— **要分两种情况，不能一刀切**：

* **真正的 Windows 控制台**（双击 ``run.bat`` / 在 cmd 里直接跑）：
  **什么都别做**。Python 在这里走的是 ``WriteConsoleW``，把字符串直接交给
  系统，本来就和代码页无关（cp936 也照样显示中文）。硬改成 UTF-8 反而会让
  字节被控制台按 cp936 再解释一遍 —— 那才是乱码的来源。
* **输出被重定向**（管道 / 文件：CI、``> log.txt``、被别的程序 capture）：
  这时用的是代码页编码（中文 Windows 上是 cp936），遇到码表里没有的字符
  （``✅`` ``❌`` ``⟦`` ``⟧``）会直接抛 ``UnicodeEncodeError`` 把程序掀翻。
  必须显式指定 UTF-8。

调用时机：**在任何输出之前**。放在模块顶层、其它 import 之后立刻调。
"""
from __future__ import annotations

import sys

__all__ = ["fix_console_encoding"]


def fix_console_encoding() -> None:
    """把被重定向的 stdout/stderr 切成 UTF-8；真控制台保持原样。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            buffer = getattr(stream, "buffer", None)
            if type(buffer).__name__ == "_WindowsConsoleIO":
                continue  # 真控制台，交给 WriteConsoleW
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
