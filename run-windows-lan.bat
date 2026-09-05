@echo off
setlocal
title Project X - Windows LAN Host
color 0B
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [Project X] Creating Python environment...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if errorlevel 1 goto :error
)

echo [Project X] Installing capture and media optimizations...
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :error

".venv\Scripts\python.exe" start.py --no-tunnel --host 0.0.0.0 --port 5001
goto :end

:error
echo.
echo [Project X] Setup failed. Install Python 3.10 or newer and try again.
pause

:end
endlocal
