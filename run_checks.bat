@echo off
title TT_Sniffer
cd /d %~dp0
pip install -q -r requirements-dev.txt 2>nul
python scripts\check.py %*
pause
