@echo off
setlocal EnableExtensions

py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  py -3.12 %*
  exit /b %errorlevel%
)

py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  py -3 %*
  exit /b %errorlevel%
)

python -c "import sys" >nul 2>&1
if not errorlevel 1 (
  python %*
  exit /b %errorlevel%
)

echo [BLAD] Nie znaleziono interpretera Python.
echo Zainstaluj Python 3.11-3.13 i zaznacz opcje Python Launcher lub Add Python to PATH.
exit /b 9009
