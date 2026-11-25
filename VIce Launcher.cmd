@echo off
cd /d "%~dp0"
powershell -NoProfile -WindowStyle Hidden -Command "Get-CimInstance Win32_Process -Filter \"CommandLine like '%studio_server.py%'\" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 1 /nobreak >nul
start "VIce Studio" pythonw.exe "%~dp0launcher\studio\studio_server.py"
