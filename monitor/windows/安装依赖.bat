@echo off
chcp 936 >nul
cd /d "%~dp0..\.."

where py >nul 2>nul
if errorlevel 1 (
  echo 没找到 Python。先安装 3.11 或 3.12，勾选 Add python.exe to PATH。
  echo https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 正在创建虚拟环境...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo 创建虚拟环境失败。
    pause
    exit /b 1
  )
)

echo 正在安装依赖...
".venv\Scripts\python.exe" -m pip install -U pip
".venv\Scripts\python.exe" -m pip install -r "monitor\requirements.txt"
if errorlevel 1 (
  echo 依赖安装失败。检查网络后再跑一次本脚本。
  pause
  exit /b 1
)

if not exist "monitor\.env" (
  copy "monitor\.env.example" "monitor\.env" >nul
  echo 已生成 monitor\.env，请用记事本打开填密码：
  echo   盛隆看板要 MYSQL_PASSWORD，镔鑫看板要 BINXIN_PG_PASSWORD。
  echo   只开一个场景就只填对应那一个。
) else (
  echo monitor\.env 已存在，未覆盖。
)

echo.
echo 装好了。下一步：填写密码后，双击 启动看板.bat（盛隆）
echo 或 启动镔鑫看板.bat（镔鑫）。两个可以同时开。
pause
