@echo off
REM ==========================================================================
REM  cn-stock-screener  --  ONE-CLICK RANKING (for everyday use)
REM
REM  What it does:
REM     1. computes the score ranking from the LOCAL cache (no Token needed)
REM     2. opens the reports folder so you can see the CSV
REM
REM  Takes about 5 minutes (it recomputes indicators for ~5000 stocks).
REM  It never goes online, so it works without a Tushare Token.
REM
REM  Want fresh data? Run run_screener.bat full  (needs Token + internet),
REM  or just wait for the 22:30 scheduled task.
REM ==========================================================================
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>nul

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." || (echo [ERROR] cannot enter project dir & pause & exit /b 2)
set "PROJECT_DIR=%CD%"

set "PYTHON="
if exist "%PROJECT_DIR%\.venv\Scripts\python.exe" set "PYTHON=%PROJECT_DIR%\.venv\Scripts\python.exe"
if not defined PYTHON (
    where py >nul 2>nul && set "PYTHON=py -3"
)
if not defined PYTHON (
    where python >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
    echo.
    echo [ERROR] Python not found. Please install Python 3.9+ from python.org
    echo         and tick "Add python.exe to PATH" during setup.
    echo.
    pause
    popd
    exit /b 9009
)

echo ==========================================================================
echo  A-share ranking  /  A gu pai hang bang
echo  project : %PROJECT_DIR%
echo  python  : %PYTHON%
echo --------------------------------------------------------------------------
echo  Computing from local cache, please wait about 5 minutes...
echo  (No internet needed. No Tushare Token needed.)
echo --------------------------------------------------------------------------
echo.

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%PROJECT_DIR%"

%PYTHON% "%PROJECT_DIR%\main.py" --cli --local-only --score
set "RC=%ERRORLEVEL%"

echo.
echo ==========================================================================
if "%RC%"=="0" (
    echo  DONE. Opening the reports folder...
    start "" "%PROJECT_DIR%\output\reports"
) else (
    echo  FAILED with code %RC%
    echo  Try: python main.py --self-test --offline
)
echo ==========================================================================
echo.
pause
popd
endlocal & exit /b %RC%
