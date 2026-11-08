@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Nado Bot
echo Бот запускается. Чтобы остановить: Ctrl+C (ордера будут сняты). Не закрывай окно крестиком.
".venv\Scripts\python.exe" -m nadobot run
echo.
echo Бот остановлен. Лог: data\bot.log
pause
