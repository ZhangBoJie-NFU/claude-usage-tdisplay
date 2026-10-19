@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
title Claude Usage -^> T-Display
python "%~dp0usage_bridge.py" %*
pause
