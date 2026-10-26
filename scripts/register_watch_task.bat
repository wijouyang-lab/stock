@echo off
REM ==========================================================================
REM  cn-stock-screener  --  register the intraday watcher task (every 15 min)
REM
REM  Creates ONE task:
REM     CNStockScreener_Watch_15m   every 15 minutes, 09:30-15:00, Mon-Fri
REM
REM  The watcher itself skips the lunch break (11:30-13:00) and any time
REM  outside the trading sessions, so a 15-minute cadence is safe.
REM
REM  Usage:  register_watch_task.bat          register / overwrite
REM          register_watch_task.bat query    show status
REM  Remove: schtasks /Delete /TN CNStockScreener_Watch_15m /F
REM
REM  NOTE: keep this file ASCII-only. Chinese comments break cmd parsing
REM        because the file is read with the OEM code page (GBK).
REM ==========================================================================
setlocal EnableExtensions
chcp 65001 >nul 2>nul

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." || (echo [ERROR] cannot enter project dir & exit /b 2)
set "PROJECT_DIR=%CD%"
set "ENTRY=%SCRIPT_DIR%run_watcher.bat"
set "TASK=CNStockScreener_Watch_15m"

echo ==========================================================================
echo  register intraday watcher task
echo  project : %PROJECT_DIR%
echo  entry   : %ENTRY%
echo  schedule: every 15 min, 09:30-15:00, Mon-Fri
echo ==========================================================================

if /I "%~1"=="query" goto :query

if not exist "%ENTRY%" (
    echo [ERROR] entry script not found: %ENTRY%
    popd
    exit /b 3
)

REM  /SC WEEKLY + /D limits to Mon-Fri (/SC DAILY does not accept /D)
REM  /RI 15    = repeat every 15 minutes
REM  /DU 05:30 = repeat for 5.5 hours starting 09:30 (until 15:00)
schtasks /Create /F /TN "%TASK%" /TR "\"%ENTRY%\"" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:30 /RI 15 /DU 05:30 /RL LIMITED
if errorlevel 1 (
    echo [ERROR] failed to create %TASK%.
    echo         If it says "Access is denied", re-run as Administrator.
    popd
    exit /b 1
)

:query
schtasks /Query /TN "%TASK%" /FO LIST 2>nul | findstr /I "TaskName Status Next"
echo.
echo  Remove with:  schtasks /Delete /TN "%TASK%" /F
echo  Alert csv  :  %PROJECT_DIR%\output\alerts
echo --------------------------------------------------------------------------
popd
endlocal
exit /b 0
