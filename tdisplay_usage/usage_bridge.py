#!/usr/bin/env python3
"""
Claude Code 用量 -> LILYGO T-Display (USB 序列埠)

每隔一段時間向 Anthropic API 送出 1 token 的極小請求，只讀回應中的
rate-limit 標頭（5 小時 session 與每週用量），再以一行 JSON 經 USB
序列埠傳給開發板顯示。OAuth token 只留在這台電腦上，不會送到板子。

用法:
    python usage_bridge.py              # 自動找 CH9102/CP210x 序列埠
    python usage_bridge.py --port COM3
    python usage_bridge.py --once       # 只查一次並印出，不連板子（測試用）

需要: pip install pyserial
"""

import argparse
import json
import os
import re
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

POLL_INTERVAL = 60      # 秒；每次查詢都是一個真實請求，不要調太短
RETRY_INTERVAL = 15     # 查詢失敗後多久重試
RESEND_INTERVAL = 10    # 定期重送最後一筆資料，板子重開機後馬上有畫面

# USB 轉序列晶片：CH9102 / CH340 (1A86)、CP210x (10C4)
_BOARD_VIDS = {0x1A86, 0x10C4}
_BOARD_PIDS_EXCLUDE = {0x5722}   # 1A86:5722 是 USB 副螢幕，不是開發板


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
#  Token
# --------------------------------------------------------------------------- #
def _credential_candidates():
    if override := os.environ.get("CLAUDE_CREDENTIALS_PATH"):
        return [Path(override)]
    if config_dir := os.environ.get("CLAUDE_CONFIG_DIR"):
        return [Path(config_dir) / ".credentials.json"]
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    return [
        home / ".claude" / ".credentials.json",
        local / "Claude" / ".credentials.json",
        roaming / "Claude" / ".credentials.json",
    ]


