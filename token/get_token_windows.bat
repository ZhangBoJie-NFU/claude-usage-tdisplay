@echo off
rem Windows: double-click to run, or pass arguments from a command prompt, e.g. get_token_windows.bat --local
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
python "%~dp0get_token.py" %*
pause
