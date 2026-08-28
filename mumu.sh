#!/usr/bin/env bash
# MuMuAINovel 服务管理脚本
# 用法: ./mumu.sh {start|stop|restart|status|logs} [backend|frontend|all]
# 示例: ./mumu.sh start          # 启动全部
#       ./mumu.sh restart backend # 仅重启后端
#       ./mumu.sh status          # 查看状态
#       ./mumu.sh logs backend   # 实时查看后端日志
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
VENV_PY="$BACKEND_DIR/.venv/bin/python"
DB_URL="sqlite+aiosqlite:///$BACKEND_DIR/data/mumuai_novel.db"
BACKEND_PORT=8008
FRONTEND_PORT=5173
BACKEND_LOG="/tmp/mumu-backend.log"
FRONTEND_LOG="/tmp/mumu-frontend.log"

# 颜色输出
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✔${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✘${NC} $1"; }

is_backend_running() { pgrep -f "uvicorn app.main" >/dev/null 2>&1; }
is_frontend_running() { pgrep -f "vite" >/dev/null 2>&1; }

start_backend() {
  if is_backend_running; then warn "后端已在运行 (PID $(pgrep -f 'uvicorn app.main' | head -1))"; return 0; fi
  cd "$PROJECT_ROOT"
  DATABASE_URL="$DB_URL" SESSION_COOKIE_SECURE=false PYTHONPATH=backend \
    nohup "$VENV_PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" \
    > "$BACKEND_LOG" 2>&1 &
  sleep 8
  if is_backend_running; then ok "后端已启动 (PID $(pgrep -f 'uvicorn app.main' | head -1), 端口 $BACKEND_PORT)"; else fail "后端启动失败，查看日志: $BACKEND_LOG"; return 1; fi
}

start_frontend() {
  if is_frontend_running; then warn "前端已在运行 (PID $(pgrep -f vite | head -1))"; return 0; fi
  cd "$PROJECT_ROOT/frontend"
  nohup npm run dev > "$FRONTEND_LOG" 2>&1 &
  sleep 6
  if is_frontend_running; then ok "前端已启动 (PID $(pgrep -f vite | head -1), 端口 $FRONTEND_PORT)"; else fail "前端启动失败，查看日志: $FRONTEND_LOG"; return 1; fi
}

stop_backend() {
  if ! is_backend_running; then warn "后端未在运行"; return 0; fi
  pkill -f "uvicorn app.main" 2>/dev/null || true
  sleep 2
  if is_backend_running; then fail "后端停止失败"; return 1; else ok "后端已停止"; fi
}

stop_frontend() {
  if ! is_frontend_running; then warn "前端未在运行"; return 0; fi
  pkill -f "vite" 2>/dev/null || true
  sleep 2
  if is_frontend_running; then fail "前端停止失败"; return 1; else ok "前端已停止"; fi
}

status_backend() {
  if is_backend_running; then
    local pid=$(pgrep -f 'uvicorn app.main' | head -1)
    local health=$(curl -s -m 3 http://localhost:$BACKEND_PORT/health 2>/dev/null || echo "无响应")
    ok "后端: 运行中 (PID $pid, 端口 $BACKEND_PORT) — /health: $health"
  else
    warn "后端: 未运行"
  fi
}

status_frontend() {
  if is_frontend_running; then
    local pid=$(pgrep -f vite | head -1)
    local code=$(curl -s -o /dev/null -w "%{http_code}" -m 3 http://localhost:$FRONTEND_PORT/ 2>/dev/null || echo "无响应")
    ok "前端: 运行中 (PID $pid, 端口 $FRONTEND_PORT) — HTTP $code"
  else
    warn "前端: 未运行"
  fi
}

case "${1:-}" in
  start)
    case "${2:-all}" in
      backend) start_backend ;;
      frontend) start_frontend ;;
      all) start_backend; start_frontend ;;
      *) fail "未知服务: $2 (可选: backend/frontend/all)"; exit 1 ;;
    esac
    ;;
  stop)
    case "${2:-all}" in
      backend) stop_backend ;;
      frontend) stop_frontend ;;
      all) stop_backend; stop_frontend ;;
      *) fail "未知服务: $2 (可选: backend/frontend/all)"; exit 1 ;;
    esac
    ;;
  restart)
    case "${2:-all}" in
      backend) stop_backend; start_backend ;;
      frontend) stop_frontend; start_frontend ;;
      all) stop_backend; start_backend; stop_frontend; start_frontend ;;
      *) fail "未知服务: $2 (可选: backend/frontend/all)"; exit 1 ;;
    esac
    ;;
  status)
    status_backend; status_frontend
    ;;
  logs)
    case "${2:-backend}" in
      backend) tail -f "$BACKEND_LOG" ;;
      frontend) tail -f "$FRONTEND_LOG" ;;
      *) fail "未知服务: $2 (可选: backend/frontend)"; exit 1 ;;
    esac
    ;;
  *)
    echo "用法: ./mumu.sh {start|stop|restart|status|logs} [backend|frontend|all]"
    echo "示例: ./mumu.sh start | ./mumu.sh restart backend | ./mumu.sh status | ./mumu.sh logs backend"
    exit 1
    ;;
esac
