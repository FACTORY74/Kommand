@echo off
setlocal

set "PROJECT_ROOT=%~dp0"
set "VENV_PYTHON=%PROJECT_ROOT%.venv\Scripts\python.exe"
set "SOURCE_LAUNCHER=%PROJECT_ROOT%kommand-source\run.py"

if not exist "%VENV_PYTHON%" (
    echo Kommand's Python environment was not found.
    echo Run setup_env.cmd first.
    exit /b 1
)

"%VENV_PYTHON%" "%SOURCE_LAUNCHER%" %*
exit /b %ERRORLEVEL%

