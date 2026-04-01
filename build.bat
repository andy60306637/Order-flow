@echo off
:: OrderFlow — 一鍵打包腳本
:: 執行前確保已安裝：pip install -r requirements.txt pyinstaller

echo [1/3] 检查 PyInstaller...
python -m PyInstaller --version >nul 2>&1
if %errorlevel% neq 0 (
    echo PyInstaller 未安裝，正在安裝...
    pip install pyinstaller
)

echo [2/3] 清理舊的建置產物...
rmdir /s /q build 2>nul
rmdir /s /q dist  2>nul
del /q OrderFlow.exe 2>nul

echo [3/3] 開始打包...
python -m PyInstaller orderflow.spec --clean

if %errorlevel% equ 0 (
    echo.
    echo =====================================================
    echo  打包成功！輸出：dist\OrderFlow.exe
    echo =====================================================
    copy dist\OrderFlow.exe .\OrderFlow.exe >nul
    echo  已複製到專案根目錄：OrderFlow.exe
) else (
    echo.
    echo [錯誤] 打包失敗，請查看上方錯誤訊息。
    exit /b 1
)
