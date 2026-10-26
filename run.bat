@echo off
setlocal enableextensions

cd /d "%~dp0"

if exist "venv\Scripts\activate.bat" (
    set "VENV_ACTIVATE=venv\Scripts\activate.bat"
    goto :ACTIVATE
)
if exist ".venv\Scripts\activate.bat" (
    set "VENV_ACTIVATE=.venv\Scripts\activate.bat"
    goto :ACTIVATE
)

echo [ERROR] Virtual environment (venv) not found!
echo Please create it first using: python -m venv venv
pause
exit /b 1

:ACTIVATE
call "%VENV_ACTIVATE%"

set "FLASK_FLAGS=--debug"
if /i "%~1"=="--production" (
    set "FLASK_FLAGS="
    echo Starting Mizalla Flask App in PRODUCTION mode...
) else (
    echo Starting Mizalla Flask App in DEBUG mode...
)

python -m flask --app mizalla run %FLASK_FLAGS% --host 0.0.0.0 --port 8080


