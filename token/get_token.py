#!/usr/bin/env python3
"""
Get a Claude token and write it to the T-Display (works on Windows / macOS / Linux).

    python get_token.py               # run claude setup-token -> paste token -> verify -> write to board
    python get_token.py --no-write    # only get and verify, don't write to the board
    python get_token.py --skip-setup  # you already have a setup-token token, paste it directly
    python get_token.py --local       # show this computer's Claude Code login token (masked)
    python get_token.py --port COM3   # serial port (auto-detected by default)

WiFi mode on the board needs a long-lived token from `claude setup-token`.
Claude Code's own login token on this computer (the one --local shows) expires
within hours, and if the board refreshed it, the computer would be logged out -
so never write that one to the board.

The token is only held in memory: it is never saved to disk or printed in full.
Writing to the board needs pyserial (pip install pyserial); verifying does not.
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

# USB-serial chips: CH9102 / CH340 (1A86), CP210x (10C4)
BOARD_VIDS = {0x1A86, 0x10C4}
BOARD_PIDS_EXCLUDE = {0x5722}   # 1A86:5722 is a USB display adapter, not a dev board

SYSTEM = platform.system()      # "Windows" / "Darwin" / "Linux"
OS_NAME = {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(SYSTEM, SYSTEM)


def mask(tok):
    return tok[:14] + "…" + tok[-4:] if len(tok) > 24 else "***"


# --------------------------------------------------------------------------- #
#  Verify a token: send a 1-token request and read the rate-limit headers
# --------------------------------------------------------------------------- #
def check_token(tok):
    """Return (ok?, message)"""
    req = urllib.request.Request(
        API_URL, data=json.dumps(API_BODY).encode(),
        headers=dict(API_HEADERS, Authorization=f"Bearer {tok}"), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            hdrs = resp.headers
    except urllib.error.HTTPError as e:
        hdrs = e.headers   # error responses such as 429 still carry the rate-limit headers
        if e.code in (401, 403):
            return False, f"HTTP {e.code}: token is invalid or expired"
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e) and SYSTEM == "Darwin":
            return False, ("SSL certificate verification failed: run /Applications/Python 3.x/"
                           "Install Certificates.command and try again")
        return False, f"Connection failed: {e.reason}"
    except Exception as e:
        return False, f"Connection failed: {e}"

    s = hdrs.get("anthropic-ratelimit-unified-5h-utilization")
    w = hdrs.get("anthropic-ratelimit-unified-7d-utilization")
    if s is None:
        return False, "No rate-limit headers in the response (not a Claude subscription token?)"
    pct = lambda v: f"{round(float(v) * 100)}%" if v else "n/a"
    return True, f"5-hour {pct(s)}, weekly {pct(w)}"


# --------------------------------------------------------------------------- #
#  claude setup-token
# --------------------------------------------------------------------------- #
def find_claude():
    exe = shutil.which("claude")
    if exe:
        return exe
    # Common install locations (in case PATH isn't set up)
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
        print("The claude command was not found. Install Claude Code first, or run "
              "`claude setup-token` yourself in any terminal and paste the token with --skip-setup.")
        return False
    print("\nAbout to run `claude setup-token`:")
    print("  1. A browser opens the Claude login page - log in and authorize")
    print("  2. When done, the terminal prints a token starting with sk-ant-oat - copy all of it")
    print("  3. Come back here and paste it\n")
    input("Press Enter to start...")
    try:
        subprocess.run([exe, "setup-token"])
    except OSError as e:
        print(f"Could not run {exe}: {e}")
        return False
    return True


def ask_token():
    print()
    tok = getpass.getpass("Paste the token (input is hidden), then press Enter: ").strip()
    tok = re.sub(r"\s+", "", tok)   # the terminal may wrap a long token into several pieces
    if not tok:
        sys.exit("No token entered")
    if not tok.startswith("sk-ant-oat"):
        print("Warning: setup-token tokens usually start with sk-ant-oat - make sure you copied all of it")
    return tok


# --------------------------------------------------------------------------- #
#  Write to the board (serial)
# --------------------------------------------------------------------------- #
def find_port():
    from serial.tools import list_ports
    ports = [p for p in list_ports.comports()
             if p.vid in BOARD_VIDS and p.pid not in BOARD_PIDS_EXCLUDE]
    # macOS lists the same device as /dev/cu.* and /dev/tty.*; use cu
    ports.sort(key=lambda p: "/dev/tty." in p.device)
    return ports[0].device if ports else None


def write_to_board(tok, port_arg):
    try:
        import serial
    except ImportError:
        print("Writing to the board needs pyserial: " +
              ("pip install pyserial" if SYSTEM == "Windows" else "python3 -m pip install pyserial"))
        return False

    port = port_arg or find_port()
    if not port:
        print("Could not find the board's serial port. Check the USB cable, or pass --port.")
        if SYSTEM == "Darwin":
            print("On macOS, if there's no /dev/cu.wchusbserial* or /dev/cu.usbserial*, install the WCH CH34x/CH9102 driver.")
        elif SYSTEM == "Linux":
            print("On Linux the device is usually /dev/ttyACM0 or /dev/ttyUSB0.")
        return False

    ser = serial.Serial()
    ser.port = port
    ser.baudrate = 115200
    ser.timeout = 0.2
    ser.dtr = False   # don't assert DTR/RTS when opening, or the ESP32 resets
    ser.rts = False
    try:
        ser.open()
    except Exception as e:
        print(f"Could not open {port}: {e}")
        if SYSTEM == "Windows":
            print("If start_bridge.bat is running it holds the serial port - close its window first.")
        elif SYSTEM == "Linux" and "ermission" in str(e):
            print("No permission for the serial port: sudo usermod -aG dialout $USER, then log out and back in.")
        else:
            print("If usage_bridge.py is running it holds the serial port - stop it first.")
        return False

    def wait_line(want, timeout):
        end = time.time() + timeout
        while time.time() < end:
            line = ser.readline().decode("utf-8", "ignore").strip()
            if line.startswith(want):
                return line
        return None

    try:
        wait_line("ready", 2.5)   # the board may reboot when the port opens; wait for it
        ser.reset_input_buffer()
        ser.write((json.dumps({"cfg": {"token": tok}}, separators=(",", ":")) + "\n").encode())
        ok = wait_line("cfg ok", 5) is not None
    finally:
        ser.close()
    if ok:
        print(f"Written to the board ({port}); the board is restarting")
    else:
        print("The board did not respond - check that the firmware is up to date and the port is correct")
    return ok


# --------------------------------------------------------------------------- #
#  This computer's Claude Code login token
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
    """macOS: Claude Code stores its login in the Keychain item "Claude Code-credentials"."""
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
    """Return (source description, credential dict) or (None, None)"""
    if SYSTEM == "Darwin":
        blob = _keychain_blob()
        if blob and (cred := _parse_credentials(blob)):
            return "macOS Keychain (Claude Code-credentials)", cred
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
        print("No Claude Code login found on this computer - run claude and log in first.")
        if SYSTEM == "Darwin":
            print("(macOS will ask whether to allow Keychain access - choose \"Allow\")")
        return 1
    tok = cred["accessToken"]
    print(f"System       : {OS_NAME}")
    print(f"Source       : {src}")
    print(f"Token        : {mask(tok)}")
    if cred.get("subscriptionType"):
        print(f"Subscription : {cred['subscriptionType']}")
    if isinstance(cred.get("expiresAt"), (int, float)):
        left = cred["expiresAt"] / 1000 - time.time()
        exp = time.strftime("%Y-%m-%d %H:%M", time.localtime(cred["expiresAt"] / 1000))
        print(f"Expires      : {exp} (" + (f"{int(left // 3600)} h {int(left % 3600 // 60)} min left" if left > 0 else "expired - running claude once refreshes it") + ")")
    ok, msg = check_token(tok)
    print(f"Query test   : {'OK' if ok else 'FAILED'} - {msg}")
    print("\nThis is the token USB mode (usage_bridge.py) uses; it refreshes automatically. "
          "Don't write it to the board - use setup-token for WiFi mode.")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", help="serial port, e.g. COM3, /dev/cu.wchusbserial110, /dev/ttyACM0")
    ap.add_argument("--no-write", action="store_true", help="only get and verify, don't write to the board")
    ap.add_argument("--skip-setup", action="store_true", help="don't run claude setup-token, paste a token directly")
    ap.add_argument("--local", action="store_true", help="show this computer's Claude Code login token")
    args = ap.parse_args()

    if args.local:
        sys.exit(show_local())

    print(f"== Get a Claude token for the board ({OS_NAME}) ==")
    if not args.skip_setup and not run_setup_token():
        sys.exit(1)
    tok = ask_token()

    print("Verifying...")
    ok, msg = check_token(tok)
    print(("OK - " if ok else "FAILED - ") + msg)
    if not ok:
        sys.exit("The token can't be used; nothing was written to the board")

    if args.no_write:
        print("Verified (--no-write: not written to the board)")
        return
    if not write_to_board(tok, args.port):
        print("You can write it again later with: python get_token.py --skip-setup")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled")
        sys.exit(130)
