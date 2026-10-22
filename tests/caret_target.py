# -*- coding: utf-8 -*-
"""靶子窗口（带光标位置）：独立进程里的 Tk Text，用来接收真键盘注入。

和 ``tests/target_window.py`` 的区别：那个只 dump 文本内容，这个**还要 dump
光标位置**（``txt.index("insert")``）。方向键被输入法吞掉时，「文本没变」是弱证据
（方向键本来也不改文本），**光标没动**才是硬证据。

每 100ms 把 ``{"insert": ..., "text": ..., "cmd": ...}`` 写进 <outfile>（``cmd`` 是
「最后执行的那条命令」，测试靠它确认命令真的落地了，而不是光读到旧状态）；
每 100ms 看一眼 <cmdfile>，内容变了就执行（目前只认 ``clear``：
清空文本并把光标放回 1.0，供测试在阶段之间重置）。

窗口标题固定为 T-IME-CARET，测试进程用 FindWindowW 找它。

**这个靶子还会把系统中文输入法从自己窗口上摘掉**（见下面的
``detach_system_ime()``）：不摘的话，注入的字母会进系统 IME 的候选串而不是
Text，要等回车/空格才提交 —— 那会让「字母有没有被我们放行」的检查永远失败，
而那并不是产品的问题。

直接跑：python tests/caret_target.py [输出文件] [命令文件]
"""
import ctypes
import json
import sys
import tkinter as tk

out = sys.argv[1] if len(sys.argv) > 1 else "caret_out.json"
cmd = sys.argv[2] if len(sys.argv) > 2 else "caret_cmd.txt"

root = tk.Tk()
root.title("T-IME-CARET")            # 测试靠这个标题 FindWindowW，别改
root.geometry("820x300+300+340")
root.configure(bg="#f3f4f6")

bar = tk.Frame(root, bg="#e8eaed", height=34)
bar.pack(fill="x")
tk.Label(
    bar, text="  记事本 — 光标靶子（独立进程）", bg="#e8eaed", fg="#3c4043",
    font=("Microsoft YaHei UI", 11), anchor="w",
).pack(side="left", fill="y")

txt = tk.Text(
    root, font=("Microsoft YaHei UI", 17), bg="#ffffff", fg="#111111",
    relief="flat", padx=14, pady=12, insertwidth=2, wrap="word",
)
txt.pack(fill="both", expand=True)
txt.mark_set("insert", "1.0")
txt.focus_set()
root.update()

root.attributes("-topmost", True)
root.update()


def detach_system_ime():
    """把这个窗口的**系统**中文输入法摘掉（不摘，注入的字母根本进不了 Text）。

    实测（`.tmp/r25_matrix.py`，不装我们自己的钩子，只注入按键）：
    空格 → ``' '``、数字 ``0x31`` → ``'1'``、``VK_OEM_COMMA`` → **全角 ``'，'``**、
    而 ``0x41``/``0x4E``（a/n）注入后文本**一直是空的**，直到再注入一个回车，
    ``'anan'`` 才（以原始拼音串的形式）一次掉下来。
    也就是说：字母先进了系统 IME 的候选串，要等终止键才提交；ASCII 逗号也被它
    按中文标点转了全角。于是「挂起后字母原样落进靶子」这条检查永远测不过 ——
    那是**测试环境的坑，不是产品的坑**（阶段 B 之所以全绿，是因为那些键被我们
    自己的钩子在系统 IME 之前就吞掉了）。

    所以这里主动把这个窗口从系统 IME 上摘下来：``ImmAssociateContext(hwnd, None)``。
    Tk 的 ``winfo_id()`` 只是里层的 TkChild，真顶层要沿 ``GetParent`` 往上走
    （窗口样式还带 ``WS_CHILD`` 就继续往上），两个都摘一遍最稳。
    """
    try:
        u32 = ctypes.windll.user32
        imm32 = ctypes.windll.imm32
        u32.GetParent.restype = ctypes.c_void_p
        u32.GetParent.argtypes = [ctypes.c_void_p]
        u32.GetWindowLongW.restype = ctypes.c_int
        u32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        imm32.ImmAssociateContext.restype = ctypes.c_void_p
        imm32.ImmAssociateContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        hwnd = int(root.winfo_id())
        targets = [hwnd]
        for _ in range(8):
            if not (u32.GetWindowLongW(ctypes.c_void_p(hwnd), -16) & 0x40000000):
                break                       # 不再是子窗口，到顶了
            parent = u32.GetParent(ctypes.c_void_p(hwnd))
            if not parent:
                break
            hwnd = int(parent)
            targets.append(hwnd)
        for h in targets:
            imm32.ImmAssociateContext(ctypes.c_void_p(h), None)
    except Exception as exc:                # 摘不掉也别把靶子弄死，测试那边会自己看出来
        print(f"ime-detach-failed: {exc!r}", flush=True)


detach_system_ime()
print("ready", flush=True)

_last_cmd = None


def dump():
    global _last_cmd
    try:
        with open(cmd, encoding="utf-8") as fh:
            now = fh.read().strip()
    except Exception:
        now = None
    if now and now != _last_cmd:
        _last_cmd = now
        if now.split()[0] == "clear":   # 写成 "clear 1" / "clear 2"…，否则连写两次内容一样，不触发
            txt.delete("1.0", "end")
            txt.mark_set("insert", "1.0")
    # **先执行命令，再写 dump**，并且把「最后执行的命令」一起写出去：
    # 反过来的话，测试读完 dump 就注入按键，而那条 clear 还没落地 —— 按键会被
    # 随后落地的 clear 抹掉，看起来就像「注入的键根本进不去」（第 26 轮那个
    # 时好时坏的 flaky 的真身）。测试靠 ``cmd`` 这个回显确认命令已经落地。
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"insert": txt.index("insert"),
                       "text": txt.get("1.0", "end-1c"),
                       "cmd": _last_cmd}, fh)
    except Exception:
        pass
    root.after(100, dump)


root.after(100, dump)
root.after(180000, root.destroy)
root.mainloop()
