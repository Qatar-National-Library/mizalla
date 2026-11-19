@echo off
setlocal enableextensions enabledelayedexpansion

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

rem Default configurations
set "PORT=8080"
set "THREADS=1"
set "POS_IDX=0"

:PARSE_ARGS
if "%~1"=="" goto :RUN_APP

if /i "%~1"=="-p" (
    set "PORT=%~2"
    shift
    shift
    goto :PARSE_ARGS
)
if /i "%~1"=="--port" (
    set "PORT=%~2"
    shift
    shift
    goto :PARSE_ARGS
)
if /i "%~1"=="-t" (
    set "THREADS=%~2"
    shift
    shift
    goto :PARSE_ARGS
)
if /i "%~1"=="--threads" (
    set "THREADS=%~2"
    shift
    shift
    goto :PARSE_ARGS
)
if /i "%~1"=="-h" goto :SHOW_HELP
if /i "%~1"=="--help" goto :SHOW_HELP

rem Handle positional parameters: run.bat [PORT] [THREADS]
if !POS_IDX! EQU 0 (
    set "PORT=%~1"
    set /a POS_IDX+=1
    shift
    goto :PARSE_ARGS
)
if !POS_IDX! EQU 1 (
    set "THREADS=%~1"
    set /a POS_IDX+=1
    shift
    goto :PARSE_ARGS
)

shift
goto :PARSE_ARGS

:SHOW_HELP
echo Usage: run.bat [-p ^| --port PORT] [-t ^| --threads THREADS]
echo        run.bat [PORT] [THREADS]
echo Options:
echo   -p, --port PORT       Port to bind (default: 8080)
echo   -t, --threads THREADS Number of worker threads (default: 1)
exit /b 0

:RUN_APP
echo Starting Mizalla with Gunicorn on 0.0.0.0:%PORT% (threads: %THREADS%)...

python -m gunicorn --bind 0.0.0.0:%PORT% --threads %THREADS% "mizalla:create_app()"
