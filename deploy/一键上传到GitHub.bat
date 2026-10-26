@echo off
chcp 65001 >nul 2>nul
title cn-stock-screener -> GitHub
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0?????GitHub.ps1"
if errorlevel 1 pause
