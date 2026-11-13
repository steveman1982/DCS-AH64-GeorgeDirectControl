@echo off
rem Builds two self-contained programs into .\dist:
rem   config.exe       - the bindings GUI (no console window)
rem   george_ctrl.exe  - the controller (console window, shows what it sends)
rem Both read and write config.json in the same folder as the .exe.
setlocal
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller pygame pynput || goto :fail

rem pkg_resources is excluded: nothing here uses it, and its PyInstaller
rem start-up hook can crash the .exe before the program even starts.
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name config --exclude-module pkg_resources ^
    george_config_gui.py || goto :fail

python -m PyInstaller --noconfirm --clean --onefile --console ^
    --name george_ctrl --exclude-module pkg_resources ^
    --hidden-import pynput.keyboard._win32 ^
    --hidden-import pynput.mouse._win32 ^
    george_control.py || goto :fail

rem Put your current bindings next to the programs (an existing one in dist is kept).
if exist config.json if not exist dist\config.json copy config.json dist\config.json >nul

echo.
echo Done:  dist\config.exe   dist\george_ctrl.exe   dist\config.json
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
exit /b 1
