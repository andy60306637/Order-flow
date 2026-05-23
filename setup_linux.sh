#!/usr/bin/env bash
# OrderFlow — Linux 環境初始化腳本
set -e

# ── 選擇 Python：優先 3.12，其次 3.11，最後 3.10 ──────────────────────────────
pick_python() {
    for py in python3.12 python3.11 python3.10 python3; do
        if command -v "$py" &>/dev/null; then
            echo "$py"
            return
        fi
    done
    echo ""
}

PYTHON=${PYTHON:-$(pick_python)}

echo "[1/5] 確認 Python 版本..."
if [[ -z "$PYTHON" ]] || ! command -v "$PYTHON" &>/dev/null; then
    echo ">>> 找不到 Python 3.10+，透過 deadsnakes PPA 安裝 Python 3.12..."
    sudo apt-get update -qq
    sudo apt-get install -y software-properties-common
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update -qq
    sudo apt-get install -y python3.12 python3.12-venv python3.12-dev
    PYTHON=python3.12
elif [[ "$PYTHON" != "python3.12" ]] && ! command -v python3.12 &>/dev/null; then
    echo ">>> 系統 Python: $PYTHON（非 3.12）"
    echo ">>> 嘗試透過 deadsnakes PPA 安裝 Python 3.12..."
    sudo apt-get update -qq
    sudo apt-get install -y software-properties-common
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update -qq
    sudo apt-get install -y python3.12 python3.12-venv python3.12-dev && PYTHON=python3.12 \
        || echo ">>> Python 3.12 安裝失敗，繼續使用 $PYTHON"
fi

echo ">>> 使用: $PYTHON ($($PYTHON --version))"

echo "[2/5] 建立虛擬環境 .venv ..."
"$PYTHON" -m venv .venv

echo "[3/5] 升級 pip ..."
.venv/bin/pip install --upgrade pip -q

echo "[4/5] 安裝相依套件..."
.venv/bin/pip install -r requirements/server.txt -q
.venv/bin/pip install -r requirements/desktop.txt -q 2>/dev/null || \
    echo ">>> desktop.txt 安裝部分失敗（PyQt6 在 headless server 上可忽略）"

echo "[5/5] 安裝 Node.js 相依（Vue 前端）..."
if command -v npm &>/dev/null; then
    (cd web && npm install)
else
    echo ">>> npm 未找到，跳過前端安裝。請手動執行: cd web && npm install"
fi

echo ""
echo "======================================================"
echo " 環境初始化完成！"
echo " 使用 Python: $($PYTHON --version)"
echo ""
echo " 啟動 GUI:    .venv/bin/python main.py"
echo " 啟動 Server: .venv/bin/python server_main.py"
echo " 前端開發:    cd web && npm run dev"
echo "======================================================"
