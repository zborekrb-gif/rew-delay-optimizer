@echo off
setlocal EnableExtensions
cd /d "%~dp0"

call "%~dp0RUN_PYTHON.cmd" -m pytest -q
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [BLAD] Testy nie przeszly. Kod: %RC%
) else (
  echo.
  echo [OK] Wszystkie testy przeszly.
)
exit /b %RC%
