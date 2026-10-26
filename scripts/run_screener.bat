@echo off
REM ==========================================================================
REM  cn-stock-screener  --  run entry
REM  Called by Windows Task Scheduler, or double-click, or from any directory.
REM  All paths are derived from %~dp0 (this script's own folder), never from CWD.
REM
REM  Usage:
REM     run_screener.bat              auto mode by current time (scheduled guard)
REM     run_screener.bat full         scheduled, full scan (with financials) 22:30
REM     run_screener.bat quote        scheduled, quote-only scan             17:30
REM     run_screener.bat manual       bypass time window (debug / first run)
REM     run_screener.bat gui          open the desktop window
REM     run_screener.bat <args...>    any other args are forwarded to main.py
REM ==========================================================================
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>nul

REM --- 1. project root = parent folder of this script -------------------------
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." || (echo [ERROR] cannot enter project dir & exit /b 2)
set "PROJECT_DIR=%CD%"

echo ==========================================================================
echo  cn-stock-screener
echo  project dir : %PROJECT_DIR%
echo  invoked from: %SCRIPT_DIR%
echo  time        : %DATE% %TIME%
echo ==========================================================================

REM --- 2. locate python (project venv first, then py launcher, then python) ---
set "PYTHON="
if exist "%PROJECT_DIR%\.venv\Scripts\python.exe" set "PYTHON=%PROJECT_DIR%\.venv\Scripts\python.exe"
if not defined PYTHON (
    where py >nul 2>nul && set "PYTHON=py -3"
)
if not defined PYTHON (
    where python >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
    echo [ERROR] Python not found. Install Python 3.9+ and make sure "python" is on PATH.
    popd
    exit /b 9009
)
echo  python      : %PYTHON%
echo.

REM --- 3. make output/logs writable locations exist ---------------------------
if not exist "%PROJECT_DIR%\output\logs" mkdir "%PROJECT_DIR%\output\logs" >nul 2>nul

REM --- 4. build the argument list ---------------------------------------------
REM  first arg may be a keyword: full / quote / manual / gui
REM  any extra args are appended and forwarded to main.py (e.g. run_screener.bat full --force)
set "ARGS="
set "KEYWORD="
if "%~1"==""      ( set "ARGS=--scheduled" & goto :collect )
if /I "%~1"=="full"   set "KEYWORD=--scheduled --mode full"
if /I "%~1"=="quote"  set "KEYWORD=--scheduled --mode quote"
if /I "%~1"=="manual" set "KEYWORD=--cli --mode full"
if /I "%~1"=="gui"    set "KEYWORD=--gui"
if not defined KEYWORD goto :passthrough
set "ARGS=%KEYWORD%"
shift

:collect
if "%~1"=="" goto :collect_done
set "ARGS=!ARGS! %~1"
shift
goto :collect
:collect_done
goto :run

:passthrough
set "ARGS=%*"

:run

REM --- 5. run (UTF-8 in/out so Chinese logs render correctly) -----------------
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%PROJECT_DIR%"

%PYTHON% "%PROJECT_DIR%\main.py" %ARGS%
set "RC=%ERRORLEVEL%"

echo.
echo --------------------------------------------------------------------------
if "%RC%"=="0" (
    echo [OK] finished, exit code = 0
) else (
    echo [FAILED] exit code = %RC%
)
echo  logs   : %PROJECT_DIR%\output\logs
echo  reports: %PROJECT_DIR%\output\reports
echo --------------------------------------------------------------------------

popd
endlocal & exit /b %RC%
