#!/usr/bin/env python3
"""
取得 Claude token 並寫進 T-Display（Windows / macOS / Linux 通用）。

    python get_token.py               # 執行 claude setup-token -> 貼上 token -> 驗證 -> 寫入板子
    python get_token.py --no-write    # 只取得並驗證，不寫入板子
    python get_token.py --skip-setup  # 已經有 setup-token 產生的 token，直接貼上
    python get_token.py --local       # 查看這台電腦 Claude Code 目前登入的 token（不顯示完整內容）
    python get_token.py --port COM3   # 指定序列埠（預設自動偵測）

板子 WiFi 模式要用 `claude setup-token` 產生的長期 token。
電腦上 Claude Code 自己的登入 token（--local 看到的那個）幾小時就過期，
而且板子若自行續期會讓電腦被登出，所以不要寫進板子。

token 只在記憶體中處理，不會存檔、不會完整印出。
寫入板子需要 pyserial（pip install pyserial）；只驗證不需要。
"""

import argparse
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}

# USB 轉序列晶片：CH9102 / CH340 (1A86)、CP210x (10C4)
BOARD_VIDS = {0x1A86, 0x10C4}
BOARD_PIDS_EXCLUDE = {0x5722}   # 1A86:5722 是 USB 副螢幕，不是開發板

