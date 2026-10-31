@echo off
cd /d "%~dp0"
start "" "http://127.0.0.1:8888/"
where py >nul 2>nul && (py -3 server.py) || (python server.py)
pause
