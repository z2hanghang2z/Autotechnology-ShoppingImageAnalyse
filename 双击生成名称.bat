@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem  Double-click launcher for:  run.bat "referexcel\work.xlsx"
rem  Keep this file in the same folder as run.bat.
rem ============================================================

if not exist "%~dp0run.bat" goto :norun
if not exist "referexcel\work.xlsx" goto :noxlsx

echo ============================================================
echo   Generate product titles
echo   Input : referexcel\work.xlsx
echo ============================================================
echo.

rem run.bat prints its own summary and pauses before exiting,
rem so this launcher does not pause again on the happy path.
call "%~dp0run.bat" "referexcel\work.xlsx"
set "RC=%ERRORLEVEL%"
exit /b %RC%

:norun
echo.
echo [ERROR] run.bat not found in this folder.
echo         Keep this launcher together with run.bat.
echo.
echo         Current folder:
echo         %~dp0
goto :err

:noxlsx
echo.
echo [ERROR] Input file not found:
echo         %~dp0referexcel\work.xlsx
echo.
echo         Put your Excel file into the referexcel folder
echo         and name it  work.xlsx
echo.
echo         To use another file, run run.bat directly:
echo             run.bat "referexcel\your-file.xlsx"
goto :err

:err
echo.
pause
exit /b 1
