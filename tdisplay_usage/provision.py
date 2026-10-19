#!/usr/bin/env python3
"""
Write WiFi and the Claude token to the T-Display (stored in the board's NVS, not in the source).

    python provision.py --wifi "SSID" "PASSWORD"
    python provision.py --token          # hidden input; paste the token from `claude setup-token`
    python provision.py --status         # show the board's current status (without the token)
    python provision.py --clear-wifi     # erase the WiFi credentials on the board

Switch modes (USB / WiFi) with the board's right button; WiFi can also be set up from
a phone by holding the right button for 5 seconds and scanning the QR code.

Add --port COMx to pick the port (default: auto-detect). Requires pyserial.
"""

import argparse
import getpass
import json
import sys
import time

from usage_bridge import find_port, open_serial


def wait_line(ser, want, timeout):
    """Return the first line starting with `want`; None on timeout."""
    end = time.time() + timeout
    while time.time() < end:
        line = ser.readline().decode("utf-8", "ignore").strip()
        if line.startswith(want):
            return line
    return None


def send(ser, obj):
    ser.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--wifi", nargs=2, metavar=("SSID", "PASSWORD"))
    g.add_argument("--token", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--clear-wifi", action="store_true")
    args = ap.parse_args()

    cfg = None
    if args.wifi:
        cfg = {"ssid": args.wifi[0], "pass": args.wifi[1]}
    elif args.clear_wifi:
        cfg = {"ssid": "", "pass": ""}
    elif args.token:
        tok = getpass.getpass("Paste the token (input is hidden): ").strip()
        if not tok:
            sys.exit("No token entered")
        if not tok.startswith("sk-ant-oat"):
            print("Warning: the token usually starts with sk-ant-oat; make sure it comes from `claude setup-token`")
        cfg = {"token": tok}

    port = args.port or find_port()
    if not port:
        sys.exit("Board serial port not found, specify it with --port")
    ser = open_serial(port)
    # The board may reboot when the port opens; wait for "ready" (time out if it didn't)
    wait_line(ser, "ready", 2.5)
    ser.reset_input_buffer()

    if cfg is not None:
        send(ser, {"cfg": cfg})
        if wait_line(ser, "cfg ok", 5):
            print(f"Wrote {', '.join(cfg)}, board is restarting")
        else:
            sys.exit("No response from the board, make sure the firmware is up to date")
    else:
        send(ser, {"cmd": "status"})
        line = wait_line(ser, "{", 5)
        print(line or "No response from the board")
    ser.close()


if __name__ == "__main__":
    main()
