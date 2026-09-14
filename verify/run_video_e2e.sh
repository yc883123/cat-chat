#!/usr/bin/env bash
# 视频抽帧前端反推：把「隔离源码实例（真实 API）+ 无头浏览器 + 反推脚本」串在同一个 shell。
#
# 为什么必须串在同一个 shell：分开启动时，服务与浏览器会随 Bash 工具调用结束被回收，
# 检验就变成对着空气跑（历史坑：截图全变成同一张空白帧）。
# 为什么用隔离实例：8765 常驻的是打包版 exe，读内置资源，看不到仓库 public/ 的真实行为。
#
# 用法：bash verify/run_video_e2e.sh
set -u
cd "$(dirname "$0")/.."

PY=".venv/Scripts/python.exe"
NODE="${NAIBA_NODE_BIN:-${USERPROFILE:-$HOME}/.workbuddy/binaries/node/versions/22.22.2-3/node.exe}"
NODE_MODULES="${NAIBA_NODE_MODULES:-${USERPROFILE:-$HOME}/node_modules}"   # playwright 装在这里（仓库里没有 node_modules）
PORT="${NAIBA_TMP_PORT:-8797}"
LOG="verify/_video_e2e_server.log"

echo "== 0/3 清掉可能占着端口的残留实例 =="
for PID in $(netstat -ano 2>/dev/null | grep LISTENING | grep ":$PORT " | awk '{print $5}' | sort -u); do
  echo "  杀掉占用 :$PORT 的 pid=$PID"
  MSYS_NO_PATHCONV=1 taskkill /F /PID "$PID" >/dev/null 2>&1
done

echo "== 1/3 启动隔离源码实例（port=$PORT，真实供应商，data_dir=verify/_tmp_video_e2e/data） =="
NAIBA_TMP_PORT="$PORT" "$PY" verify/_serve_video_e2e.py > "$LOG" 2>&1 &
SERVER_PID=$!

cleanup() {
  kill "$SERVER_PID" 2>/dev/null
  wait "$SERVER_PID" 2>/dev/null
}
trap cleanup EXIT

READY=0
CURL_NOTE=""
for i in $(seq 1 40); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -m 2 "http://127.0.0.1:$PORT/api/health" 2>/dev/null)
  CURL_RC=$?
  if [ "$CODE" = "200" ]; then READY=1; break; fi
  CURL_NOTE="curl rc=$CURL_RC http=$CODE"
  if [ "$i" = "1" ] || [ "$i" = "20" ]; then echo "  [wait $i] $CURL_NOTE"; fi
  sleep 1
done
if [ "$READY" != "1" ]; then
  echo "!! 隔离实例没起来（$CURL_NOTE），日志："; tail -30 "$LOG"
  exit 1
fi
echo "  实例就绪：http://127.0.0.1:$PORT"

echo "== 2/3 跑真实反推（真模型 + 真抽帧 + 截图） =="
NAIBA_SMOKE_BASE="http://127.0.0.1:$PORT" NODE_PATH="$NODE_MODULES" "$NODE" verify/video_e2e_smoke.cjs
CODE=$?

echo "== 3/3 收尾 =="
echo "  服务日志尾部："; tail -12 "$LOG"
echo "  反推退出码 = $CODE"
exit "$CODE"
