@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "VERSION=20.31"
set "LOG=%CD%\BUILD_ONEDIR_LOG.txt"
set "TEMPLOG=%TEMP%\REWDO_BUILD_LAST.log"
set "PUBLIC_ROOT=%PUBLIC%\REWDO_GUI_ONEDIR_BUILD"
call :SET_PATHS

> "%LOG%" echo REWDO v%VERSION% - SINGLE EXE ONEDIR BUILD
>>"%LOG%" echo Start: %DATE% %TIME%
>>"%LOG%" echo Source folder: %CD%
> "%TEMPLOG%" echo REWDO v%VERSION% build reached at %DATE% %TIME%

echo ===============================================================
echo  REWDO v%VERSION% - GUI ONEDIR EXE BUILD
echo  SINGLE EXE: REWDO_GUI.exe
echo ===============================================================
echo.
echo This build makes one executable only:
echo   REWDO_GUI.exe
echo The same EXE runs GUI and internal CLI helper.
echo.

echo Installed Python runtimes according to py launcher: >> "%LOG%"
py -0p >> "%LOG%" 2>&1

set "PYTHON_CMD="

py -3.12 -c "import sys; print(sys.executable); print(sys.version)" >> "%LOG%" 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py -3.12"
    goto :python_found
)

py -c "import sys; print(sys.executable); print(sys.version)" >> "%LOG%" 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py"
    goto :python_found
)

python -c "import sys; print(sys.executable); print(sys.version)" >> "%LOG%" 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    goto :python_found
)

echo ERROR: No Python found.
>>"%LOG%" echo ERROR: No Python found.
pause
exit /b 1

:python_found
echo Using Python command: %PYTHON_CMD%
>>"%LOG%" echo Using Python command: %PYTHON_CMD%
%PYTHON_CMD% -c "import sys; print('Executable:', sys.executable); print('Version:', sys.version)" >> "%LOG%" 2>&1

echo.
echo Installing/updating build requirements...
%PYTHON_CMD% -m pip install --upgrade pip >> "%LOG%" 2>&1
%PYTHON_CMD% -m pip install -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 (
    echo ERROR: pip install failed.
    type "%LOG%"
    pause
    exit /b 1
)

echo.
echo Closing previous REWDO_GUI.exe instances, if any...
>>"%LOG%" echo Closing previous REWDO_GUI.exe instances, if any...
taskkill /IM REWDO_GUI.exe /F /T >> "%LOG%" 2>&1
timeout /t 2 /nobreak >nul

echo.
echo Preparing short build folder:
echo   %SHORT_SRC%
>>"%LOG%" echo Preparing short build folder: %SHORT_SRC%

call :SAFE_RMDIR "%PUBLIC_ROOT%"
if exist "%PUBLIC_ROOT%" (
    echo.
    echo WARNING: Old build folder is still locked:
    echo   %PUBLIC_ROOT%
    echo Using a fresh fallback build folder instead.
    >>"%LOG%" echo WARNING: Old build folder locked, switching to fallback folder.

    for /f %%I in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%I"
    set "PUBLIC_ROOT=%PUBLIC%\REWDO_GUI_ONEDIR_BUILD_v%VERSION%_!STAMP!_!RANDOM!"
    call :SET_PATHS
    >>"%LOG%" echo Fallback build folder: !PUBLIC_ROOT!
)

mkdir "%SHORT_SRC%" >> "%LOG%" 2>&1
mkdir "%SHORT_BUILD%" >> "%LOG%" 2>&1
mkdir "%SHORT_DIST%" >> "%LOG%" 2>&1

echo Copying source files...
robocopy "%CD%" "%SHORT_SRC%" /E /R:3 /W:1 /XD "__pycache__" "build" "dist" ".git" ".venv" "docs\archive" /XF "BUILD_ONEDIR_LOG.txt" "REWDO_STARTUP_DIAGNOSTIC.log" "RUN_SOURCE_GUI_NO_EXE.bat" "*.mdat" >> "%LOG%" 2>&1
if errorlevel 8 (
    echo ERROR: robocopy failed.
    type "%LOG%"
    pause
    exit /b 1
)

if exist "%CD%\90_BW48_main30.mdat" (
    copy /Y "%CD%\90_BW48_main30.mdat" "%SHORT_SRC%\90_BW48_main30.mdat" >> "%LOG%" 2>&1
) else (
    >>"%LOG%" echo WARNING: 90_BW48_main30.mdat not found; Projektant biquadów akustycznych will reconstruct reference from RAW.
)

