@echo off
REM Jalankan Gow LanguAge Editor (PC / Wine). Butuh Python 3 lengkap (dengan tkinter).
where python >nul 2>nul
if %errorlevel%==0 ( python "%~dp0gow_language_editor.py" ) else ( py -3 "%~dp0gow_language_editor.py" )
pause
