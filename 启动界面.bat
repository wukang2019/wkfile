@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
rem Use pythonw from PATH (source-route launcher; the exe route does not need this).
where pythonw >nul 2>nul
if errorlevel 1 (
  echo [X] pythonw not found. Install Python 3.8+ , or use wkfile.exe on a server.
  pause
  exit /b 1
)
start "" pythonw "%~dp0server\wkfile_gui.py"
exit /b 0
