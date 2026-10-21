#!/usr/bin/env bash
# Raggi 重启脚本
#
#   ./restart.sh                原地重启（停 → 起，配置不变）
#   ./restart.sh --rebuild      顺带重新构建前端
#   RAGGI_PORT=9000 ./restart.sh 按新端口重启（旧实例会被收掉）
#
# 与 `stop.sh && start.sh` 的差别只有一处，但很关键：**重启时先按 --all 收掉
# 所有同项目实例**。改了端口之后，旧实例在新端口的幂等闸上看不见——只停
# PID 文件里那个的话，改一次配置就会多留一个实例占着旧端口，那正是本项目
# 之前"两个端口各有一个服务"的由来。

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
. "$HERE/scripts/_common.sh"

cd "$ROOT"
info "重启 Raggi（端口 $PORT · 数据 $DATA_DIR · 进程数 $PROCESSES）"

"$HERE/stop.sh" --all --force
"$HERE/start.sh" "$@"
