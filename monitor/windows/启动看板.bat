@echo off
chcp 936 >nul
cd /d "%~dp0..\.."

if not exist ".venv\Scripts\python.exe" (
  echo 还没装依赖。先双击 安装依赖.bat
  pause
  exit /b 1
)
if not exist "monitor\.env" (
  echo 缺少 monitor\.env。先双击 安装依赖.bat，再填写 MYSQL_PASSWORD。
  pause
  exit /b 1
)

echo 看板地址 http://127.0.0.1:8765
echo 先连上公司 VPN，再在页面里选工作方式。
echo 关掉本窗口 = 关掉看板。
echo.
".venv\Scripts\python.exe" -m monitor dashboard
if errorlevel 1 (
  echo 启动失败。常见原因：没连 VPN，或 .env 密码不对。
  pause
)
