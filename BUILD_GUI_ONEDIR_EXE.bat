@echo off
setlocal EnableExtensions
cd /d "%~dp0"
call "%~dp0RUN_PYTHON.cmd" build_windows_v20_34_dev2.py
set "REWDO_BUILD_RC=%ERRORLEVEL%"
if not "%REWDO_BUILD_RC%"=="0" echo Build failed. Review the printed error and build log.
pause
exit /b %REWDO_BUILD_RC%
