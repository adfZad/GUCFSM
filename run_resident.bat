@echo off
setlocal enabledelayedexpansion

REM Load simple env vars from .env
for /f "tokens=1,2 delims==" %%a in (field-service-bot\.env) do (
    set "key=%%a"
    set "val=%%b"
    if "!key!"=="BOT_TOKEN" set BOT_TOKEN=!val!
    if "!key!"=="AGENT_BOT_TOKEN" set AGENT_BOT_TOKEN=!val!
    if "!key!"=="PHOTO_DIR" set PHOTO_DIR=!val!
    if "!key!"=="LOG_DIR" set LOG_DIR=!val!
)

set DB_CONNECTION_STRING=Driver={ODBC Driver 18 for SQL Server};Server=DESKTOP-5MEJ09S;Database=GUCFSM;Uid=sa;Pwd=Nokia@7610;TrustServerCertificate=yes;

set NOTIFICATIONS_ENABLED=true

cd /d "%~dp0field-service-bot\app"
python bot.py
pause
