@echo off
title Discord Keyword Sound Bot
cd /d "%~dp0"
echo ===================================================
echo     Discord Keyword Sound Bot (RSS Studios)
echo ===================================================
echo.
python bot.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Bot is gestopt met een foutcode.
    pause
)
