#!/bin/zsh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="$(command -v python3)"
DEST="$HOME/Library/LaunchAgents/com.slzj.dashboard.plist"
mkdir -p "$REPO/monitor/logs"
sed -e "s|__REPO_ROOT__|${REPO}|g" -e "s|__PYTHON__|${PYTHON}|g" \
  "$REPO/monitor/launchd/com.slzj.dashboard.plist" > "$DEST"
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"
echo "已装看板保活: $DEST"
echo "打开 http://127.0.0.1:8765 ，在页面里选「监控当天」或「复核历史日」"
MOUSE_DEST="$HOME/Library/LaunchAgents/com.slzj.vpn-mouse.plist"
sed -e "s|__REPO_ROOT__|${REPO}|g" -e "s|__PYTHON__|${PYTHON}|g" \
  "$REPO/monitor/launchd/com.slzj.vpn-mouse.plist" > "$MOUSE_DEST"
launchctl unload "$MOUSE_DEST" 2>/dev/null || true
launchctl load "$MOUSE_DEST"
echo "已装 VPN 鼠标保活: $MOUSE_DEST"
echo "当天监控不要装 launchd。需要时在看板点「监控当天」。"
echo "若鼠标不动：系统设置 → 隐私与安全性 → 辅助功能，勾选 python3 / 终端"
