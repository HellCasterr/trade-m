@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.11 or newer from https://www.python.org/downloads/windows/
  echo During installation, select "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating the Python environment...
  py -3 -m venv .venv
  if errorlevel 1 goto :failed
)

echo Installing Trade M...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo.
  echo Created .env. Open it in Notepad and enter your Zerodha, Upstox, and/or Dhan credentials.
) else (
  echo Existing .env kept unchanged.
)

echo.
echo Setup complete. Edit .env, then double-click start_windows.bat.
pause
exit /b 0

:failed
echo.
echo Setup failed. Review the error above.
pause
exit /b 1
