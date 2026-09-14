#!/usr/bin/env bash
# 一次性把「本地服务 + 无头 Chrome + 截图脚本」串起来。
# 关键点：三者必须在同一个 shell 生命周期内，否则前两者会随 shell 退出而被杀，
# 表现就是所有截图变成同一张空白帧（23632 字节）。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY=".venv/Scripts/python.exe"
NODE="${NAIBA_NODE_BIN:-${USERPROFILE:-$HOME}/.workbuddy/binaries/node/versions/22.22.2-3/node.exe}"
CHROME="${NAIBA_CHROME_BIN:-C:/Program Files/Google/Chrome/Application/chrome.exe}"
PROFILE="${NAIBA_CDP_PROFILE:-${LOCALAPPDATA:-$HOME/AppData/Local}/Temp/naiba-cdp-manual}"

echo "== 1/4 杀掉残留进程 =="
PowerShell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { \$_.Name -eq 'chrome.exe' -and \$_.CommandLine -like '*9222*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }" 2>/dev/null
sleep 1

echo "== 2/4 启动本地服务 =="
"$PY" server.py > "$ROOT/_manual_server.log" 2>&1 &
SERVER_PID=$!
PORT=""
for i in $(seq 1 30); do
  for p in 8765 8799 8800; do
    code=$(curl -s -o /dev/null -w "%{http_code}" -m 2 "http://127.0.0.1:$p/" 2>/dev/null)
    if [ "$code" = "200" ]; then PORT=$p; break 2; fi
  done
  sleep 3
done
if [ -z "$PORT" ]; then
  echo "!! 服务没起来，日志："; tail -20 "$ROOT/_manual_server.log"
  kill $SERVER_PID 2>/dev/null; exit 1
fi
echo "  服务端口 = $PORT"

echo "== 3/4 启动无头 Chrome =="
"$CHROME" --headless=new --no-sandbox --disable-gpu --remote-debugging-port=9222 \
  --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check \
  > "$ROOT/_manual_chrome.log" 2>&1 &
CHROME_PID=$!
sleep 10
for i in $(seq 1 10); do
  code=$(curl -s -o /dev/null -w "%{http_code}" -m 2 http://127.0.0.1:9222/json/version 2>/dev/null)
  [ "$code" = "200" ] && break
  sleep 3
done
echo "  CDP = $(curl -s -o /dev/null -w '%{http_code}' -m 2 http://127.0.0.1:9222/json/version)"

echo "== 4/4 跑截图 =="
NAIBA_PORT=$PORT "$NODE" scripts/screenshot.mjs
NODE_EXIT=$?
echo "  node exit = $NODE_EXIT"

kill $CHROME_PID 2>/dev/null
kill $SERVER_PID 2>/dev/null
echo "== 完成，图片 $(ls docs/manual/images | wc -l) 张 =="
