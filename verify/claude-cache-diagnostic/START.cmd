@echo off
setlocal
chcp 65001 >nul
title Cat Chat Cache Diagnostic
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0cache-diagnostic.ps1"
if errorlevel 1 (
  echo.
  echo Diagnostic stopped. Please keep any generated report files.
  pause
)
endlocal
