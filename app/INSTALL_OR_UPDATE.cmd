@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
if not exist "%~dp0install_update.py" goto missing_package
if not exist "%~dp0bundle_manifest.json" goto missing_package
if exist "%USERPROFILE%\AgentRehearsal\.venv\Scripts\python.exe" goto existing_python
where py >nul 2>&1
if not errorlevel 1 goto python_launcher
where python >nul 2>&1
if not errorlevel 1 goto path_python
echo Python 3.10 or newer was not found. Install Python, then run this file again.
pause
exit /b 1
:existing_python
"%USERPROFILE%\AgentRehearsal\.venv\Scripts\python.exe" -u "%~dp0install_update.py"
goto finished
:python_launcher
py -3 -u "%~dp0install_update.py"
goto finished
:path_python
python -u "%~dp0install_update.py"
goto finished
:missing_package
echo Extract the complete ZIP using Extract All, then run INSTALL_OR_UPDATE.cmd from the extracted folder.
pause
exit /b 1
:finished
set "AR_RESULT=%ERRORLEVEL%"
echo.
if not "%AR_RESULT%"=="0" echo The update or verification stopped. Read the message above.
pause
exit /b %AR_RESULT%
