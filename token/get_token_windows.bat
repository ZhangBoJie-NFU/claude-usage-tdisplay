@echo off
rem Windows：雙擊執行，或在命令列加參數，例如 get_token_windows.bat --local
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
python "%~dp0get_token.py" %*
pause
