#!/usr/bin/env bash
# Raggi 进程管理 · 公共函数库
#
# **不要直接执行本文件**——start.sh / stop.sh / restart.sh 会 source 它。
# 这里只放三者共用的东西：路径解析、配置、端口与进程判定、日志与健康检查。
#
# 设计前提（本项目启动踩过的坑，都在这一层处理）：
#
# 1. **一个实例会变成 6 个进程**：`rag.server serve` 的 `--processes` 默认
#    `auto` = min(cpu, 4)，uvicorn 以 spawn 方式拉起 worker，外加一个
#    multiprocessing 的 resource_tracker。脚本默认 `--processes 1`：调试期
#    断点与日志都想要单进程，需要吞吐时用 `RAGGI_PROCESSES=auto ./start.sh`。
# 2. **端口冲突不能靠"先 kill 再启动"**：那会连别人的服务一起杀掉。这里
#    按端口查出占用者，只在确认它**是 Raggi 自己**时才允许清理，否则直接
#    报错退出。
# 3. **端口和 PID 文件两道闸**：只查 PID 文件会漏掉"别人手工起的实例"，
#    只查端口会漏掉"进程活着但没监听（启动中/挂了）"的情况。

# ---- 颜色（非 TTY 时自动关闭）----------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
  C_BLU=$'\033[34m'; C_DIM=$'\033[2m';  C_OFF=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_DIM=""; C_OFF=""
fi

log()  { printf '%s\n' "$*"; }
info() { printf '%s▸%s %s\n' "$C_BLU" "$C_OFF" "$*"; }
ok()   { printf '%s✓%s %s\n' "$C_GRN" "$C_OFF" "$*"; }
warn() { printf '%s!%s %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }
die()  { printf '%s✗%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

# ---- 路径与配置---------------------------------------------------------
# 以本文件所在位置推导项目根，而不是 $PWD：脚本必须能从任何目录调用。
RAGGI_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$RAGGI_LIB_DIR/.." && pwd)"

# 全部可用环境变量覆盖（默认值写在后面，注释即文档）
HOST="${RAGGI_HOST:-127.0.0.1}"          # 监听地址；0.0.0.0 = 对外开放
PORT="${RAGGI_PORT:-8156}"              # 端口。默认 8156 而不是 8000：
                                         # 8000 常被别的开发服务占着
DATA_DIR="${RAGGI_DATA:-$ROOT/data}"   # 数据目录
PROCESSES="${RAGGI_PROCESSES:-1}"       # API 进程数：1 = 单进程（默认）
PYTHON="${PYTHON:-python3}"             # 解释器
PID_FILE="${RAGGI_PID_FILE:-$ROOT/.raggi.pid}"
LOG_DIR="${RAGGI_LOG_DIR:-$ROOT/logs}"
LOG_FILE="$LOG_DIR/raggi.log"
LOG_MAX_BYTES=$((5 * 1024 * 1024))       # 超过 5MB 轮转一次
LOG_KEEP=3                                # 保留 raggi.log.1 … .3
HEALTH_TIMEOUT="${RAGGI_HEALTH_TIMEOUT:-60}"   # 启动健康检查秒数

# 判定"这是 Raggi 自己的进程"用的匹配串（**两种启动方式都要认**）：
#   python3 -m rag.server serve ...   ← 本仓库 scripts/ 与文档里的写法
#   .../bin/rag serve ...             ← 装了 CLI 之后最常见的写法
# 早先只匹配前者，于是 CLI 起的实例在 stop.sh --all 的清单里**根本不出现**
# （实测：主进程 rag serve 活着，端口被它的 worker 占着，脚本却说"没有实例"）。
RAGGI_MATCH_PATS=("rag.server serve" "rag serve")

# 自己的进程组绝不能当成目标：kill -- -PGID 会连调用脚本的 shell 一起杀掉
# （踩过：清理时把执行命令的 bash 也带走了，命令直接 terminated）。
own_pgid() { ps -o pgid= -p $$ 2>/dev/null | tr -d ' '; }


# ---- 进程与端口判定-----------------------------------------------------

# 端口占用者的 PID 列表（去掉子进程，只留监听进程）
port_pids() {
  local port="$1" pids=""
  if command -v ss >/dev/null 2>&1; then
    pids="$(ss -ltnp "sport = :$port" 2>/dev/null \
      | grep -o 'pid=[0-9]\+' | cut -d= -f2 | sort -u)"
  fi
  if [ -z "$pids" ] && command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti "tcp:$port" -sTCP:LISTEN 2>/dev/null | sort -u)"
  fi
  printf '%s' "$pids"
}

port_busy() { [ -n "$(port_pids "$1")" ]; }

# 实例命令行里声明的端口（幂等闸要核对"是不是用户要的那个端口"）
pid_port() {
  local cmd; cmd="$(pid_cmdline "${1:-}")"
  case "$cmd" in
    *"--port "*) printf '%s' "${cmd##*--port }" | cut -d' ' -f1 ;;
    *) printf '' ;;
  esac
}

