#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backend_dir="$project_dir/backend"
frontend_dir="$project_dir/frontend"

if [[ ! -f "$backend_dir/.env" ]]; then
  echo "缺少 backend/.env；请先从 backend/.env.example 复制并填写。" >&2
  exit 1
fi
if [[ ! -x "$backend_dir/.venv/bin/python" ]]; then
  echo "缺少后端依赖；请先在 backend 目录运行 uv sync。" >&2
  exit 1
fi
if [[ ! -d "$frontend_dir/node_modules" ]]; then
  echo "缺少前端依赖；请先在 frontend 目录运行 npm ci。" >&2
  exit 1
fi

backend_pid=""
frontend_pid=""

cleanup() {
  [[ -n "$backend_pid" ]] && kill "$backend_pid" 2>/dev/null || true
  [[ -n "$frontend_pid" ]] && kill "$frontend_pid" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "启动后端：http://localhost:8000"
(cd "$backend_dir" && exec .venv/bin/python src/main.py) &
backend_pid=$!

echo "启动前端：http://localhost:5174"
(cd "$frontend_dir" && exec npm run dev) &
frontend_pid=$!

wait "$backend_pid"
