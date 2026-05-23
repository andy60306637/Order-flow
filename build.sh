#!/usr/bin/env bash
# OrderFlow — Linux 一鍵打包腳本（對應 Windows build.bat）
set -e

PYTHON=.venv/bin/python

echo "[1/3] 確認 PyInstaller..."
if ! $PYTHON -m PyInstaller --version &>/dev/null; then
    echo "安裝 PyInstaller..."
    .venv/bin/pip install pyinstaller -q
fi

echo "[2/3] 清理舊的建置產物..."
rm -rf build dist OrderFlow 2>/dev/null || true

echo "[3/3] 開始打包..."
$PYTHON -m PyInstaller orderflow.spec --clean

if [ $? -eq 0 ]; then
    cp dist/OrderFlow ./OrderFlow 2>/dev/null || true
    echo ""
    echo "======================================================"
    echo " 打包成功！輸出：dist/OrderFlow"
    echo "======================================================"
else
    echo "[錯誤] 打包失敗，請查看上方錯誤訊息。"
    exit 1
fi