pid_alive() { [ -n "${1:-}" ] && [ -d "/proc/$1" ]; }

# 进程的完整命令行（/proc 读不到时退化为 ps）
pid_cmdline() {
  local pid="$1"
  if [ -r "/proc/$pid/cmdline" ]; then
    tr '\0' ' ' < "/proc/$pid/cmdline"
  else
    ps -p "$pid" -o args= 2>/dev/null || true
  fi
}

# 这个 PID 是不是 Raggi 服务主进程？
is_raggi_pid() {
  local cmd p
  cmd="$(pid_cmdline "${1:-}")"
  # 排除"把脚本当参数"的 shell / python -c：它们的命令行里恰好含这些字符串
  case "$cmd" in
    *"/bin/bash -c"*|*"bash -c "*|*"python3 -c "*|*"python -c "*) return 1 ;;
  esac
  for p in "${RAGGI_MATCH_PATS[@]}"; do
    case "$cmd" in *"$p"*) return 0 ;; esac
  done
  return 1
}

# PID 文件里记录的实例是否还活着（活着且确实是 Raggi）
running_pid() {
  [ -f "$PID_FILE" ] || return 1
  local pid; pid="$(cat "$PID_FILE" 2>/dev/null | tr -d '[:space:]')"
  pid_alive "$pid" || return 1
  is_raggi_pid "$pid" || return 1
  printf '%s' "$pid"
}

# 列出本机所有 Raggi 服务主进程（每行一个 PID）
all_raggi_pids() {
  local pid
  for pid in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
    [ "$pid" = "$$" ] && continue
    is_raggi_pid "$pid" && printf '%s\n' "$pid"
  done
}

# 孤儿 worker：主进程已经不在，uvicorn/multiprocessing 的 spawn worker 还活着
# 并占着端口。判据是 **cwd + 进程组**，不是命令行——worker 的 cmdline 永远
# 是 `spawn_main`，看不出是谁家的；而 cwd 是从主进程继承的，指向本项目就够。
# （实测踩过：只 kill 主进程留下 4 个 worker 继续 listen 8000。）
is_own_orphan_worker() {
  local pid="$1" cmd cwd pgid alive_pgid
  cmd="$(pid_cmdline "$pid")"
  case "$cmd" in
    *multiprocessing*|*resource_tracker*) ;;
    *) return 1 ;;
  esac
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  [ "$cwd" = "$ROOT" ] || return 1
  # 进程组里还有没有活着的 Raggi 主进程？有的话它不是孤儿（stop 会整组带走）
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
  [ -n "$pgid" ] || return 1
  for alive_pgid in $(all_raggi_pids); do
    [ "$(ps -o pgid= -p "$alive_pgid" 2>/dev/null | tr -d ' ')" = "$pgid" ] && return 1
  done
  return 0
}

# 这个 PID 是不是"本项目的"Raggi 实例。
#
# 归属判据用 **cwd**（启动时所在目录）而不是命令行参数：CLI 方式
# （`rag serve`）通常**不带 --data**（读 config.toml 里的相对 data_dir），
# 参数里根本找不到数据目录；而 cwd 指向本项目根是鲜明特征。
is_own_raggi_pid() {
  is_raggi_pid "${1:-}" || return 1
  local cwd; cwd="$(readlink -f "/proc/$1/cwd" 2>/dev/null || true)"
  [ "$cwd" = "$ROOT" ] && return 0
  case "$(pid_cmdline "$1")" in *"$DATA_DIR"*) return 0 ;; esac
  return 1
}

# 该 PID 是不是本项目默认端口/数据目录的实例（即"我"）
is_own_instance() {
  local cmd; cmd="$(pid_cmdline "${1:-}")"
  case "$cmd" in
    *"--port $PORT"*|*"--port=$PORT"*) return 0 ;;
    *"$DATA_DIR"*) return 0 ;;
  esac
  return 1
}

