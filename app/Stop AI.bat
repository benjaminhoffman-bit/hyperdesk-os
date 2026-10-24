@echo off
title Stop AI (kill switch)
rem Kill switch before gaming: unloads models, then ends Ollama and HyperDesk so nothing can reload onto the GPU.
rem Opening HyperDesk starts it all again.
set "PATH=%LOCALAPPDATA%\Programs\Ollama;%PATH%"
set "HD_ROOT=%~dp0"
for /f "skip=1 tokens=1" %%m in ('ollama ps 2^>nul') do ollama stop %%m
rem Ollama tray + server + model runners: otherwise any request reloads a model into VRAM
taskkill /f /t /im "ollama app.exe" >nul 2>&1
taskkill /f /t /im ollama.exe >nul 2>&1
rem Then anything running from this install's AgentOS folder: HyperDesk, Home Base, their jobs, the voice worker.
rem The folder comes in HD_ROOT so an apostrophe in the path cannot break the quoting. Twice, so nothing a dying
rem parent was about to spawn survives. Plus the ffmpeg they started (Quick edit), never other children (a browser).
for %%i in (1 2) do powershell -NoProfile -Command "$r = (Join-Path $env:HD_ROOT 'AgentOS').ToLower(); $all = @(Get-CimInstance Win32_Process); $ours = @($all | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.ToLower().Contains($r) } | ForEach-Object { $_.ProcessId }); $all | Where-Object { $ours -contains $_.ProcessId -or ($ours -contains $_.ParentProcessId -and $_.Name -match '^ff(mpeg|probe)\.exe$') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo.
echo AI stopped. Open HyperDesk to start it again.
timeout /t 5 >nul
