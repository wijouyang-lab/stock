@echo off
REM ==========================================================================
REM  cn-stock-screener  --  unregister Windows scheduled tasks
REM  Removes the two tasks created by register_task.bat.
REM  Usage:  unregister_task.bat
REM ==========================================================================
setlocal EnableExtensions
chcp 65001 >nul 2>nul

set "SCRIPT_DIR=%~dp0"
set "TASK_FULL=CNStockScreener_Daily_2230"
set "TASK_QUOTE=CNStockScreener_Quote_1730"

echo ==========================================================================
echo  unregister scheduled tasks for cn-stock-screener
echo ==========================================================================

echo.
echo [1/2] deleting %TASK_FULL% ...
schtasks /Delete /F /TN "%TASK_FULL%"
if errorlevel 1 (
    echo [WARN] %TASK_FULL% was not found or could not be deleted ^(already removed?^)
) else (
    echo [OK] %TASK_FULL% removed.
)

echo.
echo [2/2] deleting %TASK_QUOTE% ...
schtasks /Delete /F /TN "%TASK_QUOTE%"
if errorlevel 1 (
    echo [WARN] %TASK_QUOTE% was not found or could not be deleted ^(already removed?^)
) else (
    echo [OK] %TASK_QUOTE% removed.
)

echo.
echo --------------------------------------------------------------------------
echo  remaining cn-stock-screener tasks ^(should be empty^):
schtasks /Query /FO LIST 2>nul | findstr /I "CNStockScreener"
echo --------------------------------------------------------------------------
echo  Note: files in data/ and output/ are NOT deleted by this script.
echo --------------------------------------------------------------------------
endlocal
exit /b 0
