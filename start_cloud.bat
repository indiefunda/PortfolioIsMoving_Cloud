@echo off
REM ============================================================
REM  PortfolioIsMoving (Cloud) - start the control panel
REM
REM  Double-click this file. It starts the panel and opens
REM  http://localhost:8000 in your browser automatically.
REM ============================================================
cd /d "%~dp0"

REM Find Python: PATH, the py launcher, or common install locations.
set "PY="
where python >nul 2>nul
if not errorlevel 1 (
  set "PY=python"
  goto :found
)
where py >nul 2>nul
if not errorlevel 1 (
  set "PY=py"
  goto :found
)
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "C:\Python\python.exe" set "PY=C:\Python\python.exe"
if not defined PY (
  echo Python was not found. Install it from https://www.python.org/downloads/
  pause
  exit /b 1
)

:found
REM Open the panel in the browser after a short delay.
REM The panel always uses port 8000 (it refuses to start if the port is
REM taken), so this URL is always right.
start "" /b cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:8000"

REM Run the panel (stays open until you close the window)
"%PY%" cloud_manager.py

echo.
echo The panel has stopped. Close this window.
pause
