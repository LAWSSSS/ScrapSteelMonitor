@echo off
chcp 936 >nul
cd /d "%~dp0..\.."

if not exist ".venv\Scripts\python.exe" (
  echo 还没装依赖，先双击 安装依赖.bat
  pause
  exit /b 1
)
if not exist "monitor\.env" (
  echo 缺少 monitor\.env，先双击 安装依赖.bat，再填写 BINXIN_PG_PASSWORD。
  pause
  exit /b 1
)

echo 镔鑫看板 http://127.0.0.1:8788
echo 盛隆看板在 8765，两个端口不冲突，可以同时开两个窗口。
echo 要连上公司 VPN。点批准即落库，写前自动存快照和回滚脚本。
echo.
".venv\Scripts\python.exe" -m monitor.binxin.dashboard %*
if errorlevel 1 (
  echo 启动失败。常见原因：没连 VPN、monitor\.env 里 BINXIN_PG_PASSWORD 没填、
  echo 或者 8788 被占用（这时用 启动镔鑫看板.bat --port 8789 换个口）。
  pause
)
