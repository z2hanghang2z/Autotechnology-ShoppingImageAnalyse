@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

rem ============================================================
rem  push.bat - commit and push this project to GitHub
rem  Auto-detects whether a local proxy is needed.
rem  NOTE: this file MUST stay pure ASCII + CRLF line endings.
rem ============================================================

cd /d "%~dp0"

set "PROXY=http://127.0.0.1:7897"

echo.
echo [1/4] Checking network...
set "USE_PROXY="
where curl >nul 2>nul
if errorlevel 1 goto :nocheck

rem try direct first
curl -s -m 6 -o nul https://github.com
if not errorlevel 1 (
    echo       Direct connection OK.
    goto :donecheck
)

rem direct failed, try proxy
curl -s -m 6 -x %PROXY% -o nul https://github.com
if not errorlevel 1 (
    echo       Direct failed, proxy %PROXY% OK.
    set "USE_PROXY=1"
    goto :donecheck
)

echo       [WARN] Neither direct nor proxy reachable. Push will probably fail.
goto :donecheck

:nocheck
echo       [WARN] curl not found, skipping network check.

:donecheck

echo.
echo [2/4] Staging changes...
git add -A
if errorlevel 1 goto :fail

echo.
echo [3/4] Committing...
git diff --cached --quiet
if not errorlevel 1 (
    echo       Nothing new to commit.
    goto :push
)

set "MSG=%~1"
if "%MSG%"=="" set "MSG=update"

git commit -m "%MSG%"
if errorlevel 1 goto :fail

:push
echo.
echo [4/4] Pushing to origin/main...
if defined USE_PROXY (
    git -c http.proxy=%PROXY% -c http.sslBackend=openssl push origin main
) else (
    git -c http.proxy= -c https.proxy= -c http.sslBackend=schannel push origin main
)
if errorlevel 1 goto :fail

echo.
echo [OK] Push finished.
echo.
pause
exit /b 0

:fail
echo.
echo [ERROR] Something failed. See the output above.
echo.
pause
exit /b 1
