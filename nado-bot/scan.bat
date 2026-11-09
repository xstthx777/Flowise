@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Nado Bot - SCAN
set MIN=%1
if "%MIN%"=="" set MIN=60
echo Сканер рынков на %MIN% минут: живые данные Nado, кошелёк не нужен.
".venv\Scripts\python.exe" -m nadobot scan %MIN%
pause
