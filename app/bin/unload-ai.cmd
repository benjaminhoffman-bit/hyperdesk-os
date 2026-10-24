@echo off
set "PATH=%LOCALAPPDATA%\Programs\Ollama;%PATH%"
for /f "skip=1 tokens=1" %%m in ('ollama ps 2^>nul') do ollama stop %%m
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>nul
