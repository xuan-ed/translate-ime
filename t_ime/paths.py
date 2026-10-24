# -*- coding: utf-8 -*-
"""两种运行方式下的目录：源码直接跑，和打包成 exe 之后跑。

源码跑的时候，「可写的目录」和「资源所在目录」是同一个 —— 就是仓库根。
冻结（PyInstaller）之后这两件事必须分开，否则会出两类毛病：

* **资源**（``webui.html``、托盘图标、``config.example.json``）在 PyInstaller
  的解包目录里（``sys._MEIPASS``）。它是**只读**的，而且是每次启动临时解出来的
  路径。以前这些地方全靠 ``Path(__file__).parent.parent`` 推算，冻结后正好指到
  解包目录上，能读不能用。
* **可写的东西**（``config.json``、日志、``pinyin_index.pkl``、``user_boosts.json``、
  状态 / 命令文件）必须落在 **exe 旁边**。落到解包目录里的话，进程一退出就没了 ——
  用户改了 API key 下次打开又变回空，自学习学的东西也每次都清零。

所以这里只做一件很窄的事：给出这两个根。谁需要路径就来问，别自己再推一遍。
"""
from __future__ import annotations

import sys
from pathlib import Path

#: 是不是打包后的 exe。PyInstaller 会注入 ``sys.frozen``。
FROZEN = bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """**可写**根目录：配置、日志、索引缓存、状态文件都放这儿。

    * 冻结后：exe 所在目录（解压到哪儿就在哪儿，绿色软件的行为）；
    * 源码：仓库根目录。
    """
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundle_root() -> Path:
    """**只读**资源根目录：``webui.html`` / ``assets`` / ``config.example.json``。

    * 冻结后：``sys._MEIPASS``（PyInstaller 的解包目录）；
    * 源码：同 :func:`app_root`。
    """
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", app_root()))
    return Path(__file__).resolve().parent.parent


def resource(*parts: str) -> Path:
    """资源路径的快捷写法（找不到也照样返回路径，让调用方自己判断存不存在）。"""
    return bundle_root().joinpath(*parts)
