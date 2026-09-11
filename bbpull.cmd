@echo off
REM bbpull launcher - double-click this file, or run `bbpull.cmd <subcommand>`.
REM
REM Prefers the project virtual environment (.venv) so a machine with several
REM Pythons always gets the same dependencies and the same GUI engine. Even if
REM this file falls through to a bare `python`, bbpull re-execs itself into
REM .venv once it exists, so the environment cannot drift.
REM
REM Credentials are never stored in this file; the program asks and can save
REM them in Windows DPAPI-encrypted form.
setlocal
set "HERE=%~dp0"
set "PYTHONPATH=%HERE%;%PYTHONPATH%"
chcp 65001 >nul 2>&1

set "VENV_PY=%HERE%.venv\Scripts\python.exe"
if exist "%VENV_PY%" (
  "%VENV_PY%" -m bbpull %*
) else (
  python -m bbpull %*
)
set "CODE=%ERRORLEVEL%"
if "%~1"=="" (
  echo.
  echo [exit code %CODE%]
  pause
)
endlocal
exit /b %CODE%
