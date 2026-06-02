"""
LinuxDO OAuth2 服务
"""
import httpx
import secrets
from typing import Optional, Dict, Any
from app.config import settings
from app.logger import get_logger, safe_json_preview, safe_preview

logger = get_logger(__name__)


class LinuxDOOAuthService:
    """LinuxDO OAuth2 服务类"""
    
    # LinuxDO OAuth2 端点
    AUTHORIZE_URL = "https://connect.linux.do/oauth2/authorize"
    TOKEN_URL = "https://connect.linux.do/oauth2/token"
    USERINFO_URL = "https://connect.linux.do/api/user"  # 修复：使用正确的用户信息端点
    
    def __init__(self):
        self.client_id = settings.LINUXDO_CLIENT_ID
        self.client_secret = settings.LINUXDO_CLIENT_SECRET
        self.redirect_uri = settings.LINUXDO_REDIRECT_URI
        self.proxy_url = settings.LINUXDO_PROXY_URL
        
        # 如果未配置，使用默认值（本地开发）
        if not self.redirect_uri:
            self.redirect_uri = "http://localhost:8000/api/auth/callback"
            logger.warning(
                "⚠️  LINUXDO_REDIRECT_URI 未配置，使用默认值: http://localhost:8000/api/auth/callback\n"
                "如需使用 OAuth 登录，请在 .env 文件中配置：\n"
                "本地开发: LINUXDO_REDIRECT_URI=http://localhost:8000/api/auth/callback\n"
                "Docker部署: LINUXDO_REDIRECT_URI=https://your-domain.com/api/auth/callback"
            )
        
        # 警告：检查是否使用了localhost（在非开发环境）
        if not settings.debug and "localhost" in self.redirect_uri.lower():
            logger.warning(
                f"⚠️  生产环境检测到使用 localhost 作为回调地址: {self.redirect_uri}\n"
                "这可能导致OAuth回调失败！请使用实际的域名或服务器IP。"
            )

        if self.proxy_url:
            logger.info("LinuxDO OAuth 已启用专用代理: %s", self.proxy_url)

    def _client_options(self, **overrides) -> Dict[str, Any]:
        """构建 LinuxDO 专用 HTTP 客户端参数。"""
        options: Dict[str, Any] = {
            "trust_env": False,
        }
        if self.proxy_url:
            options["proxy"] = self.proxy_url
        options.update(overrides)
        return options
        
    def generate_state(self) -> str:
        """生成随机 state 参数"""
        return secrets.token_urlsafe(32)
    
    def get_authorization_url(self, state: str) -> str:
        """
        获取授权 URL
        
        Args:
            state: 随机 state 参数
            
        Returns:
            授权 URL
        """
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "read",
            "state": state
        }
        
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        return f"{self.AUTHORIZE_URL}?{query_string}"
    
    async def get_access_token(self, code: str) -> Optional[Dict[str, Any]]:
        """
        使用授权码获取访问令牌
        
        Args:
            code: 授权码
            
        Returns:
            包含 access_token 的字典,失败返回 None
        """
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri
        }
        
        try:
            async with httpx.AsyncClient(**self._client_options(timeout=30.0)) as client:
                response = await client.post(
                    self.TOKEN_URL,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"}
                )
                
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.error("获取访问令牌失败: status=%s response=%s", response.status_code, safe_preview(response.text, 500))
                    return None
                     
        except Exception as e:
            logger.error("获取访问令牌异常: %s", e)
            return None
    
    async def get_user_info(self, access_token: str) -> Optional[Dict[str, Any]]:
        """
        使用访问令牌获取用户信息
        
        Args:
            access_token: 访问令牌
            
        Returns:
            用户信息字典,失败返回 None
        """
        try:
            # 添加真实浏览器请求头，避免被 Cloudflare 拦截
            headers = {
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            
            # 不自动处理编码，让 httpx 自动解压
            async with httpx.AsyncClient(**self._client_options(follow_redirects=True, timeout=30.0)) as client:
                response = await client.get(
                    self.USERINFO_URL,
                    headers=headers
                )
                
                logger.debug(
                    "获取用户信息响应: status=%s headers=%s",
                    response.status_code,
                    safe_json_preview(dict(response.headers), 500),
                )
                
                if response.status_code == 200:
                    try:
                        user_data = response.json()
                        logger.debug("用户信息获取成功: %s", safe_json_preview(user_data, 500))
                        return user_data
                    except Exception as json_error:
                        logger.error("解析用户信息 JSON 失败: %s, response=%s", json_error, safe_preview(response.text, 300))
                        return None
                else:
                    logger.error("获取用户信息失败: status=%s response=%s", response.status_code, safe_preview(response.text, 300))
                    return None
                     
        except Exception as e:
            logger.error("获取用户信息异常: %s: %s", type(e).__name__, e, exc_info=True)
            return None
