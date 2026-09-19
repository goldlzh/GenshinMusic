@echo off
chcp 936 >nul
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 正在请求管理员权限...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0"
echo 正在检查依赖库...
python -m pip install pydirectinput mido pypinyin==0.55.0 >nul 2>&1
python genshin_gui.py
pause
