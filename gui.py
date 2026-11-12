# -*- coding: utf-8 -*-
"""Windows GUI with configurable silent-mode notifications and boss key."""
import ctypes
from ctypes import wintypes
import json
import queue
import sqlite3
import threading
import time
import tkinter as tk
from tkinter import ttk
from pathlib import Path
from playwright.sync_api import sync_playwright
from monitor import HOME, DB, CSV, URL, metadata, wbi_key, check, make_session, browser

CONFIG = HOME / "settings.json"
DEFAULTS = {"hotkey": "Ctrl+Alt+H", "notifications": False, "start_hidden": False, "interval": 120}
KEYS = {"F%d" % i: 0x6F + i for i in range(1, 13)}
KEYS.update({"SPACE": 0x20, "ESC": 0x1B})
MODS = {"ALT": 1, "CTRL": 2, "SHIFT": 4, "WIN": 8}
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
HOTKEY_ID = 4810

class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt", POINT)]

def decode_hotkey(value):
    parts = [p.strip().upper() for p in value.split("+")]
    if len(parts) < 2 or len(set(parts)) != len(parts):
        raise ValueError("格式示例：Ctrl+Alt+H")
    flags = 0
    for part in parts[:-1]:
        if part not in MODS:
            raise ValueError("修饰键仅支持 Ctrl / Alt / Shift / Win")
        flags |= MODS[part]
    key = parts[-1]
    if len(key) == 1 and key in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        code = ord(key)
    elif key in KEYS:
        code = KEYS[key]
    else:
        raise ValueError("末位仅支持字母、数字或 F1–F12 等")
    return flags | 0x4000, code

def read_settings():
    data = dict(DEFAULTS)
    try:
        loaded = json.loads(CONFIG.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data.update({key: value for key, value in loaded.items() if key in DEFAULTS})
    except (OSError, ValueError):
        pass
    return data

class GlobalHotkey(threading.Thread):
    def __init__(self, events, sequence):
        super().__init__(daemon=True)
        self.events = events
        self.sequence = sequence
        self.thread_id = None
        self.ready = threading.Event()
        self.ok = False

    def run(self):
        user32 = ctypes.windll.user32
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        try:
            flags, vk = decode_hotkey(self.sequence)
            self.ok = bool(user32.RegisterHotKey(None, HOTKEY_ID, flags, vk))
        except ValueError:
            self.ok = False
        self.ready.set()
        if not self.ok:
            return
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.events.put(("boss", None))
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    def stop(self):
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)

