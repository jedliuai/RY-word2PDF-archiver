@echo off
chcp 65001 >nul
cd /d "%~dp0"
python "WPS转PDF神器.py"
if errorlevel 1 pause
