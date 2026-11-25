@echo off
cd /d "%~dp0"
start "VIce Studio" pythonw.exe "%~dp0launcher\studio\studio_server.py"