class MonitorUI:
    def __init__(self):
        self.cfg = read_settings()
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.worker = None
        self.hidden = False
        self.root = tk.Tk()
        self.root.title("大A纪要 · UP主回复监控")
        self.root.geometry("1020x690")
        self.root.minsize(840, 520)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.status = tk.StringVar(value="尚未开始监控")
        self.pin_label = tk.StringVar(value="当前置顶：未读取")
        self.notification = tk.BooleanVar(value=bool(self.cfg["notifications"]))
        self.start_hidden = tk.BooleanVar(value=bool(self.cfg["start_hidden"]))
        self.hotkey = tk.StringVar(value=str(self.cfg["hotkey"]))
        self.interval = tk.StringVar(value=str(self.cfg["interval"]))
        self.render()
        self.hotkey_thread = None
        self.install_hotkey()
        self.refresh()
        self.root.after(250, self.poll_events)
        if self.start_hidden.get():
            self.root.after(300, self.hide)

    def render(self):
        area = ttk.Frame(self.root, padding=16)
        area.pack(fill="both", expand=True)
        ttk.Label(area, text="大A纪要 · 置顶评论监控", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(area, text="仅保留当前置顶主评论，以及该评论楼中楼中 UP 主自己的回复", foreground="#555").pack(anchor="w", pady=(3, 12))
        actions = ttk.Frame(area)
        actions.pack(fill="x", pady=(0, 10))
        self.start_button = ttk.Button(actions, text="登录并启动监控", command=self.start_monitoring)
        self.start_button.pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="停止监控", command=self.stop_monitoring).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="刷新记录", command=self.refresh).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="隐藏窗口（老板键）", command=self.hide).pack(side="left")
        ttk.Label(area, textvariable=self.status, foreground="#16608c").pack(anchor="w", pady=3)
        ttk.Label(area, textvariable=self.pin_label).pack(anchor="w", pady=(0, 10))
        prefs = ttk.LabelFrame(area, text="设置", padding=10)
        prefs.pack(fill="x")
        ttk.Checkbutton(prefs, text="发现 UP 主新回复时弹窗提醒（默认关闭）",
                        variable=self.notification, command=self.save_settings).grid(row=0, column=0, sticky="w", columnspan=4, pady=3)
        ttk.Checkbutton(prefs, text="软件启动时隐藏窗口", variable=self.start_hidden,
                        command=self.save_settings).grid(row=1, column=0, sticky="w", columnspan=4, pady=3)
        ttk.Label(prefs, text="老板键：").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(prefs, textvariable=self.hotkey, width=19).grid(row=2, column=1, sticky="w", padx=(0, 12))
        ttk.Label(prefs, text="检查间隔（秒）：").grid(row=2, column=2, sticky="w")
        ttk.Entry(prefs, textvariable=self.interval, width=9).grid(row=2, column=3, sticky="w")
        ttk.Button(prefs, text="保存快捷键和间隔", command=self.save_settings).grid(row=2, column=4, padx=10)
        ttk.Label(prefs, text="隐藏后后台继续记录，绝不弹出抓取提醒。重新按老板键恢复窗口。", foreground="#666").grid(row=3, column=0, columnspan=5, sticky="w")
        ttk.Label(area, text="已抓取记录（仅在打开软件时查看）", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(14, 6))
        table_frame = ttk.Frame(area)
        table_frame.pack(fill="both", expand=True)
        fields = ("posted", "kind", "author", "message")
        self.table = ttk.Treeview(table_frame, columns=fields, show="headings", selectmode="browse")
        for col, name, width in (("posted","发布时间",165),("kind","类别",105),("author","发布者",130),("message","评论内容",530)):
            self.table.heading(col, text=name)
            self.table.column(col, width=width, minwidth=75, stretch=(col=="message"))
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.table.bind("<<TreeviewSelect>>", self.select_row)
        self.detail = tk.Text(area, height=5, wrap="word")
        self.detail.pack(fill="x", pady=(6, 4))
        self.detail.configure(state="disabled")
        ttk.Label(area, text="关闭窗口会隐藏而非退出。结束程序请使用下方“退出程序”按钮。").pack(anchor="w")
        ttk.Button(area, text="退出程序", command=self.quit).pack(anchor="e", pady=(5,0))

    def install_hotkey(self):
        new_key = self.hotkey.get().strip()
        try:
            decode_hotkey(new_key)
        except ValueError as exc:
            self.status.set("老板键不合法：" + str(exc))
            return False
        old = self.hotkey_thread
        if old:
            old.stop()
            old.join(timeout=1.0)
        current = GlobalHotkey(self.events, new_key)
        current.start()
        current.ready.wait(2)
        self.hotkey_thread = current
        if not current.ok:
            self.status.set("快捷键已被占用，换一个组合后保存")
            return False
        return True

    def save_settings(self):
        try:
            interval = int(self.interval.get())
            if not 30 <= interval <= 3600:
                raise ValueError("监控间隔应为 30–3600 秒")
            decode_hotkey(self.hotkey.get())
        except ValueError as exc:
            self.status.set("设置未保存：" + str(exc))
            return
        changed = self.hotkey.get().strip().upper() != str(self.cfg["hotkey"]).upper()
        self.cfg = {"hotkey": self.hotkey.get().strip(),
                    "interval": interval, "notifications": self.notification.get(),
                    "start_hidden": self.start_hidden.get()}
        CONFIG.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        if changed:
            self.install_hotkey()
        self.status.set("设置已保存；提醒：" + ("开启" if self.notification.get() else "关闭"))

    def hide(self):
        self.hidden = True
        self.root.withdraw()

    def toggle(self):
        if self.hidden:
            self.hidden = False
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self.refresh()
        else:
            self.hide()

    def start_monitoring(self):
        if self.worker and self.worker.is_alive():
            return
        self.stop.clear()
        self.start_button.configure(state="disabled")
        self.status.set("正在启动登录浏览器，请登录后保持网页可访问……")
        self.worker = threading.Thread(target=self.work, daemon=True)
        self.worker.start()

    def stop_monitoring(self):
        self.stop.set()
        self.status.set("正在停止（网络请求结束后生效）")

    def work(self):
        conn = None
        try:
            conn = sqlite3.connect(DB, timeout=30)
            with sync_playwright() as pw:
                ctx = browser(pw, wait_for_login=False)
                try:
                    session = None
                    info = None
                    while not self.stop.is_set():
                        try:
                            session = make_session(ctx)
                            info = metadata(session)
                            key = wbi_key(session)
                            break
                        except Exception as exc:
                            self.events.put(("status", "等待 B 站账号登录及充电权限：" + str(exc)[:100]))
                            self.stop.wait(8)
                    if info is None:
                        return
                finally:
                    ctx.close()
                oid, kind, uid, author = info
                self.events.put(("status", "正在监控 UP 主：" + str(author)))
                count = 0
                while not self.stop.is_set():
                    try:
                        old = {row[0] for row in conn.execute("SELECT id FROM records WHERE kind='UP主楼中楼'")}
                        check(session, conn, oid, kind, uid, key)
                        new_rows = conn.execute("SELECT id,content FROM records WHERE kind='UP主楼中楼' ORDER BY seen_at DESC").fetchall()
                        fresh = [(identifier, content) for identifier, content in new_rows if identifier not in old]
                        root = conn.execute("SELECT root_id FROM pin_history ORDER BY id DESC LIMIT 1").fetchone()
                        self.events.put(("update", (root[0] if root else "", fresh)))
                        self.events.put(("status", "最近检查：" + time.strftime("%Y-%m-%d %H:%M:%S") + "；发现 %d 条新回复" % len(fresh)))
                        count += 1
                        if count % 30 == 0:
                            # Refresh signature; the browser session itself is never written to GitHub.
                            key = wbi_key(session)
                    except Exception as exc:
                        self.events.put(("status", "检查失败（历史仍保留）：" + str(exc)[:150]))
                    self.stop.wait(int(self.cfg["interval"]))
        except Exception as exc:
            self.events.put(("status", "启动失败：" + str(exc)))
        finally:
            if conn:
                conn.close()
            self.events.put(("stopped", None))

    def poll_events(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "boss":
                    self.toggle()
                elif event == "status":
                    self.status.set(payload)
                elif event == "update":
                    self.pin_label.set("当前置顶评论 ID：" + str(payload[0]))
                    self.refresh()
                    if payload[1] and self.notification.get() and not self.hidden:
                        self.show_notice(payload[1])
                elif event == "stopped":
                    self.start_button.configure(state="normal")
        except queue.Empty:
            pass
        self.root.after(250, self.poll_events)

    def show_notice(self, fresh):
        if self.hidden or not self.notification.get():
            return
        popup = tk.Toplevel(self.root)
        popup.title("UP 主有新回复")
        popup.geometry("460x180")
        popup.transient(self.root)
        ttk.Label(popup, text="抓取到 %d 条 UP 主新回复" % len(fresh),
                  font=("Microsoft YaHei UI", 11, "bold")).pack(padx=14, pady=(14, 6), anchor="w")
        ttk.Label(popup, text=str(fresh[0][1])[:110], wraplength=420).pack(padx=14, pady=4, anchor="w")
        ttk.Button(popup, text="查看软件记录", command=popup.destroy).pack(pady=8)
        popup.after(12000, lambda: popup.destroy() if popup.winfo_exists() else None)

    def refresh(self):
        if not DB.exists():
            return
        try:
            conn = sqlite3.connect(DB, timeout=2)
            rows = conn.execute("SELECT id,posted_at,kind,name,content FROM records ORDER BY posted_at DESC,id DESC LIMIT 1000").fetchall()
            conn.close()
        except sqlite3.Error:
            return
        self.record_by_id = {str(r[0]): r[4] for r in rows}
        for item in self.table.get_children():
            self.table.delete(item)
        for item_id, posted, kind, name, content in rows:
            self.table.insert("", "end", iid=str(item_id), values=(posted,kind,name,content[:180]))

    def select_row(self, _):
        selected = self.table.selection()
        if not selected:
            return
        text = self.record_by_id.get(selected[0], "")
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def quit(self):
        self.stop.set()
        if self.hotkey_thread:
            self.hotkey_thread.stop()
        self.root.destroy()

if __name__ == "__main__":
    MonitorUI().root.mainloop()
