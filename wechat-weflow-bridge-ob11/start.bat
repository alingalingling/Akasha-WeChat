@echo off
cd /d "%~dp0"

rem 绕过本机代理，确保 127.0.0.1 / localhost 直连（WeFlow 与 AstrBot 都在本地）
set NO_PROXY=127.0.0.1,localhost,::1
set no_proxy=127.0.0.1,localhost,::1

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py
) else (
    python main.py
)
pause
