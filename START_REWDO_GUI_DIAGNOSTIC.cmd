@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "APPLOG=%CD%\START_DIAGNOSTIC_LOG.txt"
set "TEMPLOG=%TEMP%\REWDO_STARTER_LAST.log"

echo STARTER REACHED > "%APPLOG%"
echo Date: %DATE% %TIME% >> "%APPLOG%"
echo Folder: %CD% >> "%APPLOG%"
echo STARTER REACHED %DATE% %TIME% > "%TEMPLOG%"
echo Folder: %CD% >> "%TEMPLOG%"

echo ===============================================================
echo  REWDO GUI START DIAGNOSTIC - v20.31
echo ===============================================================
echo.
echo Jeżeli to okno zamknie się natychmiast, uruchom z CMD:
echo   cmd /k START_REWDO_GUI_DIAGNOSTIC.cmd
echo.
echo Logs:
echo   %APPLOG%
echo   %TEMPLOG%
echo.

echo Directory listing: >> "%APPLOG%"
dir /b >> "%APPLOG%" 2>&1

if not exist "REWDO_GUI.exe" (
    echo BŁĄD: nie znaleziono REWDO_GUI.exe w tym folderze.
    echo BŁĄD: brak REWDO_GUI.exe >> "%APPLOG%"
    echo.
    echo Prawdopodobnie uruchamiasz to z folderu źródłowego, a nie z:
    echo   C:\Users\Public\REWDO_GUI_ONEDIR_BUILD\dist\REWDO_GUI_ONEDIR
    echo.
    pause
    exit /b 1
)

echo File check: >> "%APPLOG%"
dir "REWDO_GUI.exe" >> "%APPLOG%" 2>&1

echo.
echo [1/4] Startup diagnostic...
"REWDO_GUI.exe" --startup-diagnostic >> "%APPLOG%" 2>&1
set "RC1=%ERRORLEVEL%"
echo RC startup diagnostic: %RC1%
echo RC startup diagnostic: %RC1% >> "%APPLOG%"

echo.
echo [2/4] Internal CLI test...
"REWDO_GUI.exe" --rewdo-cli --help >> "%APPLOG%" 2>&1
set "RC2=%ERRORLEVEL%"
echo RC CLI: %RC2%
echo RC CLI: %RC2% >> "%APPLOG%"

echo.
echo [3/4] Tk smoke test...
"REWDO_GUI.exe" --smoke-test >> "%APPLOG%" 2>&1
set "RC3=%ERRORLEVEL%"
echo RC smoke-test: %RC3%
echo RC smoke-test: %RC3% >> "%APPLOG%"

if not "%RC1%"=="0" goto :show_fail
if not "%RC2%"=="0" goto :show_fail
if not "%RC3%"=="0" goto :show_fail

echo.
echo [4/4] Uruchamiam GUI...
echo Uruchamiam GUI at %DATE% %TIME% >> "%APPLOG%"
"REWDO_GUI.exe" >> "%APPLOG%" 2>&1
set "RC4=%ERRORLEVEL%"
echo RC GUI: %RC4%
echo RC GUI: %RC4% >> "%APPLOG%"

if not "%RC4%"=="0" goto :show_fail

echo.
echo GUI finished normally.
echo Press any key to close.
pause
exit /b 0

:show_fail
echo.
echo ===============================================================
echo  FAILURE DETECTED
echo ===============================================================
echo.
echo Contents of %APPLOG%:
echo.
type "%APPLOG%"
echo.
if exist "REWDO_GUI_CRASH.log" (
    echo ===============================================================
    echo  REWDO_GUI_CRASH.log
    echo ===============================================================
    type "REWDO_GUI_CRASH.log"
)
echo.
echo Also check:
echo   %TEMPLOG%
echo.
pause
exit /b 1
