@echo off
REM ==========================================================================
REM  cn-stock-screener  --  intraday watcher entry
REM  Called by the 15-minute scheduled task (register_watch_task.bat) or by hand.
REM  All paths come from %~dp0, never from the current directory.
REM
REM  Usage:
REM     run_watcher.bat              one check, then exit (for scheduled tasks)
REM     run_watcher.bat loop         keep looping until the session ends
REM     run_watcher.bat once         same as no argument
REM     run_watcher.bat codes 300006.SZ,605577.SH    watch only these codes
REM ==========================================================================
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>nul

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." || (echo [ERROR] cannot enter project dir & exit /b 2)
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
    echo [ERROR] Python not found.
    popd
    exit /b 9009
)

set "ARGS=--watch"
if /I "%~1"=="loop" set "ARGS=--watch --loop"
if /I "%~1"=="once" set "ARGS=--watch"
if /I "%~1"=="codes" (
    shift
    set "ARGS=--watch --watch-codes %~1"
)

echo ==========================================================================
echo  cn-stock-screener  intraday watcher
echo  project : %PROJECT_DIR%
echo  time    : %DATE% %TIME%
echo  args    : %ARGS%
echo ==========================================================================

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%PROJECT_DIR%"

%PYTHON% "%PROJECT_DIR%\main.py" %ARGS%
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo [WARN] watcher exit code = %RC%
)
popd
endlocal & exit /b %RC%
