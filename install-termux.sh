#!/bin/bash
# =============================================================================
# MuMuAINovel Termux 一键安装脚本
# =============================================================================
# 

set -e

# ── 路径配置 ──────────────────────────────────────────────────────────────────
INSTALL_DIR="$HOME/MuMuAINovel"                    # 项目安装目录
DATA_DIR="$HOME/mumuainovel/data"                   # 数据库目录
LOG_DIR="$HOME/mumuainovel/logs"                    # 日志目录
REPO="https://ghfast.top/https://github.com/xiamuceer-j/MuMuAINovel.git"  # GitHub 镜像

# ── 输出函数 ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
err()   { echo -e "${RED}[✗]${NC} $1"; }
step()  { echo -e "\n${CYAN}[$1/$2]${NC} $3"; }

# ── 转圈动画函数 ──────────────────────────────────────────────────────────────
# 用法: SPIN <后台进程PID> <提示文字> <日志文件路径>
# 原理: 检测进程是否存活，存活就显示旋转动画，结束后显示 ✅ 或 ❌
SPIN() {
    local PID=$1 MSG=$2 LOGF=$3
    echo -n "  $MSG"
    while kill -0 $PID 2>/dev/null; do
        for s in ⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏; do
            echo -ne "\r  $s $MSG"
            sleep 0.3
            kill -0 $PID 2>/dev/null || break 2
        done
    done
    wait $PID
    local RET=$?
    if [ $RET -eq 0 ]; then
        echo -e "\r  ✅ $MSG 完成          "
    else
        echo -e "\r  ❌ $MSG 失败          "
        if [ -n "$LOGF" ] && [ -f "$LOGF" ]; then
            echo -e "${RED}--- 错误日志 (最后20行) ---${NC}"
            tail -20 "$LOGF"
            echo -e "${RED}--- 日志结束 ---${NC}"
        fi
        exit 1
    fi
}

# ── pip 镜像源（国内加速）──────────────────────────────────────────────────────
MIRROR="-i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com"

# =============================================================================
echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   📚 MuMuAINovel Termux 一键安装         ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

TOTAL=9

# =============================================================================
# 步骤 1: 检查 Termux 环境
# =============================================================================
step 1 $TOTAL "检查环境"
if [ ! -d "/data/data/com.termux" ]; then
    err "未检测到 Termux 环境，请在 Termux 中运行"
    exit 1
fi
info "Termux 环境检测通过"

# =============================================================================
# 步骤 2: 安装系统依赖 (python/nodejs/git)
# 说明: pkg install 自动跳过已安装的包，重复运行不会重新下载
# =============================================================================
step 2 $TOTAL "安装系统依赖"
LOG="$TMPDIR/pkg-install.log"
pkg install -y python nodejs git > "$LOG" 2>&1 &
SPIN $! "安装中" "$LOG"

# =============================================================================
# 步骤 3: 拉取/更新项目源码
# 说明: 已有 .git 目录 → git pull 增量更新 (保留 venv/.env/data)
#       没有 → git clone 全量下载
# =============================================================================
step 3 $TOTAL "拉取/更新项目源码"
if [ -d "$INSTALL_DIR/.git" ]; then
    LOG="$TMPDIR/git-pull.log"
    (
        cd "$INSTALL_DIR"
        git fetch origin
        git reset --hard origin/main 2>/dev/null || git reset --hard origin/master
    ) > "$LOG" 2>&1 &
    SPIN $! "拉取中" "$LOG"
else
    # 目录存在但不是 git 仓库，清理后重新克隆
    if [ -d "$INSTALL_DIR" ]; then
        rm -rf "$INSTALL_DIR"
    fi
    LOG="$TMPDIR/git-clone.log"
    git clone "$REPO" "$INSTALL_DIR" > "$LOG" 2>&1 &
    SPIN $! "克隆中" "$LOG"
fi

BACKEND="$INSTALL_DIR/backend"
FRONTEND="$INSTALL_DIR/frontend"

