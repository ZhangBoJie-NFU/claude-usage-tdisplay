#!/usr/bin/env python3
"""
把 WiFi 與 Claude token 寫進 T-Display（存在板子的 NVS，不寫進原始碼）。

    python provision.py --wifi "SSID" "密碼"
    python provision.py --token          # 隱藏輸入，貼上 `claude setup-token` 產生的 token
    python provision.py --status         # 查看板子目前狀態（不含 token）
    python provision.py --clear-wifi     # 清除板子上的 WiFi 帳密

模式切換（USB / WiFi）用板子的右鍵；WiFi 也可以長按右鍵 5 秒後用手機掃 QR code 設定。

可加 --port COMx（預設自動偵測）。需要 pyserial。
"""

import argparse
import getpass
import json
import sys
import time

from usage_bridge import find_port, open_serial


def wait_line(ser, want, timeout):
    """讀到以 want 開頭的一行就回傳該行；逾時回傳 None。"""
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
        tok = getpass.getpass("貼上 token（輸入不會顯示）: ").strip()
        if not tok:
            sys.exit("沒有輸入 token")
        if not tok.startswith("sk-ant-oat"):
            print("警告：token 通常以 sk-ant-oat 開頭，請確認是 `claude setup-token` 產生的")
        cfg = {"token": tok}

    port = args.port or find_port()
    if not port:
        sys.exit("找不到開發板的序列埠，請用 --port 指定")
    ser = open_serial(port)
    # 開埠時板子可能重開機，先等它說 ready（沒重開就直接逾時繼續）
    wait_line(ser, "ready", 2.5)
    ser.reset_input_buffer()

    if cfg is not None:
        send(ser, {"cfg": cfg})
        if wait_line(ser, "cfg ok", 5):
            print(f"已寫入 {', '.join(cfg)}，板子重新啟動中")
        else:
            sys.exit("板子沒有回應，請確認韌體已更新")
    else:
        send(ser, {"cmd": "status"})
        line = wait_line(ser, "{", 5)
        print(line or "板子沒有回應")
    ser.close()


if __name__ == "__main__":
    main()
