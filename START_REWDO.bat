@echo off
setlocal EnableExtensions
if exist "%~dp0aplikacja\REWDO_GUI.exe" (
    start "REWDO" "%~dp0aplikacja\REWDO_GUI.exe"
    exit /b 0
)
call "%~dp0aplikacja\RUN_APP.cmd"
set "REWDO_START_RC=%ERRORLEVEL%"
if not "%REWDO_START_RC%"=="0" pause
exit /b %REWDO_START_RC%
