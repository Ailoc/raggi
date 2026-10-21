#!/usr/bin/env bash
# Raggi 启动脚本
#
#   ./start.sh                 前台启动完成即返回（服务在后台），打印访问地址
#   RAGGI_PORT=9000 ./start.sh 换端口
#   RAGGI_PROCESSES=4 ./start.sh 多 API 进程（默认 1，调试友好）
#   ./start.sh --rebuild       强制重新构建前端
#
# 幂等：**已有实例在跑时不会重复启动**（PID 文件 + 端口双重判定），
# 所以连按两次 start 不会变成两套进程抢同一个端口。

set -euo pipefail
# shellcheck source=scripts/_common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/_common.sh"

FORCE_BUILD=0
for arg in "$@"; do
  case "$arg" in
    --rebuild) FORCE_BUILD=1 ;;
    -h|--help)
      sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) die "未知参数：$arg（用 --help 看用法）" ;;
  esac
done

cd "$ROOT"

# ---- 1. 幂等闸：已有实例就什么都不做 ------------------------------------
# 注意必须**核对端口**：只查 PID 文件的话，`RAGGI_PORT=其它 ./start.sh`
# 会被报成"已在运行（新端口）"——而服务其实还挂在旧端口上，用户以为换成功了。
# （实测踩过：请求 8157、实际服务在 8156，脚本却答"已在运行 8157"。）
if pid="$(running_pid)"; then
  cur_port="$(pid_port "$pid")"
  if [ "$cur_port" = "$PORT" ]; then
    ok "已在运行（PID $pid）：http://$HOST:$PORT"
    info "要换配置请用 ./restart.sh，要停止用 ./stop.sh"
    exit 0
  fi
  die "已有实例在运行，但它在端口 ${cur_port:-?}（PID $pid），而你请求的是 $PORT。
     换端口请用 ./restart.sh（会先收掉旧实例），或先 ./stop.sh 再启动。"
fi
# PID 文件可能是上次异常退出留下的陈旧文件
[ -f "$PID_FILE" ] && { info "清理陈旧 PID 文件"; rm -f "$PID_FILE"; }

# ---- 2. 端口闸：别人的服务绝不误杀 --------------------------------------
if pids="$(port_pids "$PORT")" && [ -n "$pids" ]; then
  if all_own=1; then for p in $pids; do is_own_instance "$p" || all_own=0; done; fi
  if [ "${all_own:-0}" = "1" ]; then
    die "端口 $PORT 已被本项目的另一个实例占用（PID: $(echo $pids)）。
     那多半是手工起过一个：先 ./stop.sh 停掉，或用 ./restart.sh 原地重启。"
  fi
  for p in $pids; do
    if is_raggi_pid "$p"; then
      die "端口 $PORT 被另一个 Raggi 实例占用（PID $p，端口/数据目录与本次不同）。
     清理所有 Raggi 实例：./stop.sh --all"
    fi
    die "端口 $PORT 已被非 Raggi 进程占用（PID $p）：
     $(pid_cmdline "$p" | cut -c1-120)
     换端口：RAGGI_PORT=其它端口 ./start.sh"
  done
fi

# ---- 3. 同项目的其它实例（不同端口/数据目录）：提示但不自动停 -----------
others="$(all_raggi_pids | grep -v -x "${pid:-}" || true)"
if [ -n "$others" ]; then
  warn "检测到另外 $(echo "$others" | wc -l | tr -d ' ') 个 Raggi 实例在跑："
  for p in $others; do
    printf '    PID %-8s %s\n' "$p" "$(pid_cmdline "$p" | cut -c1-100)" >&2
  done
  warn "它们会各占一个端口。要一次清干净：./stop.sh --all"
fi

# ---- 4. 前端产物 ---------------------------------------------------------
if [ "$FORCE_BUILD" = "1" ] || { [ "${RAGGI_SKIP_BUILD:-0}" != "1" ] && dist_is_stale; }; then
  # 前端工程在**仓库根**（package.json + vite.config.ts，root 指向 web/）：
  # node_modules 与 npm 命令都在根目录跑。曾经这里写的是 cd web，
  # 靠 npm 向上找 package.json 才碰巧能用——路径精确了才好维护。
  if [ ! -d "$ROOT/node_modules" ]; then
    info "安装前端依赖（首次启动）"
    (cd "$ROOT" && npm install --no-fund --no-audit)
  fi
  info "构建前端（web/dist 不入库，产物缺失或比源码旧）"
  (cd "$ROOT" && npm run build)
fi

mkdir -p "$LOG_DIR" "$DATA_DIR"
rotate_log_if_needed

# ---- 5. 启动 -------------------------------------------------------------
info "启动 Raggi：数据 $DATA_DIR · 进程数 $PROCESSES · 日志 $LOG_FILE"
setsid nohup "$PYTHON" -m rag.server serve \
  --data "$DATA_DIR" --host "$HOST" --port "$PORT" \
  --processes "$PROCESSES" \
  >> "$LOG_FILE" 2>&1 < /dev/null &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

# setsid 之后 $! 是 setsid 自己的 pid，而 setsdiv 会立刻 exec 服务——两次
# 之间有窗口期。这里再取一次"真实主进程"：优先用 PID 文件里已经写好的，
# 否则用 $!。无论哪种，失败路径都会清掉 PID 文件。
sleep 0.3
REAL_PID="$(running_pid || echo "$NEW_PID")"
[ "$REAL_PID" != "$NEW_PID" ] && echo "$REAL_PID" > "$PID_FILE"

# ---- 6. 健康检查 ---------------------------------------------------------
if ! wait_http_ok "http://$HOST:$PORT/api/kbs" "$HEALTH_TIMEOUT"; then
  warn "启动超时或未就绪（${HEALTH_TIMEOUT}s），正在回滚"
  stop_pid_group "$REAL_PID" || true
  rm -f "$PID_FILE"
  dump_log_tail
  die "启动失败"
fi

ok "已就绪：${C_BLU}http://$HOST:$PORT${C_OFF}  （API 文档在 /#/apidoc）"
info "进程组 $REAL_PID · 停止用 ./stop.sh · 日志 tail -f \"$LOG_FILE\""
