@echo off
setlocal
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo Virtual env not found. Run install_deps.bat first.
  exit /b 1
)
.venv\Scripts\python.exe bot.py
