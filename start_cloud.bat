@echo off
REM ============================================================
REM  PortfolioIsMoving (Cloud) - start the control panel
REM
REM  Double-click this file. It starts the panel and opens
REM  http://localhost:8000 in your browser automatically.
REM ============================================================
cd /d "%~dp0"

REM Use python from PATH, the py launcher, or common locations.
REM %LOCALAPPDATA% is expanded at runtime so this works for any user.
set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py"
)
if not exist "%PY%" (
  if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe" set "PY=%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe"
  if exist "C:\Python\python.exe" set "PY=C:\Python\python.exe"
)

REM Open the panel in the browser after a short delay
start "" /b cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:8000"

REM Run the panel (stays open until you close the window)
"%PY%" cloud_manager.py

echo.
echo The panel has stopped. Close this window.
pause
