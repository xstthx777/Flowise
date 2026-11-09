@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Nado Bot - TEST 3 markets
if not exist ".venv\Scripts\python.exe" (
  echo Сначала запусти install.bat
  pause
  exit /b 1
)
set NADO_CONFIG=presets\test3.yaml
echo ТЕСТ без кошелька: SNDK + ETH + BNB, живые цены Nado, виртуальные 300$. Ctrl+C - стоп и отчёт.
".venv\Scripts\python.exe" -m nadobot paper
echo.
".venv\Scripts\python.exe" -m nadobot paperreport
pause