SYSTEM = platform.system()      # "Windows" / "Darwin" / "Linux"
OS_NAME = {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(SYSTEM, SYSTEM)


def mask(tok):
    return tok[:14] + "…" + tok[-4:] if len(tok) > 24 else "***"


# --------------------------------------------------------------------------- #
#  驗證 token：送 1 token 的請求，讀 rate-limit 標頭
# --------------------------------------------------------------------------- #
def check_token(tok):
    """回傳 (成功?, 說明)"""
    req = urllib.request.Request(
        API_URL, data=json.dumps(API_BODY).encode(),
        headers=dict(API_HEADERS, Authorization=f"Bearer {tok}"), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            hdrs = resp.headers
    except urllib.error.HTTPError as e:
        hdrs = e.headers   # 429 等錯誤回應仍帶有 rate-limit 標頭
        if e.code in (401, 403):
            return False, f"HTTP {e.code}：token 無效或已過期"
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e) and SYSTEM == "Darwin":
            return False, ("SSL 憑證驗證失敗：請執行 /Applications/Python 3.x/"
                           "Install Certificates.command 後再試")
        return False, f"連線失敗：{e.reason}"
    except Exception as e:
        return False, f"連線失敗：{e}"

    s = hdrs.get("anthropic-ratelimit-unified-5h-utilization")
    w = hdrs.get("anthropic-ratelimit-unified-7d-utilization")
    if s is None:
        return False, "回應中沒有 rate-limit 標頭（不是 Claude 訂閱帳號的 token？）"
    pct = lambda v: f"{round(float(v) * 100)}%" if v else "n/a"
    return True, f"5 小時 {pct(s)}、每週 {pct(w)}"


# --------------------------------------------------------------------------- #
#  claude setup-token
# --------------------------------------------------------------------------- #
def find_claude():
    exe = shutil.which("claude")
    if exe:
        return exe
    # 常見安裝位置（PATH 沒設好時）
    home = Path.home()
    for p in [home / ".claude" / "local" / "claude",
              home / ".local" / "bin" / "claude",
              home / ".npm-global" / "bin" / "claude",
              Path("/usr/local/bin/claude"),
              Path("/opt/homebrew/bin/claude"),
              Path(os.environ.get("APPDATA", home)) / "npm" / "claude.cmd",
              home / ".local" / "bin" / "claude.exe"]:
        if p.is_file():
            return str(p)
    return None


def run_setup_token():
    exe = find_claude()
    if not exe:
        print("找不到 claude 指令。請先安裝 Claude Code，或在任一終端機手動執行 "
              "`claude setup-token`，再用 --skip-setup 貼上 token。")
        return False
    print("\n即將執行 `claude setup-token`：")
    print("  1. 瀏覽器會開啟 Claude 登入頁，登入並授權")
    print("  2. 完成後終端機會印出一串 sk-ant-oat 開頭的 token，把它整串複製起來")
    print("  3. 回到這裡貼上\n")
    input("按 Enter 開始…")
    try:
        subprocess.run([exe, "setup-token"])
    except OSError as e:
        print(f"無法執行 {exe}：{e}")
        return False
    return True


def ask_token():
    print()
    tok = getpass.getpass("貼上 token（輸入不會顯示，貼上後按 Enter）: ").strip()
    tok = re.sub(r"\s+", "", tok)   # 終端機換行可能把長 token 斷成多段
    if not tok:
        sys.exit("沒有輸入 token")
    if not tok.startswith("sk-ant-oat"):
        print("警告：setup-token 產生的 token 通常以 sk-ant-oat 開頭，請確認有整串複製")
    return tok


# --------------------------------------------------------------------------- #
#  寫入板子（序列埠）
# --------------------------------------------------------------------------- #
def find_port():
    from serial.tools import list_ports
    ports = [p for p in list_ports.comports()
             if p.vid in BOARD_VIDS and p.pid not in BOARD_PIDS_EXCLUDE]
    # macOS 同一個裝置有 /dev/cu.* 與 /dev/tty.*，用 cu
    ports.sort(key=lambda p: "/dev/tty." in p.device)
    return ports[0].device if ports else None


def write_to_board(tok, port_arg):
    try:
        import serial
    except ImportError:
        print("寫入板子需要 pyserial：" +
              ("pip install pyserial" if SYSTEM == "Windows" else "python3 -m pip install pyserial"))
        return False

    port = port_arg or find_port()
    if not port:
        print("找不到開發板的序列埠，請確認 USB 線已接上，或用 --port 指定。")
        if SYSTEM == "Darwin":
            print("macOS 若看不到 /dev/cu.wchusbserial* 或 /dev/cu.usbserial*，需要安裝 WCH CH34x/CH9102 驅動。")
        elif SYSTEM == "Linux":
            print("Linux 裝置通常是 /dev/ttyACM0 或 /dev/ttyUSB0。")
        return False

    ser = serial.Serial()
    ser.port = port
    ser.baudrate = 115200
    ser.timeout = 0.2
    ser.dtr = False   # 開埠時不要拉 DTR/RTS，否則 ESP32 會被重置
    ser.rts = False
    try:
        ser.open()
    except Exception as e:
        print(f"無法開啟 {port}：{e}")
        if SYSTEM == "Windows":
            print("若 start_bridge.bat 正在執行，它會占用序列埠，請先關掉它的視窗。")
        elif SYSTEM == "Linux" and "ermission" in str(e):
            print("沒有序列埠權限：sudo usermod -aG dialout $USER，登出再登入後重試。")
        else:
            print("若 usage_bridge.py 正在執行，它會占用序列埠，請先關掉它。")
        return False

    def wait_line(want, timeout):
        end = time.time() + timeout
        while time.time() < end:
            line = ser.readline().decode("utf-8", "ignore").strip()
            if line.startswith(want):
                return line
        return None

    try:
        wait_line("ready", 2.5)   # 開埠時板子可能重開機，先等它
        ser.reset_input_buffer()
        ser.write((json.dumps({"cfg": {"token": tok}}, separators=(",", ":")) + "\n").encode())
        ok = wait_line("cfg ok", 5) is not None
    finally:
        ser.close()
    if ok:
        print(f"已寫入板子（{port}），板子重新啟動中")
    else:
        print("板子沒有回應，請確認韌體已更新、連接埠正確")
    return ok


# --------------------------------------------------------------------------- #
#  這台電腦 Claude Code 的登入 token
# --------------------------------------------------------------------------- #
def _credential_files():
    if override := os.environ.get("CLAUDE_CREDENTIALS_PATH"):
        return [Path(override)]
    if config_dir := os.environ.get("CLAUDE_CONFIG_DIR"):
        return [Path(config_dir) / ".credentials.json"]
    home = Path.home()
    files = [home / ".claude" / ".credentials.json"]
    if SYSTEM == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        files += [local / "Claude" / ".credentials.json", roaming / "Claude" / ".credentials.json"]
    elif SYSTEM == "Linux":
        xdg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        files += [xdg / "claude" / ".credentials.json"]
    return files


def _keychain_blob():
    """macOS：Claude Code 把登入資料存在鑰匙圈的 "Claude Code-credentials"。"""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s",
                            "Claude Code-credentials", "-w"],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _parse_credentials(blob):
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
        return ({"accessToken": m.group(1)} if m else None)
    if isinstance(data, dict):
        for d in [data, *[v for v in data.values() if isinstance(v, dict)]]:
            if isinstance(d.get("accessToken"), str) and d["accessToken"].strip():
                return d
    return None


