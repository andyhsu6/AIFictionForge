"""应用配置管理"""
from pydantic_settings import BaseSettings
from typing import Optional
from pathlib import Path
import logging
import os
import uuid

# 获取项目根目录(从backend/app/config.py向上两级)
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# 配置模块使用标准logging（在logger.py初始化之前）
config_logger = logging.getLogger(__name__)

# 数据库配置：PostgreSQL
# 从环境变量获取数据库URL
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://aistoryforge:password@localhost:5432/aistoryforge")

config_logger.debug(f"数据库类型: PostgreSQL")
config_logger.debug(f"数据库URL: {DATABASE_URL}")

class Settings(BaseSettings):
    """应用配置"""
    
    # 应用配置
    app_name: str = "AIFictionForge"
    app_version: str = "1.5.4"
    app_host: str = "0.0.0.0"
    app_port: int = 8008
    debug: bool = False
    
    # 日志配置
    log_level: str = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL
    log_to_file: bool = True  # 是否输出到文件
    log_file_path: str = str(PROJECT_ROOT / "logs" / "app.log")
    log_max_bytes: int = 10 * 1024 * 1024  # 10MB
    log_backup_count: int = 30  # 保留30个备份文件
    log_message_max_chars: int = 2000  # 单条日志消息最大字符数
    
    # CORS配置
    cors_origins: list[str] = ["http://localhost:8008", "http://127.0.0.1:8008", "http://localhost:5173", "http://127.0.0.1:5173"]
    
    # 数据库配置 - PostgreSQL
    database_url: str = DATABASE_URL
    
    # PostgreSQL连接池配置（优化后支持150-200并发用户）
    database_pool_size: int = 50  # 核心连接池大小（优化：从30提升到50）
    database_max_overflow: int = 30  # 最大溢出连接数（优化：从20提升到30）
    database_pool_timeout: int = 90  # 连接池超时秒数（优化：从60提升到90）
    database_pool_recycle: int = 1800  # 连接回收时间秒数（30分钟，防止长时间连接失效）
    database_pool_pre_ping: bool = True  # 连接前ping检测，确保连接有效
    database_pool_use_lifo: bool = True  # 使用LIFO策略提高连接复用率
    
    # 连接池高级配置
    database_echo_pool: bool = False  # 是否记录连接池日志（调试用）
    database_pool_reset_on_return: str = "rollback"  # 连接归还时的重置策略：rollback/commit/none
    database_max_identifier_length: int = 128  # PostgreSQL标识符最大长度
    
    # 会话监控配置
    database_session_max_active: int = 50  # 活跃会话警告阈值（从100降低到50）
    database_session_leak_threshold: int = 100  # 会话泄漏严重告警阈值
    
    # 数据库监控配置
    database_enable_slow_query_log: bool = True  # 启用慢查询日志
    database_slow_query_threshold: float = 1.0  # 慢查询阈值（秒）
    database_enable_metrics: bool = True  # 启用性能指标收集
    
    # AI服务配置
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    xiaomi_mimo_api_key: Optional[str] = None
    xiaomi_mimo_base_url: str = "https://token-plan-cn.xiaomimimo.com/v1"
    gemini_api_key: Optional[str] = None
    gemini_base_url: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    anthropic_base_url: Optional[str] = None
    default_ai_provider: str = "openai"
    # 需求 #55 步骤 4：系统兜底模型常量（字段名 default_model，值是一个仅 8K 窗口的
    # 模型名）已删除。系统绝不替用户猜模型；未配置即抛 validation.ai_model_not_configured。
    default_temperature: float = 0.7
    default_max_tokens: int = 32000
    # Allow Ollama / local Llama / Docker host.docker.internal as AI base URLs.
    # Default false keeps SSRF protection for public deployments.
    allow_private_ai_endpoints: bool = False
    # Comma-separated host allowlist, e.g. "host.docker.internal,127.0.0.1"
    allowed_ai_hosts: str = ""
    
    # MCP配置
    mcp_max_rounds: int = 3  # MCP工具调用最大轮数（全局统一控制）

    # 计划执行器预算（架构计划 A §3 / PR-2c）——默认值必须与
    # app/services/agent_plan_runner.py 的模块常量逐字一致，两者不一致时以常量优先规则生效。
    agent_plan_max_steps: int = 30                 # 一份计划最多步数
    agent_plan_wall_clock_seconds: float = 7200.0  # 计划级总时长上限（秒）
    agent_plan_step_poll_timeout_seconds: float = 900.0   # 单步等子任务终态上限
    agent_plan_poll_interval_seconds: float = 2.0         # 子任务轮询间隔
    agent_plan_step_grace_seconds: float = 3.0            # 步间宽限：等 SQLite WAL 对其他会话可见（PR-4）
    agent_plan_status_message_max_chars: int = 120        # status_message 是 String(500)
    agent_plan_summary_max_chars: int = 8000               # 聚合 tool 消息/收尾文案上限
    agent_plan_running_guardrail_enabled: bool = True      # §7 护栏总开关（回滚用）
    agent_plan_round_budget: int = 3                       # 规划回合工具轮数上限（消费方 = PR-2a 的 service，不走 runner `_limit`）

    # --- 助手 prompt 预算（PR-0c，架构计划 §5）-----------------------------
    # 唯一换算式：clamp(实测窗口 tokens * agent_chars_per_token
    #                   * agent_history_budget_ratio,
    #                   min_chars, max_chars)
    # 四个键的默认值必须与 app/services/agent_prompt_budget.py 的模块常量一致
    # （tests/test_agent_prompt_budget.py::test_config_defaults_match_the_module_constants
    #  会钉住这一点，漂移即红）。
    # ratio=0.3：一个决策轮要先固定重发 系统提示词≈1.5k + 全量工具 schema≈20k 字符，
    #   外加每轮输出余量，剩下才给历史；历史是**每轮重发**的，不是发一次。
    # max=400000：防"把整张窗口当历史"造成成本失控。
    # min=60000：防异常配置算出过小/无界预算，**不是**为小模型兜底
    #   （窗口不足的模型已由计划 B 以 validation.ai_model_below_minimum 拦在系统外）。
    # 调大任何一个数字前，先读 agent_prompt_budget.py 的「来历备忘」。
    agent_history_budget_ratio: float = 0.3
    agent_history_budget_min_chars: int = 60_000
    agent_history_budget_max_chars: int = 400_000
    # 中文≈1 字符/token 的保守近似；定义源仍是 CHARS_PER_TOKEN，此处仅为运维可调。
    agent_chars_per_token: float = 1.0
    
    # LinuxDO OAuth2 配置
    LINUXDO_CLIENT_ID: Optional[str] = None
    LINUXDO_CLIENT_SECRET: Optional[str] = None
    # 回调地址：Docker部署时必须使用实际域名或服务器IP，不能使用localhost
    # 本地开发: http://localhost:8008/api/auth/callback
    # 生产环境: https://your-domain.com/api/auth/callback 或 http://your-ip:8008/api/auth/callback
    LINUXDO_REDIRECT_URI: Optional[str] = None
    # LinuxDO 专用代理配置（仅用于 OAuth token 与用户信息请求，不影响 AI/SMTP/其他请求）
    # 示例: http://127.0.0.1:7890
    LINUXDO_PROXY_URL: Optional[str] = None
    
    # 前端URL配置（用于OAuth回调后重定向）
    # 本地开发: http://localhost:8008
    # 生产环境: https://your-domain.com 或 http://your-ip:8008
    FRONTEND_URL: str = "http://localhost:8008"
    
    # 初始管理员配置（LinuxDO user_id）
    INITIAL_ADMIN_LINUXDO_ID: Optional[str] = None
    
    # 本地账户登录配置
    LOCAL_AUTH_ENABLED: bool = True  # 是否启用本地账户登录
    LOCAL_AUTH_USERNAME: Optional[str] = None  # 本地登录用户名
    LOCAL_AUTH_PASSWORD: Optional[str] = None  # 本地登录密码
    LOCAL_AUTH_DISPLAY_NAME: str = "本地用户"  # 本地用户显示名称
    
    # 会话配置
    SESSION_EXPIRE_MINUTES: int = 120  # 会话过期时间（分钟），默认2小时
    SESSION_REFRESH_THRESHOLD_MINUTES: int = 30  # 会话刷新阈值（分钟），剩余时间少于此值时可刷新
    SESSION_SECRET_KEY: Optional[str] = None  # 会话签名密钥，生产环境必须配置为高强度随机值
    SESSION_COOKIE_SECURE: Optional[bool] = None  # 是否强制 Cookie Secure；None 时按 DEBUG 自动判断

    # 系统 SMTP 默认配置（可被管理员系统设置覆盖）
    SMTP_PROVIDER: str = "qq"
    SMTP_HOST: Optional[str] = "smtp.qq.com"
    SMTP_PORT: int = 465
    SMTP_USERNAME: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_USE_TLS: bool = False
    SMTP_USE_SSL: bool = True
    SMTP_FROM_EMAIL: Optional[str] = None
    SMTP_FROM_NAME: str = "AIFictionForge"
    EMAIL_AUTH_ENABLED: bool = True
    EMAIL_REGISTER_ENABLED: bool = True
    EMAIL_VERIFICATION_CODE_TTL_MINUTES: int = 10
    EMAIL_VERIFICATION_RESEND_INTERVAL_SECONDS: int = 60
    
    # 提示词工坊配置（本地模式默认关闭云端工坊）
    WORKSHOP_MODE: str = "disabled"  # disabled: 本地部署不启用云端工坊
    WORKSHOP_CLOUD_URL: str = ""  # 云端服务地址（默认空，不指向上游）
    WORKSHOP_API_TIMEOUT: int = 30  # 云端API请求超时时间（秒）
    
    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"  # 忽略未定义的环境变量，避免验证错误


