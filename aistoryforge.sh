#!/usr/bin/env bash
# AIFictionForge 服务管理脚本（端口归属制：服务身份 = 监听端口的进程 cwd）
# 用法: ./aistoryforge.sh {start|stop|restart|status|verify|logs} [backend|frontend|all] [--force]
# 退出码: 0=成功（stop 时目标本来就没在跑也返回 0）；非 0=失败（停止超时/启动未就绪/自检未过）
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
# 有界超时（秒）：stop 先 TERM 等 8s，再 KILL 兜底等 5s；start 就绪等待上限 60s
STOP_GRACE_TIMEOUT=8
STOP_KILL_TIMEOUT=5
START_TIMEOUT=60

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✔${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✘${NC} $1"; }
info() { echo -e "${CYAN}ℹ${NC} $1"; }

# ---------- 服务身份（核心：端口 → 进程 cwd → git 分支） ----------
port_listener_pids() { lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true; }
port_owner_pid() { port_listener_pids "$1" | head -1 || true; }
pid_root()       { local r=""; for _ in 1 2 3 4 5; do r="$(lsof -p "$1" 2>/dev/null | awk '/ cwd /{print $NF}' | head -1)"; [ -n "$r" ] && break; sleep 0.4; done; echo "$r"; }
branch_of()      { git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?"; }
sha_of()         { git -C "$1" rev-parse --short HEAD 2>/dev/null || echo "?"; }
# identity_of 入参是端口（旧实现把端口当 PID 传给 pid_root，导致恒为 "?"）
identity_of()    { local pid r; pid="$(port_owner_pid "$1")"; [ -n "$pid" ] || { echo "?"; return 0; }; r="$(pid_root "$pid")"; if [ -n "$r" ]; then echo "$r ($(branch_of "$r")@$(sha_of "$r"))"; else echo "?"; fi; }
is_self_owned()  { local pid; pid="$(port_owner_pid "$1")"; [ -n "$pid" ] && { local r; r="$(pid_root "$pid")"; [[ "$r" == "$PROJECT_ROOT"* ]]; }; }
is_service_running() { [ -n "$(port_owner_pid "$1")" ]; }

# ---------- 兼容旧调用 ----------
is_backend_running()  { is_service_running "$BACKEND_PORT"; }
is_frontend_running() { is_service_running "$FRONTEND_PORT"; }

# ---------- stop 核心（确定性停止：监听者 + 服务进程树 → TERM → 有界等待 → KILL → 直至端口释放） ----------
descendants_of() {
  local frontier="$1" out="" kids p c
  while [ -n "$frontier" ]; do
    kids=""
    for p in $frontier; do
      c="$(pgrep -P "$p" 2>/dev/null | tr '\n' ' ' || true)"
      [ -n "$c" ] && kids="$kids $c"
    done
    frontier="$kids"
    [ -n "$frontier" ] && out="$out $frontier"
  done
  printf '%s' "$out"
}

# 监听者向上回溯启动器父链（只认 uvicorn/npm/vite/node；遇 shell/launchd 立即停，避免误杀无关进程）
launcher_chain_of() {
  local p="$1" ppid cmd out=""
  while [ -n "$p" ] && [ "$p" != "1" ]; do
    out="$out $p"
    ppid="$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ' || true)"
    [ -n "$ppid" ] || break
    [ "$ppid" = "1" ] && break
    cmd="$(ps -o command= -p "$ppid" 2>/dev/null || true)"
    case "$cmd" in
      *uvicorn*|*vite*|*"npm run dev"*|*node*) p="$ppid" ;;
      *) break ;;
    esac
  done
  printf '%s' "$out"
}

# 端口上服务的完整进程集合：监听者 + 启动器父链 + 全部子孙（含 --reload 的 master/worker）
service_pids() {
  local listener out=""
  for listener in $(port_listener_pids "$1"); do
    out="$out $(launcher_chain_of "$listener") $(descendants_of "$listener")"
  done
  printf '%s\n' $out | grep -E '^[0-9]+$' | sort -n -u | tr '\n' ' ' || true
}

port_free() { [ -z "$(port_listener_pids "$1")" ]; }

pid_alive() {
  local st; st="$(ps -o stat= -p "$1" 2>/dev/null | tr -d ' ' || true)"
  [ -n "$st" ] || return 1
  case "$st" in Z*) return 1 ;; esac
  return 0
}

any_pid_alive() {
  local pid
  for pid in $1; do pid_alive "$pid" && return 0; done
  return 1
}

