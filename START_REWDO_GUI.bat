@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

if exist "%~dp0START_REWDO_GUI.vbs" (
  wscript.exe "%~dp0START_REWDO_GUI.vbs"
  exit /b 0
)

if exist "%~dp0REWDO_GUI.exe" (
  start "" "%~dp0REWDO_GUI.exe"
  exit /b 0
)

echo Nie znaleziono REWDO_GUI.exe.
pause
exit /b 1
