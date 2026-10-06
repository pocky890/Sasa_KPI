@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] 安裝套件...
python -m pip install -q -r requirements.txt pyinstaller || goto :fail
echo [2/2] 打包 exe...
python -m PyInstaller --noconfirm --onefile --windowed --name 採購分析報告產生器 --exclude-module matplotlib --exclude-module scipy --exclude-module IPython app.py || goto :fail
echo.
echo 完成: %~dp0dist\採購分析報告產生器.exe
pause
exit /b 0
:fail
echo 失敗, 請看上方錯誤訊息
pause
exit /b 1
