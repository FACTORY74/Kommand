@echo off
setlocal
set "PROJECT_ROOT=%~dp0"
set "VENV_PYTHON=%PROJECT_ROOT%.venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
    echo Kommand's Python environment was not found.
    echo Run setup_env.cmd first.
    exit /b 1
)
"%VENV_PYTHON%" "%PROJECT_ROOT%kommand-central\run.py" %*
exit /b %ERRORLEVEL%
