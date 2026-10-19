#!/usr/bin/env bash
# Linux：chmod +x get_token_linux.sh 後執行，例如 ./get_token_linux.sh --local
exec python3 "$(dirname "$0")/get_token.py" "$@"
