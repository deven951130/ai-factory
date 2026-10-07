"""AI 工廠桌面版（Windows）：背景執行 Dashboard 伺服器，用 WebView2 視窗開啟；關掉視窗就結束。

設定與資料放在 %LOCALAPPDATA%\\AIFactory：nodes.json（第一次執行時複製預設值，可自行修改）、
data\\（事件紀錄、帳號清單）、logs\\app.log。安裝資料夾只放程式本身，更新 / 解除安裝不影響資料。
"""
from __future__ import annotations

import ctypes
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

APP_NAME = "AI 工廠"
PORT = 8000
HOME = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "AIFactory"
# 打包後的資源（frontend、預設 nodes.json）在 PyInstaller 的資料夾；直接跑原始碼時是 repo 根目錄。
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(RES))


def alert(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10)  # MB_ICONERROR


def first_instance() -> bool:
    # 兩個伺服器同時跑會互相覆寫 accounts.json；已經開著就不再開第二個。
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\AIFactoryDesktop")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def listen_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", PORT))
    except OSError:  # 8000 被占用（例如 start.ps1 開著）：改用系統給的空閒埠
        s.bind(("127.0.0.1", 0))
    return s


def main() -> None:
    if not first_instance():
        alert("AI 工廠已經在執行中。")
        return
    (HOME / "logs").mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:  # 視窗程式沒有主控台：輸出寫進 log
        log = open(HOME / "logs" / "app.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    config = HOME / "nodes.json"
    if not config.exists():
        shutil.copyfile(RES / "nodes.json", config)
    os.environ.setdefault("FACTORY_CONFIG", str(config))
    os.environ.setdefault("FACTORY_DATA_DIR", str(HOME / "data"))

    import uvicorn
    import webview

    from backend.main import app  # 讀上面的環境變數建立工廠

    sock = listen_socket()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", use_colors=False, timeout_graceful_shutdown=5))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started and thread.is_alive():
        time.sleep(0.05)
    if not server.started:
        alert(f"伺服器啟動失敗，詳見 {HOME / 'logs' / 'app.log'}")
        return

    # 底色和 Oneiroverse 啟動動畫一致，頁面畫出來之前不會先閃一下別的顏色。
    webview.create_window(APP_NAME, f"http://127.0.0.1:{port}/", width=1440, height=900, min_size=(900, 600),
                          background_color="#06162b", text_select=True, zoomable=True)
    webview.start(private_mode=False, storage_path=str(HOME / "webview"))  # 保留通知權限等設定
    server.should_exit = True  # 視窗關了：結束伺服器（會一併取消任務、砍掉 CLI 子程序）
    thread.join(15)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 沒有主控台，例外要用對話框讓人看到
        import traceback

        traceback.print_exc()
        alert(f"AI 工廠發生錯誤：{e}")
