"""AI Story Creator - 后端应用包"""
__version__ = "1.5.4"

# httpx 会把 NO_PROXY 里带方括号的 IPv6（如 [::1]）解析成非法代理挂载，
# 导致任何 httpx.AsyncClient() 构造即抛 InvalidURL（issue #259）。
# 规范里 IPv6 写不写方括号语义相同，进程启动时归一化一次即可。
from app.core.proxy_env import sanitize_no_proxy_env  # noqa: E402

sanitize_no_proxy_env()
