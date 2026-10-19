#!/usr/bin/env bash
# Linux: chmod +x get_token_linux.sh, then run e.g. ./get_token_linux.sh --local
exec python3 "$(dirname "$0")/get_token.py" "$@"