# 失败统一落点：端口 + 监听 PID + 日志路径 + 精确重试命令
actionable_fail() {
  local msg="$1" port="$2" svc="$3" logf holder
  if [ "$svc" = "backend" ]; then logf="$BACKEND_LOG"; else logf="$FRONTEND_LOG"; fi
  holder="$(port_listener_pids "$port" | tr '\n' ',' | sed 's/,$//')"
  fail "$msg (端口 $port; 监听 PID [${holder:-无}]; 日志: $logf)"
  fail "  重试: ./aistoryforge.sh restart $svc"
}

# SIGTERM → 有界等待 → SIGKILL 兜底 → 循环至端口释放；成功时打印被停止的 PID + 端口
stop_port_deterministic() {
  local port="$1" svc="$2" quiet="${3:-0}"
  local name pids pid survivors deadline
  if [ "$svc" = "backend" ]; then name="后端"; else name="前端"; fi
  pids="$(service_pids "$port")"
  if [ -z "${pids// /}" ]; then
    [ "$quiet" = "1" ] || warn "$name: 未运行"
    return 0
  fi
  if [ "$quiet" != "1" ]; then
    info "停止$name (端口 $port, PID $(echo $pids | sed 's/ /,/g')) — $(identity_of "$port")"
    info "  发送 SIGTERM -> PID $(echo $pids | sed 's/ /,/g')，等待退出 (≤${STOP_GRACE_TIMEOUT}s)"
  fi
  for pid in $pids; do kill -TERM "$pid" 2>/dev/null || true; done
  deadline=$((SECONDS + STOP_GRACE_TIMEOUT))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if port_free "$port" && ! any_pid_alive "$pids"; then
      [ "$quiet" = "1" ] || ok "${name}已停止 (端口 $port 已释放, PID $(echo $pids | sed 's/ /,/g'))"
      return 0
    fi
    sleep 0.25
  done
  survivors=""
  for pid in $pids; do pid_alive "$pid" && survivors="$survivors $pid"; done
  [ -n "${survivors// /}" ] || survivors="$pids"
  [ "$quiet" = "1" ] || warn "$name: TERM 等待超时，SIGKILL 兜底 -> PID $(echo $survivors | sed 's/ /,/g')"
  deadline=$((SECONDS + STOP_KILL_TIMEOUT))
  while [ "$SECONDS" -lt "$deadline" ]; do
    for pid in $(service_pids "$port"); do kill -KILL "$pid" 2>/dev/null || true; done
    for pid in $survivors; do kill -KILL "$pid" 2>/dev/null || true; done
    if port_free "$port" && ! any_pid_alive "$pids"; then
      [ "$quiet" = "1" ] || ok "${name}已停止 (SIGKILL 兜底, 端口 $port 已释放, PID $(echo $pids | sed 's/ /,/g'))"
      return 0
    fi
    sleep 0.25
  done
  actionable_fail "${name}停止失败：端口仍被占用" "$port" "$svc"
  return 1
}

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
  local spawn_pid=$!
  if wait_backend_ready "$spawn_pid"; then
    ok "后端已启动 (PID $(port_owner_pid "$BACKEND_PORT"), 端口 $BACKEND_PORT) — $(identity_of "$BACKEND_PORT")"
  else
    start_failed_message "后端" "$BACKEND_PORT" "$BACKEND_LOG" backend
    return 1
  fi
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
  local spawn_pid=$!
  if wait_frontend_ready "$spawn_pid"; then
    ok "前端已启动 (PID $(port_owner_pid "$FRONTEND_PORT"), 端口 $FRONTEND_PORT) — $(identity_of "$FRONTEND_PORT")"
  else
    start_failed_message "前端" "$FRONTEND_PORT" "$FRONTEND_LOG" frontend
    return 1
  fi
}

# ---------- stop（只停本端口上的进程树，并报告身份/被停止的 PID） ----------
stop_backend_quiet()  { stop_port_deterministic "$BACKEND_PORT" backend 1; }
stop_frontend_quiet() { stop_port_deterministic "$FRONTEND_PORT" frontend 1; }
stop_backend() {
  if ! is_service_running "$BACKEND_PORT"; then warn "后端: 未运行"; return 0; fi
  stop_port_deterministic "$BACKEND_PORT" backend 0
}
stop_frontend() {
  if ! is_service_running "$FRONTEND_PORT"; then warn "前端: 未运行"; return 0; fi
  stop_port_deterministic "$FRONTEND_PORT" frontend 0
}

