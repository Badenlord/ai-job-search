@echo off
rem Windows: double-click to open File Sorter in your browser (runs offline).
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 file_sorter_app.py) else (python file_sorter_app.py)
if errorlevel 1 pause
