@echo off
setlocal
set SERVICE_NAME=BasepackTelegramBot

net stop %SERVICE_NAME% >nul 2>&1

cd /d %~dp0
if exist requirements.txt (
  .venv\Scripts\pip.exe install -r requirements.txt
)

net start %SERVICE_NAME%
