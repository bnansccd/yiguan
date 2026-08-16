#!/bin/bash
# 昭德堂健康管理中心业务系统 - Linux/macOS 构建脚本
# Windows 环境请使用 build.bat
set -e

echo "=== 昭德堂健康管理中心业务系统 - 构建工具 ==="

# 1. 安装依赖
echo "[1/3] 安装Python依赖..."
pip install -r requirements.txt

# 2. PyInstaller打包
echo "[2/3] 开始打包..."
pyinstaller clinic.spec --clean --noconfirm

# 3. 完成
echo "[3/3] 打包完成！"
echo "输出文件: dist/昭德堂健康管理中心业务系统"
