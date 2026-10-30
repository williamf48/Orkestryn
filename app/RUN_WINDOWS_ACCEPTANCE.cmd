@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto venv
where py >nul 2>&1
if not errorlevel 1 goto launcher
python -u acceptance.py %*
goto done
:venv
".venv\Scripts\python.exe" -u acceptance.py %*
goto done
:launcher
py -3 -u acceptance.py %*
:done
set "AR_ACCEPTANCE_RESULT=%ERRORLEVEL%"
echo.
echo Acceptance records and the public evidence ZIP are in acceptance_runs.
if not "%AR_ACCEPTANCE_RESULT%"=="0" echo Review the error above and the saved acceptance record.
pause
exit /b %AR_ACCEPTANCE_RESULT%
