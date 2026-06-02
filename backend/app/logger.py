"""统一日志配置模块 - Uvicorn风格"""
import json
import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Any, Optional


DEFAULT_LOG_MESSAGE_MAX_CHARS = 2000
DEFAULT_LOG_PREVIEW_MAX_CHARS = 300
SENSITIVE_LOG_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "bearer",
    "cookie",
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
}


def _truncate_text(text: str, max_chars: Optional[int] = DEFAULT_LOG_PREVIEW_MAX_CHARS) -> str:
    """截断长文本，保留原始长度便于排查。"""
    if max_chars is None or max_chars <= 0 or len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated, length={len(text)}]"


def safe_preview(value: Any, max_chars: int = DEFAULT_LOG_PREVIEW_MAX_CHARS) -> str:
    """生成安全预览，避免长文本直接进入日志。"""
    if value is None:
        return "None"
    return _truncate_text(str(value), max_chars)


def _sanitize_for_log(value: Any, depth: int = 0) -> Any:
    """递归清理日志对象，避免敏感字段和正文全文输出。"""
    if depth > 4:
        return f"<{type(value).__name__}>"

    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_str = str(key)
            lower_key = key_str.lower()
            if any(sensitive_key in lower_key for sensitive_key in SENSITIVE_LOG_KEYS):
                sanitized[key_str] = "***REDACTED***"
            elif lower_key in {"content", "prompt", "system_prompt", "chapter_content", "messages", "arguments"}:
                sanitized[key_str] = summarize_log_value(item)
            else:
                sanitized[key_str] = _sanitize_for_log(item, depth + 1)
        return sanitized

    if isinstance(value, (list, tuple)):
        return [_sanitize_for_log(item, depth + 1) for item in value[:10]] + (
            [f"... {len(value) - 10} more items"] if len(value) > 10 else []
        )

    if isinstance(value, str):
        return safe_preview(value)

    return value


