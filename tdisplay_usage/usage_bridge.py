#!/usr/bin/env python3
"""
Claude Code usage -> LILYGO T-Display (USB serial)

Periodically sends a tiny 1-token request to the Anthropic API, reads only the
rate-limit headers of the response (5-hour session and weekly usage), and sends
them to the board as one JSON line over the USB serial port. The OAuth token
stays on this computer and is never sent to the board.

Usage:
    python usage_bridge.py              # auto-detect the CH9102/CP210x serial port
    python usage_bridge.py --port COM3
    python usage_bridge.py --once       # query once and print, no board needed (testing)

Requires: pip install pyserial
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

POLL_INTERVAL = 60      # seconds; every query is a real request, don't make it too short
RETRY_INTERVAL = 15     # retry delay after a failed query
RESEND_INTERVAL = 10    # periodically resend the last data so a rebooted board shows it at once

# USB-to-serial chips: CH9102 / CH340 (1A86), CP210x (10C4)
_BOARD_VIDS = {0x1A86, 0x10C4}
_BOARD_PIDS_EXCLUDE = {0x5722}   # 1A86:5722 is a USB display adapter, not the board


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
    """macOS: Claude Code stores its login in the Keychain as "Claude Code-credentials"."""
    import subprocess
    try:
        r = subprocess.run(["security", "find-generic-password", "-s",
                            "Claude Code-credentials", "-w"],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def read_token():
    """Re-read before every query so a token refreshed by Claude Code is picked up."""
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
#  Query usage
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
    """Return (dict for the board, None), or (None, error message) on failure."""
    headers = dict(API_HEADERS, Authorization=f"Bearer {token}")
    req = urllib.request.Request(API_URL, data=json.dumps(API_BODY).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            hdrs = resp.headers
    except urllib.error.HTTPError as e:
        # 429 and other error responses still carry the rate-limit headers
        hdrs = e.headers
        if e.code in (401, 403):
            return None, f"HTTP {e.code}: token invalid, please log in to Claude Code again"
    except Exception as e:
        return None, f"Connection failed: {e}"

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
            "w": -1, "wr": 0,      # -1 = this account has no weekly limit
            "st": h("anthropic-ratelimit-unified-status") or "unknown",
        }
    else:
        return None, "No rate-limit headers in the response"
    return data, None


def with_clock(data):
    """Add the local time (seconds since midnight) so the board can show a clock."""
    lt = time.localtime()
    return dict(data, t=lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)


# --------------------------------------------------------------------------- #
#  Serial port
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
    # Don't assert DTR/RTS when opening, otherwise the ESP32 gets reset
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def run(port_arg):
    last = None          # last successful data
    last_at = 0.0        # when `last` was fetched
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
                    log(f"Connected to {port}")
                    next_send = 0.0
                except Exception as e:
                    log(f"Cannot open {port}: {e}")
            if ser is None:
                time.sleep(3)
                continue

        if now >= next_poll:
            token = read_token()
            if not token:
                data, err = None, "Claude Code token not found (~/.claude/.credentials.json or macOS Keychain)"
            else:
                data, err = poll_usage(token)
            if data:
                last, last_at = data, now
                next_poll = now + POLL_INTERVAL
                log(f"5h {data['s']}%  weekly {data['w']}%  ({data['st']})")
            else:
                next_poll = now + RETRY_INTERVAL
                log(err)
                if last is None:
                    # The board's built-in fonts are ASCII only
                    _write(ser, {"err": "Query failed - see PC"})
            next_send = 0.0

        if last and now >= next_send:
            # When resending, subtract the elapsed time from the countdowns
            el = int(now - last_at)
            msg = dict(last, sr=max(0, last["sr"] - el), wr=max(0, last["wr"] - el))
            if not _write(ser, with_clock(msg)):
                ser = None
                continue
            next_send = now + RESEND_INTERVAL

        # Read messages from the board
        try:
            line = ser.readline().decode("utf-8", "ignore").strip()
        except Exception:
            log("Serial port lost, reconnecting...")
            ser = None
            continue
        if line == "refresh":
            log("Board requested an immediate update")
            next_poll = 0.0
        elif line == "ready":
            next_send = 0.0


def _write(ser, obj):
    try:
        ser.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        return True
    except Exception:
        log("Serial write failed, reconnecting...")
        try:
            ser.close()
        except Exception:
            pass
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", help="serial port, e.g. COM3 (default: auto-detect)")
    ap.add_argument("--once", action="store_true", help="query once and print")
    args = ap.parse_args()

    if args.once:
        token = read_token()
        if not token:
            sys.exit("Claude Code token not found")
        data, err = poll_usage(token)
        print(json.dumps(with_clock(data), ensure_ascii=False) if data else err)
        sys.exit(0 if data else 1)

    try:
        run(args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
