#!/usr/bin/env bash
# AIFictionForge 服务管理脚本（端口归属制：服务身份 = 监听端口的进程 cwd）
# 用法: ./aistoryforge.sh {start|stop|restart|status|verify|logs} [backend|frontend|all] [--force]
# 示例: ./aistoryforge.sh start           # 启动本 checkout 的服务
#       ./aistoryforge.sh start --force   # 端口被其他 checkout 占用时，停掉它并强行启动
#       ./aistoryforge.sh verify          # 验收前置检查：两个端点跑的都是【本目录】的代码
#       ./aistoryforge.sh status          # 查看状态（含每个服务的代码身份）
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
VENV_PY="$BACKEND_DIR/.venv/bin/python"
DB_URL="sqlite+aiosqlite:///$BACKEND_DIR/data/mumuai_novel.db"
BACKEND_PORT=8008
FRONTEND_PORT=5173
BACKEND_LOG="/tmp/aistoryforge-backend.log"
FRONTEND_LOG="/tmp/aistoryforge-frontend.log"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✔${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✘${NC} $1"; }
info() { echo -e "${CYAN}ℹ${NC} $1"; }

# ---------- 服务身份（核心：端口 → 进程 cwd → git 分支） ----------
port_owner_pid() { lsof -ti ":$1" -sTCP:LISTEN 2>/dev/null | head -1 || true; }
pid_root()       { local r=""; for _ in 1 2 3 4 5; do r="$(lsof -p "$1" 2>/dev/null | awk '/ cwd /{print $NF}' | head -1)"; [ -n "$r" ] && break; sleep 0.4; done; echo "$r"; }
branch_of()      { git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?"; }
sha_of()         { git -C "$1" rev-parse --short HEAD 2>/dev/null || echo "?"; }
identity_of()    { local r; r="$(pid_root "$1")"; if [ -n "$r" ]; then echo "$r ($(branch_of "$r")@$(sha_of "$r"))"; else echo "?"; fi; }
is_self_owned()  { local pid; pid="$(port_owner_pid "$1")"; [ -n "$pid" ] && { local r; r="$(pid_root "$pid")"; [[ "$r" == "$PROJECT_ROOT"* ]]; }; }
is_service_running() { [ -n "$(port_owner_pid "$1")" ]; }

# ---------- 兼容旧调用 ----------
is_backend_running()  { is_service_running "$BACKEND_PORT"; }
is_frontend_running() { is_service_running "$FRONTEND_PORT"; }

# ---------- start ----------
start_backend() {
  if is_service_running "$BACKEND_PORT"; then
    if is_self_owned "$BACKEND_PORT"; then
      warn "后端已在运行 (PID $(port_owner_pid "$BACKEND_PORT"))，代码即本 checkout"
    else
      if [ "${FORCE:-0}" != "1" ]; then
        fail "后端端口 $BACKEND_PORT 被其他 checkout 占用: $(identity_of "$BACKEND_PORT")"
        fail "请先到该目录执行 ./aistoryforge.sh stop，或使用: ./aistoryforge.sh start backend --force"
        return 1
      fi
      warn "--force: 停掉其他 checkout 的后端 ($(identity_of "$BACKEND_PORT"))"
      stop_backend_quiet
    fi
  fi
  cd "$PROJECT_ROOT"
  DATABASE_URL="$DB_URL" SESSION_COOKIE_SECURE=false PYTHONPATH=backend \
    nohup "$VENV_PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" --reload \
    > "$BACKEND_LOG" 2>&1 &
  sleep 8
  if is_service_running "$BACKEND_PORT"; then ok "后端已启动 (PID $(port_owner_pid "$BACKEND_PORT"), 端口 $BACKEND_PORT) — $(identity_of "$BACKEND_PORT")"; else fail "后端启动失败，查看日志: $BACKEND_LOG"; return 1; fi
}

start_frontend() {
  if is_service_running "$FRONTEND_PORT"; then
    if is_self_owned "$FRONTEND_PORT"; then
      warn "前端已在运行 (PID $(port_owner_pid "$FRONTEND_PORT"))，代码即本 checkout"
    else
      if [ "${FORCE:-0}" != "1" ]; then
        fail "前端端口 $FRONTEND_PORT 被其他 checkout 占用: $(identity_of "$FRONTEND_PORT")"
        fail "请先到该目录执行 ./aistoryforge.sh stop，或使用: ./aistoryforge.sh start frontend --force"
        return 1
      fi
      warn "--force: 停掉其他 checkout 的前端 ($(identity_of "$FRONTEND_PORT"))"
      stop_frontend_quiet
    fi
  fi
  cd "$PROJECT_ROOT/frontend"
  nohup npm run dev > "$FRONTEND_LOG" 2>&1 &
  sleep 6
  if is_service_running "$FRONTEND_PORT"; then ok "前端已启动 (PID $(port_owner_pid "$FRONTEND_PORT"), 端口 $FRONTEND_PORT) — $(identity_of "$FRONTEND_PORT")"; else fail "前端启动失败，查看日志: $FRONTEND_LOG"; return 1; fi
}

# ---------- stop（只停本端口归属的进程，并报告身份） ----------
stop_backend_quiet() {
  local pid; pid="$(port_owner_pid "$BACKEND_PORT")"
  [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  sleep 2
}
stop_frontend_quiet() {
  local pid; pid="$(port_owner_pid "$FRONTEND_PORT")"
  [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  sleep 2
}
stop_backend() {
  if ! is_service_running "$BACKEND_PORT"; then warn "后端: 未运行"; return 0; fi
  info "停止后端 ($(identity_of "$BACKEND_PORT"))"
  stop_backend_quiet
  if is_service_running "$BACKEND_PORT"; then fail "后端停止失败"; return 1; else ok "后端已停止"; fi
}
stop_frontend() {
  if ! is_service_running "$FRONTEND_PORT"; then warn "前端: 未运行"; return 0; fi
  info "停止前端 ($(identity_of "$FRONTEND_PORT"))"
  stop_frontend_quiet
  if is_service_running "$FRONTEND_PORT"; then fail "前端停止失败"; return 1; else ok "前端已停止"; fi
}

# ---------- status ----------
status_backend() {
  if is_service_running "$BACKEND_PORT"; then
    local pid health mark
    pid="$(port_owner_pid "$BACKEND_PORT")"
    health=$(curl -s -m 3 http://localhost:$BACKEND_PORT/health 2>/dev/null || echo "无响应")
    if is_self_owned "$BACKEND_PORT"; then mark="${GREEN}[本 checkout]${NC}"; else mark="${RED}[其他 checkout！]${NC}"; fi
    ok "后端: 运行中 (PID $pid, 端口 $BACKEND_PORT) $mark 代码: $(identity_of "$BACKEND_PORT")"
    info "     /health: $health"
  else
    warn "后端: 未运行"
  fi
}
status_frontend() {
  if is_service_running "$FRONTEND_PORT"; then
    local pid code mark
    pid="$(port_owner_pid "$FRONTEND_PORT")"
    code=$(curl -s -o /dev/null -w "%{http_code}" -m 3 http://localhost:$FRONTEND_PORT/ 2>/dev/null || echo "无响应")
    if is_self_owned "$FRONTEND_PORT"; then mark="${GREEN}[本 checkout]${NC}"; else mark="${RED}[其他 checkout！]${NC}"; fi
    ok "前端: 运行中 (PID $pid, 端口 $FRONTEND_PORT) $mark 代码: $(identity_of "$FRONTEND_PORT")"
    info "     HTTP $code"
  else
    warn "前端: 未运行"
  fi
}

# ---------- verify（验收前置：两个端点必须跑的是本 checkout 的代码） ----------
verify_services() {
  local ok_all=1
  local expected_branch; expected_branch="$(branch_of "$PROJECT_ROOT")"
  echo "本 checkout: $PROJECT_ROOT ($expected_branch@$(sha_of "$PROJECT_ROOT"))"
  for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
    local name; name=$([ "$port" = "$BACKEND_PORT" ] && echo 后端 || echo 前端)
    if ! is_service_running "$port"; then fail "$name: 未运行 (端口 $port)"; ok_all=0; continue; fi
    if ! is_self_owned "$port"; then
      fail "$name: 端口 $port 跑的是其他 checkout: $(identity_of "$port") — 禁止测试"
      ok_all=0; continue
    fi
    ok "$name: 本 checkout ($(identity_of "$port"))"
  done
  if [ "$ok_all" = "1" ]; then
    local health_branch
    health_branch=$(curl -s -m 3 http://localhost:$BACKEND_PORT/health 2>/dev/null | sed -n 's/.*"branch":"\([^"]*\)".*/\1/p' || echo "?")
    if [ "$health_branch" = "$expected_branch" ]; then
      ok "后端 /health 分支核对一致: $health_branch"
    else
      fail "后端 /health 分支 ($health_branch) 与本 checkout ($expected_branch) 不一致"
      ok_all=0
    fi
  fi
  if [ "$ok_all" = "1" ]; then ok "verify 通过：可以开始测试/验收"; else fail "verify 未通过：先解决上方身份问题再测试"; return 1; fi
}

# ---------- 参数解析（--force 可出现在任意位置） ----------
ACTION="${1:-}"; SERVICE="${2:-all}"
FORCE=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    *) ARGS+=("$a") ;;
  esac
done
ACTION="${ARGS[0]:-}"; SERVICE="${ARGS[1]:-all}"

case "$ACTION" in
  start)
    case "$SERVICE" in
      backend) start_backend ;;
      frontend) start_frontend ;;
      all) start_backend; start_frontend ;;
      *) fail "未知服务: $SERVICE (可选: backend/frontend/all)"; exit 1 ;;
    esac
    ;;
  stop)
    case "$SERVICE" in
      backend) stop_backend ;;
      frontend) stop_frontend ;;
      all) stop_backend; stop_frontend ;;
      *) fail "未知服务: $SERVICE (可选: backend/frontend/all)"; exit 1 ;;
    esac
    ;;
  restart)
    case "$SERVICE" in
      backend) stop_backend; start_backend ;;
      frontend) stop_frontend; start_frontend ;;
      all) stop_backend; start_backend; stop_frontend; start_frontend ;;
      *) fail "未知服务: $SERVICE (可选: backend/frontend/all)"; exit 1 ;;
    esac
    ;;
  status)
    status_backend; status_frontend
    ;;
  verify)
    verify_services
    ;;
  logs)
    case "$SERVICE" in
      backend) tail -f "$BACKEND_LOG" ;;
      frontend) tail -f "$FRONTEND_LOG" ;;
      *) fail "未知服务: $SERVICE (可选: backend/frontend)"; exit 1 ;;
    esac
    ;;
  *)
    echo "用法: ./aistoryforge.sh {start|stop|restart|status|verify|logs} [backend|frontend|all] [--force]"
    echo "示例: ./aistoryforge.sh start | ./aistoryforge.sh verify | ./aistoryforge.sh start backend --force | ./aistoryforge.sh status"
    exit 1
    ;;
esac
