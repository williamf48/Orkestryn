@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
set "AR_CAMPAIGN_ARGS="
if exist "%USERPROFILE%\AgentRehearsal\.venv\Scripts\python.exe" goto existing_python
where py >nul 2>&1
if not errorlevel 1 goto python_launcher
where python >nul 2>&1
if not errorlevel 1 goto path_python
echo Python 3.10 or newer was not found. Install it, then try again.
set "AR_CAMPAIGN_RESULT=1"
goto finished
:existing_python
set "AR_CAMPAIGN_PY=%USERPROFILE%\AgentRehearsal\.venv\Scripts\python.exe"
goto run_check
:python_launcher
set "AR_CAMPAIGN_PY=py"
set "AR_CAMPAIGN_ARGS=-3"
goto run_check
:path_python
set "AR_CAMPAIGN_PY=python"
:run_check
echo Installing the complete verified update without starting full acceptance.
"%AR_CAMPAIGN_PY%" %AR_CAMPAIGN_ARGS% -u "%~dp0install_update.py" --install-only
set "AR_CAMPAIGN_RESULT=%ERRORLEVEL%"
if not "%AR_CAMPAIGN_RESULT%"=="0" goto finished
cd /d "%USERPROFILE%\AgentRehearsal"
"%AR_CAMPAIGN_PY%" %AR_CAMPAIGN_ARGS% -u campaign_check.py
set "AR_CAMPAIGN_RESULT=%ERRORLEVEL%"
:finished
echo.
if "%AR_CAMPAIGN_RESULT%"=="0" echo Focused check passed. Full Windows acceptance is still required with RUN_WINDOWS_ACCEPTANCE.cmd.
if not "%AR_CAMPAIGN_RESULT%"=="0" echo Send the first error and the diagnostic_runs summary. Do not infer an acceptance pass.
pause
exit /b %AR_CAMPAIGN_RESULT%
