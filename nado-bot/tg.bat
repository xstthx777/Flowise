@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Nado Bot (Telegram)
if not exist ".venv\Scripts\python.exe" (
  echo Сначала запусти install.bat
  pause
  exit /b 1
)
findstr /b /c:"TELEGRAM_CHAT_ID=" .env | findstr /r /c:"=[0-9-]" >nul || (
  echo Telegram ещё не подключён — запускаю настройку.
  ".venv\Scripts\python.exe" -m nadobot tgsetup
)
:loop
echo Nado Bot работает. Управление — в Telegram. Не закрывай это окно.
".venv\Scripts\python.exe" -m nadobot tg
if errorlevel 1 (
  echo Сервис упал. Перезапуск через 30 секунд... ^(Ctrl+C — отмена^)
  timeout /t 30 /nobreak >nul
  goto loop
)
pause