# =============================================================================
# 步骤 4: 应用 Termux 兼容补丁
# 说明: Termux 不支持 chromadb/sentence-transformers，需要修补代码避免崩溃
#   4a. memory_service.py — import 改为 try/except，缺失时优雅降级
#   4b. API 文件 — memory_service 导入改为 try/except
#   4c. .env — 已有则跳过，新建则写入默认配置
# =============================================================================
step 4 $TOTAL "应用 Termux 补丁"
LOG="$TMPDIR/patch.log"
(
# ── 4a. 修补 memory_service.py ──────────────────────────────────────────────
python3 << 'PYEOF'
import os
f = os.path.expanduser("~/MuMuAINovel/backend/app/services/memory_service.py")
with open(f) as fh:
    c = fh.read()

# 顶层 import 改为 try/except
c = c.replace(
    "import chromadb\\nfrom sentence_transformers import SentenceTransformer",
    """try:
    import chromadb
    from sentence_transformers import SentenceTransformer
    MEMORY_AVAILABLE = True
except ImportError:
    MEMORY_AVAILABLE = False
    chromadb = None
    SentenceTransformer = None"""
)

# __init__ 中加 MEMORY_AVAILABLE 检查，缺失时直接 return 不初始化
old_init = '    def __init__(self):\n        \\\"\\\"\\\"初始化ChromaDB和Embedding模型\\\"\\\"\\\"\n        if self._initialized:\n            return\n            \n        try:'
new_init = '    def __init__(self):\n        \\\"\\\"\\\"初始化ChromaDB和Embedding模型\\\"\\\"\\\"\n        if self._initialized:\n            return\n\n        if not MEMORY_AVAILABLE:\n            self.client = None\n            self.model = None\n            self.collection = None\n            self._initialized = True\n            logger.warning("⚠️ 向量记忆功能不可用（缺少 chromadb/sentence-transformers）")\n            return\n\n        try:'
c = c.replace(old_init, new_init, 1)

with open(f, "w") as fh:
    fh.write(c)
print("  ✅ memory_service.py 已修补")
PYEOF

# ── 4b. 修补 API 文件的 memory_service 导入 ──────────────────────────────────
python3 << 'PYEOF'
import os
home = os.path.expanduser("~")
files = [
    f"{home}/MuMuAINovel/backend/app/api/chapters.py",
    f"{home}/MuMuAINovel/backend/app/api/memories.py",
    f"{home}/MuMuAINovel/backend/app/api/outlines.py",
    f"{home}/MuMuAINovel/backend/app/api/projects.py",
    f"{home}/MuMuAINovel/backend/app/services/foreshadow_service.py",
]
old = 'from app.services.memory_service import memory_service'
new = 'try:\n    from app.services.memory_service import memory_service\nexcept ImportError:\n    memory_service = None'
count = 0
for f in files:
    if not os.path.exists(f): continue
    with open(f) as fh: c = fh.read()
    if old in c:
        c = c.replace(old, new)
        with open(f, 'w') as fh: fh.write(c)
        count += 1
print(f"  ✅ API 文件已修补（{count} 个）")
PYEOF

# ── 4c. 创建 .env 配置文件 (已有则跳过) ─────────────────────────────────────
mkdir -p "$DATA_DIR" "$LOG_DIR"
if [ ! -f "$BACKEND/.env" ]; then
cat > "$BACKEND/.env" << 'ENVEOF'
# MuMuAINovel Termux 配置
APP_NAME=MuMuAINovel
APP_HOST=0.0.0.0
APP_PORT=8000
DEBUG=false
TZ=Asia/Shanghai

# SQLite 数据库（替代 PostgreSQL）
DATABASE_URL=sqlite+aiosqlite:///data/data/com.termux/files/home/mumuainovel/data/ai_story.db

# 日志
LOG_LEVEL=INFO
LOG_TO_FILE=true
LOG_FILE_PATH=/data/data/com.termux/files/home/mumuainovel/logs/app.log
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=5

# CORS
CORS_ORIGINS=["http://localhost:8000","http://127.0.0.1:8000"]

# ⚠️ 请填入你的 API Key
OPENAI_API_KEY=***
OPENAI_BASE_URL=https://api.openai.com/v1

DEFAULT_AI_PROVIDER=openai
DEFAULT_MODEL=gpt-4o-mini
DEFAULT_TEMPERATURE=0.7
DEFAULT_MAX_TOKENS=4096

# 本地登录账号
LOCAL_AUTH_ENABLED=True
LOCAL_AUTH_USERNAME=admin
LOCAL_AUTH_PASSWORD=admin123
LOCAL_AUTH_DISPLAY_NAME=Admin
ENVEOF
# 替换占位符路径为实际 $HOME 路径
sed -i "s|/data/data/com.termux/files/home|$HOME|g" "$BACKEND/.env"
sed -i "s|LOG_FILE_PATH=.*|LOG_FILE_PATH=$LOG_DIR/app.log|" "$BACKEND/.env"
echo "  ✅ .env 已创建"
else
echo "  ✅ .env 已存在，跳过"
fi
) > "$LOG" 2>&1 &
SPIN $! "修补中" "$LOG"


# =============================================================================
# 步骤 5: 安装 Python 依赖
# 说明: venv 不存在则创建；pip 自动跳过已安装的包，只安装新增的
# =============================================================================
step 5 $TOTAL "安装 Python 依赖"
if [ ! -d "$BACKEND/venv" ]; then
    python -m venv "$BACKEND/venv"
fi
PIP="$BACKEND/venv/bin/pip"

# 写入精简依赖列表 (不含 Termux 不兼容的 psutil/chromadb/sentence-transformers)
cat > "$BACKEND/requirements-lite.txt" << 'REQEOF'
fastapi==0.121.0
uvicorn==0.38.0
python-multipart==0.0.20
sqlalchemy==2.0.36
alembic==1.14.0
aiosqlite==0.22.1
pydantic==2.12.4
pydantic-settings==2.11.0
openai==2.7.0
anthropic==0.72.0
httpx==0.28.1
python-dotenv==1.1.0
aiosmtplib==4.0.2
mcp==1.22.0
greenlet>=3.0
REQEOF

