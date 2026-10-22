# -*- coding: utf-8 -*-
"""靶子窗口：一个独立进程里的 Tk Text，用来接收真键盘注入。

启动后每 200ms 把自己 Text 的内容写到 <outfile>，供测试进程读取校验。
窗口标题固定为 T-IME-TARGET，测试进程用 FindWindowW 找它。

为什么要独立进程：被测的输入法判断「哪个窗口是用户真正在用的」靠的是
「它属于别的进程」，同进程的窗口会被判成自己人。所以靶子必须是另一个进程。

直接跑：python tests/target_window.py [输出文件]
"""
import sys
import tkinter as tk

out = sys.argv[1] if len(sys.argv) > 1 else "target_out.txt"

root = tk.Tk()
root.title("T-IME-TARGET")          # e2e 测试靠这个标题 FindWindowW，别改
root.geometry("820x300+300+340")
root.configure(bg="#f3f4f6")

# 只为了截图好看，纯装饰，不参与任何断言
bar = tk.Frame(root, bg="#e8eaed", height=34)
bar.pack(fill="x")
tk.Label(
    bar, text="  记事本 — 演示（独立进程）", bg="#e8eaed", fg="#3c4043",
    font=("Microsoft YaHei UI", 11), anchor="w",
).pack(side="left", fill="y")

txt = tk.Text(
    root, font=("Microsoft YaHei UI", 17), bg="#ffffff", fg="#111111",
    relief="flat", padx=14, pady=12, insertwidth=2, wrap="word",
)
txt.pack(fill="both", expand=True)
txt.focus_set()
root.update()

# 先抢一下前台，再还回去，免得挡住别的窗口
root.attributes("-topmost", True)
root.update()
print("ready", flush=True)


def dump():
    try:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(txt.get("1.0", "end-1c"))
    except Exception:
        pass
    root.after(200, dump)


root.after(200, dump)
root.after(120000, root.destroy)
root.mainloop()