# ---------- 启动就绪等待（有界轮询：端口 + /health 分支 / HTTP 200） ----------
wait_backend_ready() {
  local spawn_pid="${1:-}" deadline=$((SECONDS + START_TIMEOUT)) body br expected
  expected="$(branch_of "$PROJECT_ROOT")"
  while [ "$SECONDS" -lt "$deadline" ]; do
    if [ -n "$spawn_pid" ] && ! pid_alive "$spawn_pid" && port_free "$BACKEND_PORT"; then
      return 1
    fi
    if [ -n "$(port_owner_pid "$BACKEND_PORT")" ]; then
      body="$(curl -s -m 2 "http://localhost:$BACKEND_PORT/health" 2>/dev/null || true)"
      br="$(printf '%s' "$body" | sed -n 's/.*"branch":"\([^"]*\)".*/\1/p' || true)"
      if [ -n "$br" ] && [ "$br" = "$expected" ]; then return 0; fi
    fi
    sleep 0.5
  done
  return 1
}

wait_frontend_ready() {
  local spawn_pid="${1:-}" deadline=$((SECONDS + START_TIMEOUT)) code
  while [ "$SECONDS" -lt "$deadline" ]; do
    if [ -n "$spawn_pid" ] && ! pid_alive "$spawn_pid" && port_free "$FRONTEND_PORT"; then
      return 1
    fi
    if [ -n "$(port_owner_pid "$FRONTEND_PORT")" ]; then
      code="$(curl -s -o /dev/null -w '%{http_code}' -m 2 "http://localhost:$FRONTEND_PORT/" 2>/dev/null || echo 000)"
      [ "$code" = "200" ] && return 0
    fi
    sleep 0.5
  done
  return 1
}

# 启动失败落点：真实原因（日志尾部）+ PID/端口 + 重试命令
start_failed_message() {
  local name="$1" port="$2" logf="$3" svc="$4" holder
  holder="$(port_listener_pids "$port" | tr '\n' ',' | sed 's/,$//')"
  fail "${name}启动失败：${START_TIMEOUT}s 内未就绪 (端口 $port; 监听 PID [${holder:-无}])"
  fail "  日志尾部 ($logf):"
  tail -n 15 "$logf" 2>/dev/null | sed 's/^/    /' || true
  fail "  重试: ./aistoryforge.sh restart $svc"
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

# ---------- restart 接管护栏 + 自检（复用 verify 的身份/分支判据；verify 本体语义不变） ----------
guard_takeover() {
  local port="$1" name="$2" svc="$3"
  is_service_running "$port" || return 0
  is_self_owned "$port" && return 0
  if [ "${FORCE:-0}" != "1" ]; then
    fail "${name}端口 $port 被其他 checkout 占用: $(identity_of "$port")"
    fail "请先到该目录执行 ./aistoryforge.sh stop，或使用: ./aistoryforge.sh restart $svc --force"
    return 1
  fi
  warn "--force: 停掉其他 checkout 的$name ($(identity_of "$port"))"
}

verify_restart_port() {
  local port="$1" name svc
  if [ "$port" = "$BACKEND_PORT" ]; then name="后端"; svc="backend"; else name="前端"; svc="frontend"; fi
  if ! is_service_running "$port"; then
    actionable_fail "$name restart 自检失败：端口未监听" "$port" "$svc"; return 1
  fi
  if ! is_self_owned "$port"; then
    actionable_fail "$name restart 自检失败：端口跑的是其他 checkout ($(identity_of "$port"))" "$port" "$svc"; return 1
  fi
  if [ "$port" = "$BACKEND_PORT" ]; then
    local hb exp; exp="$(branch_of "$PROJECT_ROOT")"
    hb="$(curl -s -m 3 "http://localhost:$port/health" 2>/dev/null | sed -n 's/.*"branch":"\([^"]*\)".*/\1/p' || true)"
    if [ "$hb" != "$exp" ]; then
      actionable_fail "$name restart 自检失败：/health 分支 ($hb) 与本 checkout ($exp) 不一致" "$port" "$svc"; return 1
    fi
  else
    local code; code="$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://localhost:$port/" 2>/dev/null || echo 000)"
    if [ "$code" != "200" ]; then
      actionable_fail "$name restart 自检失败：端口 $port HTTP $code" "$port" "$svc"; return 1
    fi
  fi
  ok "$name: restart 自检通过 ($(identity_of "$port"))"
}

restart_selfcheck() {
  if verify_services; then
    ok "restart 自检通过：两个端点均已就绪且服务本 checkout"
  else
    fail "restart 自检未通过：见上方条目; 日志: $BACKEND_LOG / $FRONTEND_LOG"
    fail "  重试: ./aistoryforge.sh restart"
    return 1
  fi
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
      backend) guard_takeover "$BACKEND_PORT" "后端" backend; stop_backend; start_backend; verify_restart_port "$BACKEND_PORT" ;;
      frontend) guard_takeover "$FRONTEND_PORT" "前端" frontend; stop_frontend; start_frontend; verify_restart_port "$FRONTEND_PORT" ;;
      all)
        guard_takeover "$BACKEND_PORT" "后端" backend
        guard_takeover "$FRONTEND_PORT" "前端" frontend
        stop_backend; start_backend; stop_frontend; start_frontend; restart_selfcheck
        ;;
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
