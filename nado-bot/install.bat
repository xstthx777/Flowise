@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка Nado Bot ===

set "PY="
py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3.13 -c "import sys" >nul 2>&1 && set "PY=py -3.13"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info[:2] in ((3,12),(3,13)) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo.
  echo [!] Нужен Python 3.12 или 3.13. Версия 3.14 НЕ подходит: SDK Nado на ней не ставится под Windows.
  echo     Скачай "Windows installer 64-bit" для 3.12: https://www.python.org/downloads/release/python-3128/
  echo     При установке поставь галочку "Add python.exe to PATH" и оставь "py launcher".
  pause
  exit /b 1
)
echo Использую: %PY%

if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || (echo [!] Не удалось создать venv & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo [!] Ошибка установки зависимостей & pause & exit /b 1)

if not exist ".env" copy ".env.example" ".env" >nul
if not exist "config.yaml" copy "config.example.yaml" "config.yaml" >nul

".venv\Scripts\python.exe" -m nadobot keygen

echo.
echo === Почти готово ===
echo Сейчас откроется .env в Блокноте: впиши NADO_OWNER = адрес своего MetaMask, сохрани и закрой Блокнот.
notepad .env
echo.
echo Подключаем Telegram-бота...
".venv\Scripts\python.exe" -m nadobot tgsetup
echo.
echo === Готово ===
echo Запускай tg.bat — дальше всё управление и подключение MetaMask в Telegram.
pause
