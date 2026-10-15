@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=

where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0connect-check.py"
) else (
    py "%~dp0connect-check.py"
)

echo.
pause
