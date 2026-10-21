#!/usr/bin/env bash
# Raggi 停止脚本
#
#   ./stop.sh          停止本项目实例（PID 文件 + 端口双重定位）
#   ./stop.sh --all    停止本机**所有** Raggi 实例（含手工起的、数据目录不同的）
#   ./stop.sh --all --force  不询问直接停（脚本化用）
#
# 停止方式：SIGTERM 整组 → 最多等 15s → SIGKILL。
# 之所以按**进程组**停：服务以 setsid 启动，是新会话的组长，只 kill 主进程
# 会留下 uvicorn worker 变成孤儿继续占着端口（这正是"关了还在跑"的由来）。

set -euo pipefail
# shellcheck source=scripts/_common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/_common.sh"

STOP_ALL=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --all) STOP_ALL=1 ;;
    --force) FORCE=1 ;;
    -h|--help)
      sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) die "未知参数：$arg（用 --help 看用法）" ;;
  esac
done

cd "$ROOT"

stopped=0

stop_one() {
  local pid="$1" why="$2"
  if ! pid_alive "$pid"; then
    info "PID $pid 已不在（$why）"
    return 0
  fi
  info "停止 $why：PID $pid"
  stop_pid_group "$pid"
  stopped=$(( stopped + 1 ))
}

# ---- 模式一：--all，清本机所有 Raggi 实例 -------------------------------
if [ "$STOP_ALL" = "1" ]; then
  targets="$(all_raggi_pids || true)"
  if [ -z "$targets" ]; then
    ok "没有正在运行的 Raggi 实例"
    rm -f "$PID_FILE"
    exit 0
  fi
  # worker 的 cmdline 是 multiprocessing.spawn，不含 "rag.server serve"，
  # 所以这里只列主进程；worker 会随主进程被整组带走。
  warn "将停止 $(echo "$targets" | wc -l | tr -d ' ') 个 Raggi 实例："
  for p in $targets; do
    printf '    PID %-8s %s\n' "$p" "$(pid_cmdline "$p" | cut -c1-100)" >&2
  done
  if [ "$FORCE" != "1" ] && [ -t 0 ]; then
    read -r -p "确认停止？[y/N] " ans
    case "$ans" in [yY]*) ;; *) die "已取消" ;; esac
  fi
  for p in $targets; do stop_one "$p" "实例"; done
  # 主进程停掉之后再扫一遍孤儿 worker：它们的 pgid 就是刚被杀掉的那些主
  # 进程，不清的话端口会一直被占着（这是"stop 完了 8000 还在 LISTEN"的成因）
  for p in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
    if is_own_orphan_worker "$p"; then
      stop_one "$p" "孤儿 worker（主进程已停）"
    fi
  done
  rm -f "$PID_FILE"
  ok "已停止 $stopped 个进程"
  exit 0
fi

# ---- 模式二：只停本项目（PID 文件 → 端口 → 数据目录 三重定位）----------
if pid="$(running_pid)"; then
  stop_one "$pid" "本项目实例"
else
  [ -f "$PID_FILE" ] && { info "清理陈旧 PID 文件"; rm -f "$PID_FILE"; }
  # PID 文件不可靠（手工起过、文件被删）时退到端口判定
  if pids="$(port_pids "$PORT")" && [ -n "$pids" ]; then
    for p in $pids; do
      if is_raggi_pid "$p"; then
        stop_one "$p" "占用端口 $PORT 的 Raggi 进程"
      fi
    done
  fi
fi

# 兜底：本项目目录下的 Raggi 实例（启动时换过端口、CLI 方式没带 --data 的
# 情况都靠这条捞回来——判据是 cwd 指向项目根，见 is_own_raggi_pid）
for p in $(all_raggi_pids); do
  if pid_alive "$p" && is_own_raggi_pid "$p"; then
    stop_one "$p" "本项目实例（cwd 匹配）"
  fi
done

# 兜底二：孤儿 worker（主进程已死，spawn worker 还占着端口）。
# 判据是 cwd + 进程组，见 _common.sh 的说明。
for p in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
  if is_own_orphan_worker "$p"; then
    stop_one "$p" "孤儿 worker（主进程已不在）"
  fi
done

if [ "$stopped" = "0" ]; then
  ok "没有正在运行的 Raggi 实例"
else
  ok "已停止 $stopped 个实例"
fi

# 端口释放校验：给出明确结论，而不是让用户自己猜
for _ in 1 2 3 4 5 6 7 8 9 10; do
  port_busy "$PORT" || break
  sleep 0.3
done
if port_busy "$PORT"; then
  warn "端口 $PORT 仍被占用："
  for p in $(port_pids "$PORT"); do
    printf '    PID %-8s %s\n' "$p" "$(pid_cmdline "$p" | cut -c1-100)" >&2
  done
  exit 1
fi
[ "$stopped" != "0" ] && ok "端口 $PORT 已释放"
exit 0
