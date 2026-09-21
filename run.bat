@echo off
chcp 65001 >nul
setlocal
set "RC=1"

set "PY=C:\Users\fucker\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe"
set "SCRIPT=%~dp0scripts\run.py"

if not exist "%PY%" goto :nopy
if not exist "%SCRIPT%" goto :noscript

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

"%PY%" "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" goto :failed
echo [OK] Command finished successfully.
goto :end

:failed
echo [WARN] Exit code %RC%. Some checks failed or an error occurred.
echo        Please read the output above.
goto :end

:nopy
echo.
echo [ERROR] Python interpreter not found:
echo         %PY%
goto :end

:noscript
echo.
echo [ERROR] Main script not found:
echo         %SCRIPT%
goto :end

:end
echo.
pause
exit /b %RC%
