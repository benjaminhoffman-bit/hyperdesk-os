@echo off
rem HyperDesk OS by SilvaDes: installs for this Windows user only (no admin) into %LOCALAPPDATA%\HyperDesk.
if not exist "%~dp0app\setup.ps1" (echo Unzip the whole HyperDesk OS folder first, then run Install.cmd from it. & pause & exit /b 1)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\setup.ps1" -Source "%~dp0app" %*
if errorlevel 1 pause
