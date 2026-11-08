@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo СНЯТЬ ВСЕ ОРДЕРА И ЗАКРЫТЬ ПОЗИЦИИ?
pause
".venv\Scripts\python.exe" -m nadobot panic
pause