# 优雅停止一个进程组，超时后 SIGKILL。
#
# 按**进程组**停而不是只 kill 主进程：uvicorn 的 worker（multiprocessing
# spawn）是主进程的兄弟子进程，只杀主进程它们会变成孤儿继续 listen 端口
# ——"服务停了但端口还占着"就是这么来的。
#
# PGID 从 ps 取而不是假设等于 PID：只有 setsid 启动的实例才保证 pgid == pid，
# 而用 `rag serve` 直接起的实例 pgid 可能是调用方的 shell 进程组——那种情况
# 下 `kill -- -PID` 要么失败，要么**连调用脚本的 shell 一起杀掉**（踩过）。
# 所以这里显式取 pgid，并与自身进程组比对后才动手。
stop_pid_group() {
  local pid="$1" waited=0 pgid mine
  pid_alive "$pid" || return 0
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
  mine="$(own_pgid)"
  if [ -n "$pgid" ] && [ "$pgid" != "$mine" ] && [ "$pgid" != "1" ]; then
    kill -TERM "-$pgid" 2>/dev/null || true
  else
    # 组不可用（就是自己这一组，或 pgid 未知）：退到单进程，且必须确保目标
    # 不是自己或自己的祖先
    if [ "$pid" = "$$" ]; then
      warn "拒绝停止 PID $pid（就是当前脚本）"
      return 1
    fi
    kill -TERM "$pid" 2>/dev/null || true
  fi
  while pid_alive "$pid" && [ "$waited" -lt 150 ]; do
    sleep 0.1; waited=$((waited + 1))
  done
  if pid_alive "$pid"; then
    warn "PID $pid 未在 15s 内退出，强制终止"
    if [ -n "$pgid" ] && [ "$pgid" != "$mine" ] && [ "$pgid" != "1" ]; then
      kill -KILL "-$pgid" 2>/dev/null || true
    else
      kill -KILL "$pid" 2>/dev/null || true
    fi
    sleep 0.3
  fi
}

# ---- 健康检查-----------------------------------------------------------
# 轮询 HTTP 直到 200。启动失败时把日志尾部打出来——否则用户只看到
# "启动超时"，还得自己去翻日志。
wait_http_ok() {
  local url="$1" timeout="${2:-60}" i=0 code=""
  while [ "$i" -lt $((timeout * 2)) ]; do
    code="$(curl -fsS -o /dev/null -w '%{http_code}' --max-time 2 "$url" 2>/dev/null || true)"
    [ "$code" = "200" ] && return 0
    sleep 0.5; i=$((i + 1))
  done
  return 1
}

dump_log_tail() {
  [ -f "$LOG_FILE" ] || return 0
  printf '%s\n' "${C_DIM}---- 日志末尾 30 行 ----${C_OFF}" >&2
  tail -n 30 "$LOG_FILE" >&2
}

# ---- 前端产物 -----------------------------------------------------------
# web/dist 不入库（见 .gitignore），所以"没构建过"是常态而不是异常。
# 比 mtime 而不是比"存在与否"：改了源码没重新构建，对着旧界面调 bug 是
# 本项目已经踩过的坑（前端守卫 test_build_output_when_present_is_fresh）。
dist_is_stale() {
  local entry="$ROOT/web/dist/index.html"
  [ -f "$entry" ] || return 0        # 从没构建过 → 需要构建
  local newer
  newer="$(find "$ROOT/web/src" "$ROOT/web/index.html" "$ROOT/web/vite.config.ts" \
    -type f -newer "$entry" 2>/dev/null | head -1)"
  [ -n "$newer" ]
}

# ---- 日志轮转 -----------------------------------------------------------
rotate_log_if_needed() {
  [ -f "$LOG_FILE" ] || return 0
  local size; size="$(wc -c < "$LOG_FILE" 2>/dev/null || echo 0)"
  [ "$size" -gt "$LOG_MAX_BYTES" ] || return 0
  local i
  for (( i = LOG_KEEP - 1; i >= 1; i-- )); do
    [ -f "$LOG_FILE.$i" ] && mv -f "$LOG_FILE.$i" "$LOG_FILE.$(( i + 1 ))"
  done
  mv -f "$LOG_FILE" "$LOG_FILE.1"
  info "日志超过 $(( LOG_MAX_BYTES / 1024 / 1024 ))MB，已轮转"
}