pushd "%SHORT_SRC%"
echo.
echo Building with PyInstaller...
>>"%LOG%" echo Running PyInstaller in %CD%
%PYTHON_CMD% -m PyInstaller --noconfirm --clean --distpath "%SHORT_DIST%" --workpath "%SHORT_BUILD%" REWDO_onedir.spec >> "%LOG%" 2>&1
set "BUILD_RC=%ERRORLEVEL%"
popd

if not "%BUILD_RC%"=="0" (
    echo ERROR: PyInstaller build failed.
    echo.
    type "%LOG%"
    pause
    exit /b %BUILD_RC%
)

if not exist "%FINAL_APP%\REWDO_GUI.exe" (
    echo ERROR: Build finished but REWDO_GUI.exe was not created.
    >>"%LOG%" echo ERROR: Missing %FINAL_APP%\REWDO_GUI.exe
    type "%LOG%"
    pause
    exit /b 1
)

copy /Y "%CD%\USER_REFERENCE_90BW48.yaml" "%FINAL_APP%\USER_REFERENCE_90BW48.yaml" >> "%LOG%" 2>&1
copy /Y "%CD%\USER_REFERENCE_102BW48.yaml" "%FINAL_APP%\USER_REFERENCE_102BW48.yaml" >> "%LOG%" 2>&1
copy /Y "%CD%\USER_REFERENCE_SLOT4MAX_90BW48.yaml" "%FINAL_APP%\USER_REFERENCE_SLOT4MAX_90BW48.yaml" >> "%LOG%" 2>&1
copy /Y "%CD%\USER_LISTENING_PROFILE.yaml" "%FINAL_APP%\USER_LISTENING_PROFILE.yaml" >> "%LOG%" 2>&1
if exist "%CD%\90_BW48_main30.mdat" copy /Y "%CD%\90_BW48_main30.mdat" "%FINAL_APP%\90_BW48_main30.mdat" >> "%LOG%" 2>&1
copy /Y "%CD%\START_REWDO_GUI.vbs" "%FINAL_APP%\START_REWDO_GUI.vbs" >> "%LOG%" 2>&1
copy /Y "%CD%\START_REWDO_GUI_DIAGNOSTIC.cmd" "%FINAL_APP%\START_REWDO_GUI_DIAGNOSTIC.cmd" >> "%LOG%" 2>&1
copy /Y "%LOG%" "%FINAL_APP%\BUILD_ONEDIR_LOG.txt" >nul 2>&1

echo.
echo Running EXE startup diagnostic...
pushd "%FINAL_APP%"
echo DIAG START > START_DIAGNOSTIC_LOG.txt
"REWDO_GUI.exe" --startup-diagnostic >> "START_DIAGNOSTIC_LOG.txt" 2>&1
set "DIAG_RC=%ERRORLEVEL%"
"REWDO_GUI.exe" --rewdo-cli --help >> "START_DIAGNOSTIC_LOG.txt" 2>&1
set "CLI_RC=%ERRORLEVEL%"
popd

>>"%LOG%" echo Startup diagnostic RC=%DIAG_RC%, CLI RC=%CLI_RC%

if not "%DIAG_RC%"=="0" (
    echo WARNING: EXE startup diagnostic failed. You can still inspect:
    echo   %FINAL_APP%\START_DIAGNOSTIC_LOG.txt
    echo   %FINAL_APP%\REWDO_GUI_CRASH.log
) else (
    echo Startup diagnostic OK.
)

echo.
echo ===============================================================
echo BUILD DONE
echo Folder:
echo   %FINAL_APP%
echo.
echo Start from that folder:
echo   REWDO_GUI.exe  albo  START_REWDO_GUI.vbs
echo ===============================================================
echo.

explorer "%FINAL_APP%"
pause
exit /b 0


:SET_PATHS
set "SHORT_SRC=%PUBLIC_ROOT%\src"
set "SHORT_BUILD=%PUBLIC_ROOT%\build"
set "SHORT_DIST=%PUBLIC_ROOT%\dist"
set "FINAL_APP=%SHORT_DIST%\REWDO_GUI_ONEDIR"
exit /b 0


:SAFE_RMDIR
set "TARGET=%~1"
if not exist "%TARGET%" exit /b 0

for /L %%N in (1,1,5) do (
    >>"%LOG%" echo Delete attempt %%N: %TARGET%
    rmdir /s /q "%TARGET%" >> "%LOG%" 2>&1
    if not exist "%TARGET%" exit /b 0
    timeout /t 2 /nobreak >nul
)

>>"%LOG%" echo WARNING: Could not delete locked folder: %TARGET%
exit /b 1
