#!/usr/bin/env bash
# 前端检验统一入口：把「隔离源码实例 + 无头浏览器 + 检验脚本」串在同一个 shell 生命周期内。
#
# 为什么必须串在一起：三者分开启动时，服务与浏览器会随 Bash 工具调用结束被回收，
# 检验就变成对着空气跑（历史坑：所有截图变成同一张空白帧）。
# 为什么必须用隔离源码实例：8765 上常驻的是打包版 exe，它读内置资源，
# 改了仓库 public/ 在浏览器里看不到（见 verify/_serve_tmp.py 顶部说明）。
#
# 用法：bash verify/run_frontend_check.sh [检验脚本.cjs]
set -u
cd "$(dirname "$0")/.."

PY=".venv/Scripts/python.exe"
NODE="C:/Users/admin/.workbuddy/binaries/node/versions/22.22.2-3/node.exe"
NODE_MODULES="C:/Users/admin/node_modules"   # playwright 装在这里（仓库里没有 node_modules）
PORT="${NAIBA_TMP_PORT:-8801}"
SCRIPT="${1:-verify/frontend_runtime_settings_check.cjs}"
LOG="verify/_frontend_check_server.log"
# 每次跑用一个全新的隔离根：这样「页面显示的默认值」断言才有意义（不会被上一轮
# 写进 config.json 的 0 污染）。目录留在 verify/ 下，已被 .gitignore 覆盖。
TMP_ROOT="verify/_tmp_frontend_$$"

echo "== 0/3 清掉可能占着端口的残留实例 =="
for PID in $(netstat -ano 2>/dev/null | grep LISTENING | grep ":$PORT " | awk '{print $5}' | sort -u); do
  echo "  杀掉占用 :$PORT 的 pid=$PID"
  MSYS_NO_PATHCONV=1 taskkill /F /PID "$PID" >/dev/null 2>&1
done

echo "== 1/3 启动隔离源码实例（port=$PORT，全新 data_dir/config：$TMP_ROOT） =="
NAIBA_TMP_PORT="$PORT" NAIBA_TMP_ROOT="$TMP_ROOT" "$PY" verify/_serve_tmp.py > "$LOG" 2>&1 &
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

echo "== 2/3 跑前端检验：$SCRIPT =="
NAIBA_SMOKE_BASE="http://127.0.0.1:$PORT" NODE_PATH="$NODE_MODULES" "$NODE" "$SCRIPT"
CODE=$?

echo "== 3/3 收尾 =="
echo "  检验退出码 = $CODE"
exit "$CODE"
