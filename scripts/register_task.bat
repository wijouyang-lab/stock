@echo off
REM ==========================================================================
REM  cn-stock-screener  --  register Windows scheduled tasks
REM
REM  Registers two tasks (current user, no admin password needed):
REM     CNStockScreener_Daily_2230   22:30 every trading day  full scan (with financials)
REM     CNStockScreener_Quote_1730   17:30 every trading day  quote-only scan
REM
REM  Both call  <project>\scripts\run_screener.bat  whose path is resolved with
REM  %~dp0, so the tasks keep working no matter where the project folder is moved.
REM
REM  Weekday filter is handled by schtasks (/D MON,TUE,WED,THU,FRI);
REM  holiday filter is handled inside main.py (non-trading days exit quietly).
REM
REM  Usage:  register_task.bat            register (overwrite if exist)
REM          register_task.bat query      show current status
REM ==========================================================================
setlocal EnableExtensions
chcp 65001 >nul 2>nul

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." || (echo [ERROR] cannot enter project dir & exit /b 2)
set "PROJECT_DIR=%CD%"
set "ENTRY=%SCRIPT_DIR%run_screener.bat"

set "TASK_FULL=CNStockScreener_Daily_2230"
set "TASK_QUOTE=CNStockScreener_Quote_1730"
set "DAYS=MON,TUE,WED,THU,FRI"
set "TIME_FULL=22:30"
set "TIME_QUOTE=17:30"

echo ==========================================================================
echo  register scheduled tasks for cn-stock-screener
echo  project dir : %PROJECT_DIR%
echo  entry script: %ENTRY%
echo ==========================================================================

if /I "%~1"=="query" goto :query

if not exist "%ENTRY%" (
    echo [ERROR] entry script not found: %ENTRY%
    popd
    exit /b 3
)

if not exist "%PROJECT_DIR%\.env" (
    echo [WARN] .env not found. The task will run but exit with a token error.
    echo        Please copy .env.example to .env and fill in TUSHARE_TOKEN first.
    echo.
)

echo.
echo [1/2] creating %TASK_FULL%  ^(%TIME_FULL% full scan^) ...
schtasks /Create /F /TN "%TASK_FULL%" /TR "\"%ENTRY%\" full" /SC WEEKLY /D %DAYS% /ST %TIME_FULL% /RL LIMITED
if errorlevel 1 (
    echo [ERROR] failed to create %TASK_FULL%.
    echo         If the message says "Access is denied", re-run this script as Administrator.
    popd
    exit /b 1
)

echo.
echo [2/2] creating %TASK_QUOTE%  ^(%TIME_QUOTE% quote-only scan^) ...
schtasks /Create /F /TN "%TASK_QUOTE%" /TR "\"%ENTRY%\" quote" /SC WEEKLY /D %DAYS% /ST %TIME_QUOTE% /RL LIMITED
if errorlevel 1 (
    echo [ERROR] failed to create %TASK_QUOTE%.
    popd
    exit /b 1
)

echo.
echo --------------------------------------------------------------------------
echo  done. current status:
echo --------------------------------------------------------------------------
goto :query

:query
schtasks /Query /TN "%TASK_FULL%"  /FO LIST 2>nul | findstr /I "TaskName Status Next Schedule Start"
schtasks /Query /TN "%TASK_QUOTE%" /FO LIST 2>nul | findstr /I "TaskName Status Next Schedule Start"
echo.
echo  Run once by hand:      %ENTRY% full
echo  Run now (force):       schtasks /Run /TN "%TASK_FULL%"
echo  Remove tasks:          %SCRIPT_DIR%unregister_task.bat
echo  Outputs:               %PROJECT_DIR%\output
echo --------------------------------------------------------------------------
popd
endlocal
exit /b 0
