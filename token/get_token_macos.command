#!/bin/bash
# macOS：在 Finder 雙擊執行（第一次先 chmod +x get_token_macos.command），
# 或在終端機加參數，例如 ./get_token_macos.command --local
cd "$(dirname "$0")" || exit 1
python3 get_token.py "$@"
echo
read -r -p "按 Enter 關閉…"