def safe_json_preview(value: Any, max_chars: int = 500) -> str:
    """生成可读 JSON 安全预览，无法序列化时退回字符串预览。"""
    try:
        text = json.dumps(_sanitize_for_log(value), ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return _truncate_text(text, max_chars)


def summarize_log_value(value: Any) -> str:
    """返回值的结构摘要，不输出正文内容。"""
    if value is None:
        return "None"
    if isinstance(value, str):
        return f"str(length={len(value)})"
    if isinstance(value, dict):
        fields = []
        for key, item in list(value.items())[:20]:
            if isinstance(item, str):
                fields.append(f"{key}:str(length={len(item)})")
            elif isinstance(item, (list, tuple, set)):
                fields.append(f"{key}:{type(item).__name__}(length={len(item)})")
            elif isinstance(item, dict):
                fields.append(f"{key}:dict(keys={len(item)})")
            else:
                fields.append(f"{key}:{type(item).__name__}")
        suffix = f", +{len(value) - 20} keys" if len(value) > 20 else ""
        return f"dict(keys={len(value)}, fields=[{', '.join(fields)}{suffix}])"
    if isinstance(value, (list, tuple, set)):
        item_types = sorted({type(item).__name__ for item in value})
        return f"{type(value).__name__}(length={len(value)}, item_types={item_types})"
    return type(value).__name__


class UvicornFormatter(logging.Formatter):
    """Uvicorn风格的日志格式化器"""
    
    # 日志级别颜色（ANSI转义码）
    COLORS = {
        'DEBUG': '\033[36m',      # 青色
        'INFO': '\033[32m',       # 绿色
        'WARNING': '\033[33m',    # 黄色
        'ERROR': '\033[31m',      # 红色
        'CRITICAL': '\033[35m',   # 紫色
    }
    RESET = '\033[0m'
    
    def __init__(self, use_colors: bool = True, max_message_chars: int = DEFAULT_LOG_MESSAGE_MAX_CHARS):
        """
        初始化格式化器
        
        Args:
            use_colors: 是否使用颜色（控制台输出使用，文件输出不使用）
        """
        super().__init__()
        self.use_colors = use_colors
        self.max_message_chars = max_message_chars
    
    def format(self, record):
        """格式化日志记录为 Uvicorn 风格"""
        # 获取日志级别名称
        levelname = record.levelname
        
        # 添加颜色（如果启用且终端支持）
        if self.use_colors and sys.stderr.isatty():
            colored_level = f"{self.COLORS.get(levelname, '')}{levelname}{self.RESET}"
        else:
            colored_level = levelname
        
        # 添加请求追踪ID（如果存在）
        request_id = getattr(record, 'request_id', None)
        request_id_str = f" [{request_id}]" if request_id else ""
        
        # 格式化时间戳 (YYYY-MM-DD HH:MM:SS)
        timestamp = self.formatTime(record, self.datefmt)
        
        message = _truncate_text(record.getMessage(), self.max_message_chars)
        # Uvicorn风格格式: INFO:     [2024-01-01 12:00:00] module_name - message [request_id]
        # 注意：INFO后面有5个空格，保持对齐
        return f"{colored_level}:     [{timestamp}] {record.name}{request_id_str} - {message}"


# 全局标志，防止重复初始化
_logging_configured = False

def setup_logging(
    level: str = "INFO",
    log_to_file: bool = False,
    log_file_path: Optional[str] = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 30,
    message_max_chars: int = DEFAULT_LOG_MESSAGE_MAX_CHARS,
):
    """
    配置统一的 Uvicorn 风格日志系统
    
    Args:
        level: 日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_to_file: 是否输出到文件
        log_file_path: 日志文件路径
        max_bytes: 单个日志文件最大字节数（默认10MB）
        backup_count: 保留的备份文件数量（默认30个）
        message_max_chars: 单条日志消息最大字符数（默认2000）
    """
    global _logging_configured
    
    # 如果已经配置过，直接返回
    if _logging_configured:
        return logging.getLogger()
    
    # 获取根日志器
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper()))
    
    # 清除已有的处理器，避免重复
    root_logger.handlers.clear()
    
    if message_max_chars <= 0:
        message_max_chars = DEFAULT_LOG_MESSAGE_MAX_CHARS

    # 1. 创建控制台处理器（带颜色）
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(getattr(logging, level.upper()))
    console_formatter = UvicornFormatter(use_colors=True, max_message_chars=message_max_chars)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)
    
    # 2. 创建文件处理器（如果启用）
    if log_to_file and log_file_path:
        # 确保日志目录存在
        log_file = Path(log_file_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        
        # 使用RotatingFileHandler实现日志轮转
        file_handler = RotatingFileHandler(
            filename=log_file_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(getattr(logging, level.upper()))
        
        # 文件日志不使用颜色
        file_formatter = UvicornFormatter(use_colors=False, max_message_chars=message_max_chars)
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)
        
        # 记录日志配置信息
        root_logger.info(f"日志文件输出已启用: {log_file_path}")
        root_logger.info(f"日志轮转配置: 单文件最大{max_bytes / 1024 / 1024:.1f}MB, 保留{backup_count}个备份")
        root_logger.info(f"单条日志消息最大长度: {message_max_chars}字符")
    
    # 配置第三方库的日志级别
    _configure_third_party_loggers()
    
    # 标记为已配置
    _logging_configured = True
    
    return root_logger


def _configure_third_party_loggers():
    """配置第三方库的日志级别"""
    # SQLAlchemy - 禁用SQL日志
    logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)
    logging.getLogger('sqlalchemy.pool').setLevel(logging.WARNING)
    logging.getLogger('sqlalchemy.dialects').setLevel(logging.WARNING)
    logging.getLogger('sqlalchemy.orm').setLevel(logging.WARNING)
    
    # aiosqlite - 异步SQLite，禁用DEBUG日志
    logging.getLogger('aiosqlite').setLevel(logging.WARNING)
    
    # Watchfiles - 开发时的文件监控，降低级别
    logging.getLogger('watchfiles').setLevel(logging.WARNING)
    
    # httpx/httpcore - HTTP客户端，禁用DEBUG日志
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    
    # openai/anthropic - AI客户端库
    logging.getLogger('openai').setLevel(logging.WARNING)
    logging.getLogger('anthropic').setLevel(logging.WARNING)
    
    # 应用模块 - AI 统计日志需要保留 INFO 级别输出
    logging.getLogger('app.services.ai_service').setLevel(logging.INFO)
    logging.getLogger('app.api.wizard').setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    获取指定名称的日志器
    
    Args:
        name: 日志器名称，通常使用 __name__
        
    Returns:
        配置好的日志器实例
    """
    return logging.getLogger(name)
