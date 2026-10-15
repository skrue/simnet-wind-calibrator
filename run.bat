@echo off
setlocal
rem SimNet Wind Calibrator - Windows virtual-environment launcher.
rem All paths are relative to this script, even when called elsewhere.
pushd "%~dp0"
if errorlevel 1 (
    echo ERROR: Cannot open the application directory.
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python virtual environment in .venv ...
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 -m venv ".venv"
    ) else (
        where python >nul 2>&1
        if errorlevel 1 (
            echo ERROR: Python 3.10 or newer was not found.
            echo Install Python from https://www.python.org/downloads/ and retry.
            goto :failed
        )
        python -m venv ".venv"
    )
    if errorlevel 1 (
        echo ERROR: Creating the virtual environment failed.
        goto :failed
    )
)

".venv\Scripts\python.exe" -c "import prompt_toolkit" >nul 2>&1
if errorlevel 1 (
    echo Installing Python dependencies ...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: Dependency installation failed.
        goto :failed
    )
)

".venv\Scripts\python.exe" wind_calibrator.py %*
set "result=%errorlevel%"
popd
exit /b %result%

:failed
echo.
echo Startup failed. Please check the messages above.
pause
popd
exit /b 1
