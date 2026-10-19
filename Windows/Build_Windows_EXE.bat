@echo off
setlocal
cd /d "%~dp0"
echo Installing/refreshing PyInstaller for your Windows Python...
py -3 -m pip install --upgrade pyinstaller
if errorlevel 1 goto :err
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name ParityKeeper gui.py
if errorlevel 1 goto :err
echo.
echo Finished: %~dp0dist\ParityKeeper.exe
echo MultiPar must still be installed separately (point the app at par2j64.exe).
pause
exit /b 0
:err
echo Build failed. Install Python 3.10+ from python.org with Tkinter enabled.
pause
exit /b 1
