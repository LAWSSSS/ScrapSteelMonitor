@echo off
cd /d "%~dp0\..\.."
echo 请把鼠标放到桌面空白处（不要放在图标/任务栏/浏览器上），然后按任意键保存起点。
pause >nul
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -m monitor.vpn_keepalive --save-origin
) else (
  python -m monitor.vpn_keepalive --save-origin
)
echo 保存后请关掉旧看板窗口，再双击 启动看板.bat
pause
