@echo off
setlocal EnableExtensions
set "PYTHONDONTWRITEBYTECODE=1"
set "REWDO_RUNTIME=%LOCALAPPDATA%\REWDO\runtime_20_32"
if not defined LOCALAPPDATA (
    echo LOCALAPPDATA is unavailable. Cannot choose a writable runtime directory.
    exit /b 1
)
if exist "%REWDO_RUNTIME%\Scripts\python.exe" goto :dependencies
py -3.12 -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
if not errorlevel 1 (
    py -3.12 -m venv "%REWDO_RUNTIME%"
    goto :runtime_created
)
py -3 -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
if not errorlevel 1 (
    py -3 -m venv "%REWDO_RUNTIME%"
    goto :runtime_created
)
python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
if not errorlevel 1 (
    python -m venv "%REWDO_RUNTIME%"
    goto :runtime_created
)
echo Install Python 3.12 for Windows with Tcl/Tk and the Python launcher, then run START_REWDO.bat.
exit /b 1
:runtime_created
if not exist "%REWDO_RUNTIME%\Scripts\python.exe" (
    echo Creating the local Python runtime failed.
    exit /b 1
)
:dependencies
"%REWDO_RUNTIME%\Scripts\python.exe" -c "import tkinter, numpy, matplotlib, PIL; from matplotlib.figure import Figure" >nul 2>&1
if errorlevel 1 (
    echo First run: installing application dependencies in your user profile.
    "%REWDO_RUNTIME%\Scripts\python.exe" -m pip install -r "%~dp0requirements-runtime.txt"
    if errorlevel 1 exit /b 1
)
"%REWDO_RUNTIME%\Scripts\python.exe" "%~dp0REWDO_GUI_SINGLE_BOOT.py"
exit /b %ERRORLEVEL%
