@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Nado Bot - TEST
echo ТЕСТ без кошелька: живые цены Nado, виртуальные деньги. Ctrl+C - стоп.
".venv\Scripts\python.exe" -m nadobot paper
echo.
".venv\Scripts\python.exe" -m nadobot paperreport
pause
