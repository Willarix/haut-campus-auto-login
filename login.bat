@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 goto use_python
py -3 "%~dp0auto_login.py" %*
goto finished

:use_python
where python >nul 2>&1
if errorlevel 1 goto missing_python
python "%~dp0auto_login.py" %*
goto finished

:missing_python
echo Python was not found. Install Python 3.9 or later and add it to PATH.
pause
exit /b 1

:finished
set "SCRIPT_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %SCRIPT_EXIT%
