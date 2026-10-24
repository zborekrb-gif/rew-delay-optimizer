@echo off
setlocal EnableExtensions
if exist "%~dp0REWDO_GUI.exe" (
    start "REWDO 20.34-dev2" "%~dp0REWDO_GUI.exe"
    exit /b 0
)
call "%~dp0RUN_APP.cmd"
set "REWDO_START_RC=%ERRORLEVEL%"
if not "%REWDO_START_RC%"=="0" pause
exit /b %REWDO_START_RC%
