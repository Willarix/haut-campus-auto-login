@echo off
setlocal
call "%~dp0login.bat" --setup %*
exit /b %ERRORLEVEL%
