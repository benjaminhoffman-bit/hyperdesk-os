@rem Removes HyperDesk OS. One line on purpose: it deletes its own folder, so cmd must never read this file again.
@cd /d "%TEMP%" & powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" -Uninstall %* & exit /b