# 创建全局配置实例
settings = Settings()
config_logger.info(f"配置加载完成: {settings.app_name} v{settings.app_version}")
config_logger.debug(f"调试模式: {settings.debug}")
config_logger.debug(f"AI提供商: {settings.default_ai_provider}")


# ==================== 提示词工坊实例标识 ====================

def get_or_create_instance_id() -> str:
    """获取或创建实例唯一标识
    
    - Server 模式：固定使用 "server" 作为标识，确保与所有 Client 实例区分
    - Client 模式：从 .instance_id 文件读取或自动生成唯一标识
    """
    # Server 模式使用固定标识
    if settings.WORKSHOP_MODE.lower() == "server":
        config_logger.info("Server 模式：使用固定实例标识 'server'")
        return "server"
    
    # Client 模式：从文件读取或生成
    instance_file = PROJECT_ROOT / ".instance_id"
    if instance_file.exists():
        with open(instance_file, 'r') as f:
            instance_id = f.read().strip()
            if instance_id and instance_id != "server":  # 确保不与 server 冲突
                return instance_id
    
    # 生成新的实例ID
    instance_id = str(uuid.uuid4())[:12]
    try:
        with open(instance_file, 'w') as f:
            f.write(instance_id)
        config_logger.info(f"生成新的实例标识: {instance_id}")
    except Exception as e:
        config_logger.warning(f"无法保存实例标识到文件: {e}")
    
    return instance_id

INSTANCE_ID = get_or_create_instance_id()

def is_workshop_server() -> bool:
    """判断当前实例是否为工坊服务端"""
    return settings.WORKSHOP_MODE.lower() == "server"

config_logger.info(f"提示词工坊模式: {settings.WORKSHOP_MODE}, 实例ID: {INSTANCE_ID}")
