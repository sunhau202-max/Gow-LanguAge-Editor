@echo off
REM Build jadi folder .exe (sama seperti yang dijalankan GitHub Actions)
python -m pip install --upgrade pyinstaller
python -m PyInstaller --onedir --windowed --noconfirm --name GowLanguageEditor "%~dp0gow_language_editor.py"
echo Hasil: folder dist\GowLanguageEditor (bawa SELURUH folder)
if not defined CI pause
