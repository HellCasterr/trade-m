@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Trade M is not installed yet. Run setup_windows.bat first.
  pause
  exit /b 1
)

if not exist ".env" (
  echo Missing .env file. Run setup_windows.bat first.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" run.py
if errorlevel 1 (
  echo.
  echo Trade M stopped with an error. Review the message above.
  pause
)
