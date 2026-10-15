@echo off
setlocal
chcp 65001 >nul
title WinFile SSH Tunnel

echo ============================================================
echo   WinFile - SSH Tunnel Launcher
echo   Run this on the PC where Trae is installed.
echo   It maps 127.0.0.1:PORT  -^>  SERVER:127.0.0.1:PORT
echo ============================================================
echo.

set /p SRV=Server IP or hostname [192.168.0.2]:
if "%SRV%"=="" set SRV=192.168.0.2

set /p SUSER=SSH account on the server:
if "%SUSER%"=="" (
  echo.
  echo [X] SSH account is required, for example: Administrator
  pause
  exit /b 1
)

set DEFPORT=%WINFILES_PORT%
if "%DEFPORT%"=="" set DEFPORT=8788
if exist "%~dp0server\agent.config.json" (
  for /f "tokens=2 delims=:, " %%a in ('findstr /i "port" "%~dp0server\agent.config.json" 2^>nul') do set DEFPORT=%%a
)

echo WinFileServer listens on port %DEFPORT% according to its own config.
echo Use the SAME port here, otherwise the tunnel points at nothing.
echo.
set /p PORT=WinFileServer port on the server [%DEFPORT%]:
if "%PORT%"=="" set PORT=%DEFPORT%

echo.
echo [1/2] Checking %SRV%:22 ...
powershell -NoProfile -Command "$r = Test-NetConnection -ComputerName '%SRV%' -Port 22 -InformationLevel Quiet -WarningAction SilentlyContinue; if ($r) { exit 0 } else { exit 1 }"
if errorlevel 1 (
  echo.
  echo [X] Cannot reach %SRV% port 22.
  echo     1^) Is OpenSSH Server installed and running on the server?
  echo        On the server, in an admin PowerShell:
  echo          Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
  echo          Start-Service sshd
  echo          Set-Service -Name sshd -StartupType Automatic
  echo          New-NetFirewallRule -Name sshd-22 -DisplayName "OpenSSH Server" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
  echo     2^) Are both machines on the same network?
  echo        This PC must be able to ping %SRV% .
  echo.
  pause
  exit /b 1
)

echo [2/2] SSH port reachable. Opening tunnel ...
echo.
echo   Local  : http://127.0.0.1:%PORT%/mcp   ^<-- put this into Trae MCP config
echo   Remote : %SRV% 127.0.0.1:%PORT%
echo.
echo   KEEP THIS WINDOW OPEN while using Trae. Ctrl+C closes the tunnel.
echo.
ssh -N -L %PORT%:127.0.0.1:%PORT% %SUSER%@%SRV%

echo.
echo Tunnel closed.
pause