def read_local_credentials():
    """回傳 (來源說明, 憑證 dict) 或 (None, None)"""
    if SYSTEM == "Darwin":
        blob = _keychain_blob()
        if blob and (cred := _parse_credentials(blob)):
            return "macOS 鑰匙圈 (Claude Code-credentials)", cred
    for path in _credential_files():
        try:
            blob = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if cred := _parse_credentials(blob):
            return str(path), cred
    return None, None


def show_local():
    src, cred = read_local_credentials()
    if not cred:
        print("找不到這台電腦的 Claude Code 登入資料，請先執行 claude 並登入。")
        if SYSTEM == "Darwin":
            print("（macOS 會跳出鑰匙圈存取確認，請選「允許」）")
        return 1
    tok = cred["accessToken"]
    print(f"系統     : {OS_NAME}")
    print(f"來源     : {src}")
    print(f"token    : {mask(tok)}")
    if cred.get("subscriptionType"):
        print(f"訂閱     : {cred['subscriptionType']}")
    if isinstance(cred.get("expiresAt"), (int, float)):
        left = cred["expiresAt"] / 1000 - time.time()
        exp = time.strftime("%Y-%m-%d %H:%M", time.localtime(cred["expiresAt"] / 1000))
        print(f"到期     : {exp}（" + (f"還有 {int(left // 3600)} 小時 {int(left % 3600 // 60)} 分" if left > 0 else "已過期，執行一次 claude 會自動續期") + "）")
    ok, msg = check_token(tok)
    print(f"查詢測試 : {'OK' if ok else '失敗'} - {msg}")
    print("\n這是 USB 模式 (usage_bridge.py) 用的 token，會自動續期；"
          "不要寫進板子，WiFi 模式請用 setup-token。")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", help="序列埠，例如 COM3、/dev/cu.wchusbserial110、/dev/ttyACM0")
    ap.add_argument("--no-write", action="store_true", help="只取得並驗證，不寫入板子")
    ap.add_argument("--skip-setup", action="store_true", help="不執行 claude setup-token，直接貼上")
    ap.add_argument("--local", action="store_true", help="查看這台電腦 Claude Code 的登入 token")
    args = ap.parse_args()

    if args.local:
        sys.exit(show_local())

    print(f"== 取得板子用的 Claude token（{OS_NAME}）==")
    if not args.skip_setup and not run_setup_token():
        sys.exit(1)
    tok = ask_token()

    print("驗證中…")
    ok, msg = check_token(tok)
    print(("OK - " if ok else "失敗 - ") + msg)
    if not ok:
        sys.exit("token 無法使用，沒有寫入板子")

    if args.no_write:
        print("已驗證（--no-write：未寫入板子）")
        return
    if not write_to_board(tok, args.port):
        print("稍後可用 python get_token.py --skip-setup 重新寫入")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)
