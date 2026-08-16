@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ================================================
echo   昭德堂健康管理中心业务系统 - Windows 打包工具
echo ================================================
echo.

rem ---------- [1/5] 检查 Python 环境 ----------
echo [1/5] 检查 Python 环境...
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where python3 >nul 2>nul && set "PY=python3"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY (
    echo [错误] 未检测到 Python，请先安装 Python 3.10 或更高版本，
    echo        安装时务必勾选 "Add Python to PATH"。
    echo        下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)"
if errorlevel 1 (
    echo [错误] Python 版本过低，需要 3.10 及以上版本。
    pause
    exit /b 1
)
for /f "delims=" %%v in ('%PY% -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%v"
echo        已找到 Python %PYVER%

rem ---------- [2/5] 安装依赖 ----------
echo [2/5] 安装依赖（首次较慢，请耐心等待）...
%PY% -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo [警告] 在线安装失败，尝试使用国内镜像...
    %PY% -m pip install --disable-pip-version-check -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
)
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络连接后重试。
    pause
    exit /b 1
)

rem ---------- [3/5] PyInstaller 打包 ----------
echo [3/5] 开始打包（约1-3分钟）...
%PY% -m PyInstaller clinic.spec --clean --noconfirm
if errorlevel 1 (
    echo [错误] 打包失败，请向上翻阅日志定位原因。
    pause
    exit /b 1
)

rem ---------- [4/5] 校验产物并生成全新空数据库 ----------
echo [4/5] 校验打包产物...
set "EXE=dist\昭德堂健康管理中心业务系统.exe"
if not exist "%EXE%" (
    echo [错误] 未找到 %EXE%，打包未成功。
    pause
    exit /b 1
)
if exist "dist\clinic.db" del /f /q "dist\clinic.db"
if exist "dist\clinic.db-wal" del /f /q "dist\clinic.db-wal"
if exist "dist\clinic.db-shm" del /f /q "dist\clinic.db-shm"
%PY% -c "import sqlite3; sqlite3.connect(r'dist\clinic.db').close()"
echo        已生成全新空数据库 dist\clinic.db（首次启动自动初始化）

rem ---------- [5/5] 完成 ----------
echo [5/5] 打包完成！
echo ================================================
echo 产物位置: dist\昭德堂健康管理中心业务系统.exe
echo.
echo 发布方法: 将 dist 目录整体复制到目标电脑，双击 exe 即可运行，
echo           无需安装 Python。首次启动会自动创建管理员账号。
echo 初始账号: admin / admin123（登录后请立即修改密码）
echo ================================================
pause
endlocal
