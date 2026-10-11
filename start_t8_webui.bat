@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 免打包啟動：用系統 Python 執行，啟動後自動開啟瀏覽器
python t8_webui.py %*
if errorlevel 1 pause
