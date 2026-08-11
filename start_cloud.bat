@echo off
REM ============================================================
REM  PortfolioIsMoving (Cloud) - start the control panel
REM
REM  Double-click this file. It starts the panel and opens
REM  http://localhost:8000 in your browser automatically.
REM ============================================================
cd /d "%~dp0"

REM Use python from PATH, or common locations
set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
  if exist "C:\Users\Achilles\AppData\Local\Python\bin\python.exe" set "PY=C:\Users\Achilles\AppData\Local\Python\bin\python.exe"
  if exist "C:\Python\python.exe" set "PY=C:\Python\python.exe"
)

REM Open the panel in the browser after a short delay
start "" /b cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:8000"

REM Run the panel (stays open until you close the window)
"%PY%" cloud_manager.py

echo.
echo The panel has stopped. Close this window.
pause