LOG="$TMPDIR/pip-install.log"
(
    $PIP install --upgrade pip setuptools wheel -q $MIRROR
    $PIP install -r "$BACKEND/requirements-lite.txt" $MIRROR
) > "$LOG" 2>&1 &
SPIN $! "安装中" "$LOG"

# =============================================================================
# 步骤 6: 数据库迁移
# 说明: 首次安装创建所有表；重复运行自动跳过已执行的迁移
# =============================================================================
step 6 $TOTAL "数据库迁移"
export DATABASE_URL="sqlite+aiosqlite:///$DATA_DIR/ai_story.db"
LOG="$TMPDIR/alembic.log"
(
    cd "$BACKEND"
    "$BACKEND/venv/bin/python" -m alembic -c alembic-sqlite.ini upgrade head
) > "$LOG" 2>&1 &
SPIN $! "迁移中" "$LOG"

# =============================================================================
# 步骤 7: 安装前端依赖
# 说明: node_modules 已存在则跳过；首次运行 npm install
# =============================================================================
step 7 $TOTAL "安装前端依赖"
cd "$FRONTEND"
if [ -d "node_modules" ] && [ -f "node_modules/.package-lock.json" ]; then
    info "前端依赖已安装，跳过"
else
    LOG="$TMPDIR/npm-install.log"
    npm install --include=dev --loglevel=silent > "$LOG" 2>&1 &
    SPIN $! "安装中" "$LOG"
fi

# =============================================================================
# 步骤 8: 构建前端
# 说明: 每次都重新构建，确保最新代码生效
# =============================================================================
step 8 $TOTAL "构建前端"
node "$FRONTEND/node_modules/typescript/bin/tsc" -b 2>/dev/null || true
LOG="$TMPDIR/vite-build.log"
node "$FRONTEND/node_modules/vite/bin/vite.js" build > "$LOG" 2>&1 &
SPIN $! "构建中" "$LOG"
grep -E "built in" "$LOG" | sed 's/^/    /'

# =============================================================================
# 步骤 9: 创建启动脚本
# 说明: 生成 ~/mumuainovel-start.sh，支持前台/后台运行
# =============================================================================
step 9 $TOTAL "创建启动脚本"
cat > "$HOME/mumuainovel-start.sh" << STARTEOF
#!/bin/bash
# MuMuAINovel Termux 启动脚本
set -e

BACKEND="$BACKEND"
PYTHON="\$BACKEND/venv/bin/python"
DATA_DIR="$DATA_DIR"
LOG_DIR="$LOG_DIR"

mkdir -p "\$DATA_DIR" "\$LOG_DIR"
export DATABASE_URL="sqlite+aiosqlite:///\$DATA_DIR/ai_story.db"
cd "\$BACKEND"

if [ "\$1" = "--bg" ]; then
    echo "🚀 后台启动 MuMuAINovel (端口 8000)..."
    nohup "\$PYTHON" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 \\
        > "\$LOG_DIR/app.log" 2>&1 &
    echo \$! > "$HOME/mumuainovel.pid"
    sleep 2
    if kill -0 \$(cat "$HOME/mumuainovel.pid") 2>/dev/null; then
        echo "✅ 已启动, PID: \$(cat $HOME/mumuainovel.pid)"
    else
        echo "❌ 启动失败，查看日志: \$LOG_DIR/app.log"
        exit 1
    fi
else
    echo "🚀 启动 MuMuAINovel (端口 8000, Ctrl+C 停止)..."
    exec "\$PYTHON" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
fi
STARTEOF
chmod +x "$HOME/mumuainovel-start.sh"
info "启动脚本已创建: ~/mumuainovel-start.sh"

# =============================================================================
# 安装完成
# =============================================================================
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  🎉 MuMuAINovel 安装完成！                   ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║                                              ║${NC}"
echo -e "${GREEN}║  前台运行（Ctrl+C 停止）:                     ║${NC}"
echo -e "${GREEN}║    bash ~/mumuainovel-start.sh                ║${NC}"
echo -e "${GREEN}║                                              ║${NC}"
echo -e "${GREEN}║  后台运行:                                    ║${NC}"
echo -e "${GREEN}║    bash ~/mumuainovel-start.sh --bg           ║${NC}"
echo -e "${GREEN}║                                              ║${NC}"
echo -e "${GREEN}║  停止后台:                                    ║${NC}"
echo -e "${GREEN}║    kill \$(cat ~/mumuainovel.pid)              ║${NC}"
echo -e "${GREEN}║                                              ║${NC}"
echo -e "${GREEN}║  查看日志:                                    ║${NC}"
echo -e "${GREEN}║    tail -f ~/mumuainovel/logs/app.log         ║${NC}"
echo -e "${GREEN}║                                              ║${NC}"
echo -e "${GREEN}║  🌐 访问: http://127.0.0.1:8000               ║${NC}"
echo -e "${GREEN}║  🔑 账号: admin / admin123                    ║${NC}"
echo -e "${GREEN}║                                              ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}  ⚠️  首次使用前请编辑 API Key:${NC}"
echo -e "     nano $BACKEND/.env"
echo -e "     修改 OPENAI_API_KEY 和 OPENAI_BASE_URL"
echo ""
