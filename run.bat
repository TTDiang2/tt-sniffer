@echo off
title TT_Sniffer
cd /d %~dp0
echo ============================================
echo   TT_Sniffer 小作文聚合嗅探器
echo   启动后浏览器打开 http://127.0.0.1:8787
echo   关闭本窗口即停止采集
echo ============================================
python -m app.main
pause