def _keychain_blob():
    """macOS：Claude Code 把登入資料存在鑰匙圈的 "Claude Code-credentials"。"""
    import subprocess
    try:
        r = subprocess.run(["security", "find-generic-password", "-s",
                            "Claude Code-credentials", "-w"],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def read_token():
    """每次查詢前重新讀取，Claude Code 更新 token 後自動跟上。"""
    blobs = []
    if sys.platform == "darwin":
        blobs.append(_keychain_blob)
    for path in _credential_candidates():
        blobs.append(lambda p=path: p.read_text(encoding="utf-8"))
    for get in blobs:
        try:
            blob = get()
        except OSError:
            continue
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            for d in [data, *[v for v in data.values() if isinstance(v, dict)]]:
                tok = d.get("accessToken")
                if isinstance(tok, str) and tok.strip():
                    return tok
        m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
        if m:
            return m.group(1)
    return None


# --------------------------------------------------------------------------- #
#  查詢用量
# --------------------------------------------------------------------------- #
def _pct(v):
    try:
        return int(round(float(v) * 100))
    except (TypeError, ValueError):
        return 0


def _epoch(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def poll_usage(token):
    """回傳要送給板子的 dict；失敗時回傳 (None, 錯誤說明)。"""
    headers = dict(API_HEADERS, Authorization=f"Bearer {token}")
    req = urllib.request.Request(API_URL, data=json.dumps(API_BODY).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            hdrs = resp.headers
    except urllib.error.HTTPError as e:
        # 429 等錯誤回應仍帶有 rate-limit 標頭
        hdrs = e.headers
        if e.code in (401, 403):
            return None, f"HTTP {e.code}：token 失效，請重新登入 Claude Code"
    except Exception as e:
        return None, f"連線失敗：{e}"

    h = lambda name: hdrs.get(name) or ""
    now = int(time.time())
    if h("anthropic-ratelimit-unified-5h-utilization"):
        data = {
            "s": _pct(h("anthropic-ratelimit-unified-5h-utilization")),
            "sr": max(0, _epoch(h("anthropic-ratelimit-unified-5h-reset")) - now),
            "w": _pct(h("anthropic-ratelimit-unified-7d-utilization")),
            "wr": max(0, _epoch(h("anthropic-ratelimit-unified-7d-reset")) - now),
            "st": h("anthropic-ratelimit-unified-5h-status") or "unknown",
        }
    elif h("anthropic-ratelimit-unified-overage-utilization"):
        data = {
            "s": _pct(h("anthropic-ratelimit-unified-overage-utilization")),
            "sr": max(0, _epoch(h("anthropic-ratelimit-unified-overage-reset")) - now),
            "w": -1, "wr": 0,      # -1 = 此帳號沒有每週限制
            "st": h("anthropic-ratelimit-unified-status") or "unknown",
        }
    else:
        return None, "回應中沒有 rate-limit 標頭"
    return data, None


def with_clock(data):
    """附上本地時間（秒，從午夜起算），板子用來顯示時鐘。"""
    lt = time.localtime()
    return dict(data, t=lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)


# --------------------------------------------------------------------------- #
#  序列埠
# --------------------------------------------------------------------------- #
def find_port():
    from serial.tools import list_ports
    for p in list_ports.comports():
        if p.vid in _BOARD_VIDS and p.pid not in _BOARD_PIDS_EXCLUDE:
            return p.device
    return None


def open_serial(port):
    import serial
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = 115200
    ser.timeout = 0.2
    # 開埠時不要拉 DTR/RTS，否則 ESP32 會被重置
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def run(port_arg):
    last = None          # 最後一筆成功的資料
    last_at = 0.0        # 取得 last 的時間
    next_poll = 0.0
    next_send = 0.0
    ser = None
    while True:
        now = time.time()

        if ser is None:
            port = port_arg or find_port()
            if port:
                try:
                    ser = open_serial(port)
                    log(f"已連上 {port}")
                    next_send = 0.0
                except Exception as e:
                    log(f"無法開啟 {port}：{e}")
            if ser is None:
                time.sleep(3)
                continue

        if now >= next_poll:
            token = read_token()
            if not token:
                data, err = None, "找不到 Claude Code token（~/.claude/.credentials.json 或 macOS 鑰匙圈）"
            else:
                data, err = poll_usage(token)
            if data:
                last, last_at = data, now
                next_poll = now + POLL_INTERVAL
                log(f"5h {data['s']}%  每週 {data['w']}%  ({data['st']})")
            else:
                next_poll = now + RETRY_INTERVAL
                log(err)
                if last is None:
                    # 板子的內建字型只有 ASCII
                    _write(ser, {"err": "Query failed - see PC"})
            next_send = 0.0

        if last and now >= next_send:
            # 重送時把倒數扣掉經過的時間
            el = int(now - last_at)
            msg = dict(last, sr=max(0, last["sr"] - el), wr=max(0, last["wr"] - el))
            if not _write(ser, with_clock(msg)):
                ser = None
                continue
            next_send = now + RESEND_INTERVAL

        # 讀板子的訊息：按下按鈕會送 "refresh"
        try:
            line = ser.readline().decode("utf-8", "ignore").strip()
        except Exception:
            log("序列埠中斷，重新連線…")
            ser = None
            continue
        if line == "refresh":
            log("板子要求立即更新")
            next_poll = 0.0
        elif line == "ready":
            next_send = 0.0


def _write(ser, obj):
    try:
        ser.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        return True
    except Exception:
        log("寫入序列埠失敗，重新連線…")
        try:
            ser.close()
        except Exception:
            pass
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", help="序列埠，例如 COM3（預設自動偵測）")
    ap.add_argument("--once", action="store_true", help="只查詢一次並印出")
    args = ap.parse_args()

    if args.once:
        token = read_token()
        if not token:
            sys.exit("找不到 Claude Code token")
        data, err = poll_usage(token)
        print(json.dumps(with_clock(data), ensure_ascii=False) if data else err)
        sys.exit(0 if data else 1)

    try:
        run(args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
