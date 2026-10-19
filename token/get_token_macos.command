#!/bin/bash
# macOS: double-click in Finder (run chmod +x get_token_macos.command once first),
# or pass arguments from Terminal, e.g. ./get_token_macos.command --local
cd "$(dirname "$0")" || exit 1
python3 get_token.py "$@"
echo
read -r -p "Press Enter to close..."
