@echo off
title VIce Pro Gameplay Auditor (DXGI + Flash Detector)
cd /d "%~dp0"
python -u scripts\vice_pro_audit.py --until-exit
echo.
pause
