#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
最终版：增强 GUI 批量下载器
- 自定义线程数（1-20）
- 并发下载（线程数可配置）
- 下载失败自动重试 3 次
- Gyazo 页面解析（batch_img 方式）
- Google Drive（公开文件）支持
- 文件名安全化、时间戳同步
- 显示进度 [cur/total]
- 右键菜单区分
- 下载完成后正确恢复按钮状态
- 下载总用时统计
- 绿色按钮样式（Custom.TButton）
"""

import os
import re
import mimetypes
import tkinter as tk
from tkinter import messagebox, filedialog
import tkinter.ttk as ttk
import threading
import requests
from queue import Queue
from email.utils import parsedate_to_datetime
import urllib.parse
import platform
import subprocess
import chardet
import time

# ---------- 配置 ----------
MAX_FILENAME_BYTES = 200
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Downloader/1.0"
DOWNLOAD_TIMEOUT = 60
DEFAULT_WORKERS = 3
MIN_WORKERS = 1
MAX_WORKERS_ALLOWED = 20
RETRY_TIMES = 3
RETRY_DELAY = 1.0  # seconds


# ---------------- 资源路径函数 ----------------
def resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):  # 打包模式
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

# ---------------- 加载图标 ----------------
def load_icon(root):
    ico_file = resource_path("app_icon.ico")
    if os.path.exists(ico_file):
        try:
            root.iconbitmap(ico_file)
        except Exception as e:
            print(f"图标加载失败: {e}")

# ==========================================================
# 工具函数
# ==========================================================
def get_default_save_dir():
    home = os.path.expanduser("~")
    path = os.path.join(home, "Pictures")
    os.makedirs(path, exist_ok=True)
    return path


def safe_print_log(msg):
    """线程安全写日志"""
    def _append():
        text_log.insert(tk.END, msg + "\n")
        text_log.see(tk.END)
    root.after(0, _append)


def open_folder(path):
    try:
        if platform.system() == "Windows":
            os.startfile(path)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        safe_print_log(f"打开保存目录失败: {e}")


# ==========================================================
# 时间戳工具
# ==========================================================
def set_mtime_from_last_modified(headers, local_path):
    try:
        last_modified = headers.get('Last-Modified')
        if not last_modified:
            return False, "无 Last-Modified"
        dt = parsedate_to_datetime(last_modified)
        ts = dt.timestamp()
        os.utime(local_path, (ts, ts))
        return True, "已同步时间戳"
    except Exception as e:
        return False, f"失败: {e}"


# ==========================================================
# 文件名处理
# ==========================================================
def get_filename_from_content_disposition(headers):
    cd = headers.get("Content-Disposition", "") if headers else ""
    match_utf8 = re.search(r'filename\*=UTF-8\'\'([^\s;]+)', cd, re.I)
    if match_utf8:
        try:
            return urllib.parse.unquote(match_utf8.group(1))
        except:
            return match_utf8.group(1)

    match_ascii = re.search(r'filename="([^"]+)"', cd)
    if match_ascii:
        raw = match_ascii.group(1)
        raw_bytes = raw.encode("latin1", errors="ignore")
        try:
            return raw_bytes.decode("utf-8")
        except:
            guess = chardet.detect(raw_bytes)
            enc = guess.get("encoding")
            if enc:
                try:
                    return raw_bytes.decode(enc)
                except:
                    pass
            return raw_bytes.decode("latin1", errors="replace")
    return None


def ext_from_content_type(content_type):
    if not content_type:
        return None
    ctype = content_type.split(";")[0].strip().lower()
    ext = mimetypes.guess_extension(ctype)
    if ext == ".jpe":
        return ".jpg"
    return ext


def make_safe_filename(orig_name):
    orig_name = orig_name or ""
    orig_name = os.path.basename(orig_name)
    base, ext = os.path.splitext(orig_name)

    invalid = set('"<>:/\\|?*')
    base = ''.join(c for c in base if ord(c) >= 32 and c not in invalid)
    base = re.sub(r'\s+', ' ', base).strip()

    suffix = ext
    suffix_bytes = suffix.encode('utf-8')
    max_len = MAX_FILENAME_BYTES - len(suffix_bytes)

    name_bytes = b''
    for ch in base:
        bch = ch.encode('utf-8')
        if len(name_bytes) + len(bch) > max_len:
            break
        name_bytes += bch

    base = name_bytes.decode("utf-8", "ignore") or "file"
    return base + suffix


def ensure_unique_filename(path):
    base, ext = os.path.splitext(path)
    n = 1
    new_path = path
    while os.path.exists(new_path):
        new_path = f"{base}_{n}{ext}"
        n += 1
    return new_path


# ==========================================================
# Gyazo 页面解析
# ==========================================================
def convert_gyazo_to_image_url(url, session):
    try:
        r = session.get(url, timeout=10)
        if r.status_code != 200:
            return url
        html = r.text

        m = re.search(r'og:image"\s+content="([^"]+)"', html)
        if m:
            return m.group(1)

        m2 = re.search(r'(https://i\.gyazo\.com/[0-9a-f]{32}\.\w+)', html)
        if m2:
            return m2.group(1)

        m3 = re.match(r'https://gyazo\.com/([0-9a-f]{32})', url)
        if m3:
            gid = m3.group(1)
            for ext in ("png", "jpg", "jpeg", "webp"):
                test_url = f"https://i.gyazo.com/{gid}.{ext}"
                h = session.head(test_url, timeout=6)
                if h.status_code == 200:
                    return test_url
    except Exception:
        pass
    return url


# ==========================================================
# Google Drive
# ==========================================================
def is_google_drive_url(url):
    return "drive.google.com" in url or "docs.google.com" in url


def get_drive_file_id(url):
    m = re.search(r"/file/d/([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def download_from_google_drive(file_id, dest, session):
    URL = "https://drive.google.com/uc?export=download"
    r = session.get(URL, params={"id": file_id}, stream=True)
    token = re.search(r"confirm=([0-9A-Za-z_]+)", r.text)
    if token:
        r = session.get(URL, params={"id": file_id, "confirm": token.group(1)}, stream=True)

    filename = get_filename_from_content_disposition(r.headers) or file_id
    ext_guess = ext_from_content_type(r.headers.get("Content-Type"))
    if ext_guess and not filename.endswith(ext_guess):
        filename += ext_guess

    safe = make_safe_filename(filename)
    final_path = ensure_unique_filename(os.path.join(dest, safe))

    with open(final_path, "wb") as f:
        for chunk in r.iter_content(32768):
            if chunk:
                f.write(chunk)

    set_mtime_from_last_modified(r.headers, final_path)
    return True, final_path, "下载成功"


# ==========================================================
# 普通文件下载（带重试）
# ==========================================================
def download_file_once(url, local_path, session):
    try:
        r = session.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT)
        r.raise_for_status()

        filename = get_filename_from_content_disposition(r.headers)
        if not filename:
            filename = os.path.basename(urllib.parse.urlparse(r.url).path) or "file"

        ext_guess = ext_from_content_type(r.headers.get("Content-Type"))
        if ext_guess and not filename.endswith(ext_guess):
            filename += ext_guess

        safe = make_safe_filename(filename)
        final_path = ensure_unique_filename(os.path.join(os.path.dirname(local_path), safe))

        with open(final_path, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)

        set_mtime_from_last_modified(r.headers, final_path)
        return True, final_path, "下载成功"
    except Exception as e:
        return False, local_path, str(e)


def download_with_retries(url, local_path, session):
    last_msg = ""
    for attempt in range(1, RETRY_TIMES + 1):
        ok, fp, msg = download_file_once(url, local_path, session)
        if ok:
            return True, fp, f"{msg} (第 {attempt} 次成功)"
        last_msg = msg
        time.sleep(RETRY_DELAY)
    return False, local_path, f"失败：{last_msg}"


# ==========================================================
# GUI 构建
# ==========================================================
root = tk.Tk()
root.title("多线程批量远程下载器（Gyazo / Google Drive / 时间戳）")
load_icon(root)
# root.geometry("900x650")

frame = tk.Frame(root)
frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

# 绿色按钮样式
style = ttk.Style()
style.configure(
    "Custom.TButton",
    background="#4CAF50",
    # foreground="white",
    width=30,
    # padding=6,
)
style.map(
    "Custom.TButton",
    background=[("active", "#45A049")],
)

# ---------- 链接输入区 ----------
tk.Label(frame, text="下载链接（每行一个）:").grid(row=0, column=0, sticky=tk.W)
text_urls = tk.Text(frame, height=12)
text_urls.grid(row=1, column=0, columnspan=6, sticky=tk.EW)
scroll_urls = tk.Scrollbar(frame, command=text_urls.yview)
text_urls.config(yscrollcommand=scroll_urls.set)
scroll_urls.grid(row=1, column=6, sticky=tk.NS)

# ---------- 保存目录 ----------
tk.Label(frame, text="保存目录:").grid(row=2, column=0, sticky=tk.W)
entry_save_dir = tk.Entry(frame)
entry_save_dir.grid(row=3, column=0, columnspan=4, sticky=tk.EW)
entry_save_dir.insert(0, get_default_save_dir())

def choose_folder():
    f = filedialog.askdirectory(initialdir=get_default_save_dir())
    if f:
        entry_save_dir.delete(0, tk.END)
        entry_save_dir.insert(0, f)

ttk.Button(frame, text="浏览", command=choose_folder).grid(row=3, column=4, sticky=tk.W)

# ---------- 线程数量 ----------
tk.Label(frame, text="线程数量 (1-20):").grid(row=4, column=0, sticky=tk.W)
entry_threads = tk.Entry(frame, width=6)
entry_threads.grid(row=4, column=1, sticky=tk.W)
entry_threads.insert(0, str(DEFAULT_WORKERS))

# ---------- 开始按钮 ----------
btn_download = ttk.Button(frame, text="开始下载", style="Custom.TButton")
btn_download.grid(row=5, column=1, pady=10, sticky=tk.W)

# ---------- 日志 ----------
tk.Label(frame, text="日志:").grid(row=6, column=0, sticky=tk.W)
text_log = tk.Text(frame, height=16)
text_log.grid(row=7, column=0, columnspan=6, sticky=tk.EW)
scroll_log = tk.Scrollbar(frame, command=text_log.yview)
text_log.config(yscrollcommand=scroll_log.set)
scroll_log.grid(row=7, column=6, sticky=tk.NS)

# ---------- 右键菜单 ----------
def _paste(widget):
    try:
        widget.insert(tk.INSERT, root.clipboard_get())
    except Exception:
        pass

def show_context_menu_urls(event):
    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="粘贴", command=lambda:_paste(text_urls))
    menu.add_command(label="清空", command=lambda:text_urls.delete("1.0", tk.END))
    menu.tk_popup(event.x_root, event.y_root)

def show_context_menu_log(event):
    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="复制", command=lambda:root.clipboard_append(text_log.get("1.0", tk.END)))
    menu.add_command(label="清空", command=lambda:text_log.delete("1.0", tk.END))
    menu.tk_popup(event.x_root, event.y_root)

text_urls.bind("<Button-3>", show_context_menu_urls)
text_log.bind("<Button-3>", show_context_menu_log)

# ==========================================================
# 多线程下载
# ==========================================================
def start_download():
    lines = text_urls.get("1.0", tk.END).strip().splitlines()
    urls = [u.strip() for u in lines if u.strip()]
    if not urls:
        messagebox.showerror("错误", "请输入下载链接")
        return

    save_dir = entry_save_dir.get().strip()
    if not save_dir:
        messagebox.showerror("错误", "请选择保存目录")
        return

    # 保存最终目录（不允许被覆盖）
    save_dir_final = save_dir

    # 线程数
    try:
        workers = int(entry_threads.get().strip())
    except:
        workers = DEFAULT_WORKERS
    workers = max(MIN_WORKERS, min(MAX_WORKERS_ALLOWED, workers))

    btn_download.config(state=tk.DISABLED)
    text_log.delete("1.0", tk.END)

    # 创建队列
    q = Queue()
    total = len(urls)
    counter = {"cur": 0}
    lock = threading.Lock()

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    # ---------- worker ----------
    def worker_run(thread_id):
        while True:
            try:
                url = q.get(timeout=1)
            except:
                return
            if url is None:
                q.task_done()
                return

            with lock:
                counter["cur"] += 1
                idx = counter["cur"]

            safe_print_log(f"[{idx}/{total}] 线程 {thread_id} 处理: {url}")

            # Gyazo
            try:
                if re.match(r"https?://(www\.)?gyazo\.com/[0-9a-f]{32}", url):
                    new_url = convert_gyazo_to_image_url(url, session)
                    if new_url != url:
                        safe_print_log(f"[{idx}/{total}] Gyazo 解析: {new_url}")
                        url = new_url
            except Exception as e:
                safe_print_log(f"[{idx}/{total}] Gyazo 解析失败: {e}")

            # Google Drive
            if is_google_drive_url(url):
                fid = get_drive_file_id(url)
                if fid:
                    ok = False
                    last_err = ""
                    for a in range(1, RETRY_TIMES + 1):
                        try:
                            ok2, p2, m2 = download_from_google_drive(fid, save_dir_final, session)
                            if ok2:
                                safe_print_log(f"[{idx}/{total}] [Drive OK] {p2}")
                                ok = True
                                break
                            else:
                                last_err = m2
                        except Exception as e:
                            last_err = str(e)
                        time.sleep(RETRY_DELAY)
                    if not ok:
                        safe_print_log(f"[{idx}/{total}] [Drive ERR] {last_err}")
                    q.task_done()
                    continue

            # 普通 URL 下载
            guessed = os.path.basename(urllib.parse.urlparse(url).path) or "file"
            lp = os.path.join(save_dir_final, guessed)
            ok, fp, msg = download_with_retries(url, lp, session)
            safe_print_log(f"[{idx}/{total}] {'[OK]' if ok else '[ERR]'} {fp} - {msg}")

            q.task_done()

    # ---------- 计时开始 ----------
    start_time = time.time()

    # 加任务
    for u in urls:
        q.put(u)

    # 启线程
    threads = []
    for i in range(workers):
        t = threading.Thread(target=worker_run, args=(i+1,), daemon=True)
        t.start()
        threads.append(t)

    # ---------- 收尾 ----------
    def finalize():
        q.join()

        # 停线程
        for _ in range(workers):
            q.put(None)
        for t in threads:
            t.join(timeout=0.1)

        # 计时结束
        elapsed = time.time() - start_time
        safe_print_log(f"\n全部下载完成，用时 {elapsed:.2f} 秒")

        # 打开目录
        open_folder(save_dir_final)

        # 恢复按钮
        root.after(0, lambda: btn_download.config(state=tk.NORMAL))

    threading.Thread(target=finalize, daemon=True).start()


btn_download.config(command=start_download)

# ==========================================================
root.mainloop()
