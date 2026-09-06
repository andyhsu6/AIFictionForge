"""ApiError 基建：统一错误码 registry + {detail, code, params[, raw]} envelope + 全局异常 handler。

契约（i18n plan todo 4 + task 14a）：
- HTTP 错误响应 body 为 {"detail": str, "code": str, "params": dict}，可选 "raw"。
  detail 保留原中文文案以兼容旧客户端；code/params 为新增结构化字段，供前端翻译。
- raw 是「原始诊断文案」双通道字段（task 14a）：code 未注册时前端只显示本地化
  通用文案，原文移入 raw 仅在调试界面展示。raw 为纯增量字段——未设置时响应
  不含该键；生产环境 500 兜底不外泄异常原文（只进日志）。
- dynamic_detail 是「动态 detail」站点的标记（todo 14）：这些站点的原文由运行时拼接产生、
  无法静态注册，因此前端不再展示 detail，而是按 code 命中 errors.json 的 `dynamic_detail`
  本地化通用文案；原文经可选 raw 通道保留（detail 仍填原中文以兼容旧客户端），raw 仅在
  调试界面展示。
- 完整 registry 在 todo 11 建立；本模块先提供机制 + 高频种子码。
"""
from typing import Any, Dict, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.logger import get_logger

logger = get_logger(__name__)

# dynamic_detail 标记：动态文案站点统一用该 code（todo 14 转换时逐站标注）
DYNAMIC_DETAIL_CODE = "dynamic_detail"

# HTTPException 未注册映射时的 fallback code
HTTP_ERROR_FALLBACK_CODE = "http_error"

# 错误码 registry: code -> (默认中文 detail, 默认 HTTP status)
# 高频种子码；完整码表在 todo 11 扩充。
ERROR_REGISTRY: Dict[str, Tuple[str, int]] = {
    "auth.unauthorized": ("未登录", 401),
    "not_found.chapter": ("章节不存在", 404),
    "not_found.project": ("项目不存在", 404),
    "not_found.outline": ("大纲不存在", 404),
    "not_found.api_route": ("API路径不存在", 404),
    "not_found.frontend_route": ("页面不存在", 404),
    "validation.config": ("配置数据格式错误", 500),
    "internal.error": ("服务器内部错误", 500),
    "validation.error": ("请求参数验证失败", 422),
    "conflict.error": ("资源冲突", 409),
    DYNAMIC_DETAIL_CODE: ("", 500),
}

# (status_code, 默认detail) -> code 反查表：把存量 HTTPException 归类到已知码
_STATUS_DETAIL_TO_CODE: Dict[Tuple[int, str], str] = {
    (meta[1], meta[0]): code
    for code, meta in ERROR_REGISTRY.items()
    if code != DYNAMIC_DETAIL_CODE
}


class ApiError(Exception):
    """带错误码的 API 异常。

    Args:
        code: 错误码（registry key）。未注册的 code 允许，detail 必须显式提供。
        detail: 错误文案；省略时取 registry 默认中文 detail（向后兼容旧客户端）。
        status: HTTP 状态码；省略时取 registry 默认值，再省略则 500。
        params: 结构化参数，供前端模板化翻译（如 {"chapter_id": "..."}）。
        raw: 原始诊断文案（task 14a 双通道）。仅调试界面展示；未设置时响应
            不含 raw 键（纯增量，旧客户端与精确断言不受影响）。
    """

    def __init__(
        self,
        code: str,
        detail: Optional[str] = None,
        status: Optional[int] = None,
        params: Optional[Dict[str, Any]] = None,
        raw: Optional[str] = None,
    ):
        self.code = code
        default_detail, default_status = ERROR_REGISTRY.get(code, ("", 500))
        self.detail = detail if detail is not None else default_detail
        self.status = status if status is not None else default_status
        self.params: Dict[str, Any] = params or {}
        self.raw = raw
        super().__init__(f"[{code}] {self.detail}")

    def to_envelope(self) -> Dict[str, Any]:
        """HTTP 响应 envelope：detail 保留旧中文文案，code/params 为结构化新字段。"""
        content: Dict[str, Any] = {"detail": self.detail, "code": self.code, "params": self.params}
        if self.raw:
            content["raw"] = self.raw
        return content


def envelope(
    detail: str,
    code: str,
    params: Optional[Dict[str, Any]] = None,
    raw: Optional[str] = None,
) -> Dict[str, Any]:
    """构造统一错误 envelope（供 handler 与非异常路径复用）；raw 真值才输出该键。"""
    content: Dict[str, Any] = {"detail": detail, "code": code, "params": params or {}}
    if raw:
        content["raw"] = raw
    return content


def code_for_http_exception(exc: HTTPException) -> str:
    """把存量 HTTPException 归类到 registry code；未命中返回 http_error fallback。"""
    detail = exc.detail if isinstance(exc.detail, str) else ""
    return _STATUS_DETAIL_TO_CODE.get((exc.status_code, detail), HTTP_ERROR_FALLBACK_CODE)


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常 handler（ApiError / HTTPException / 验证错误 / 兜底）。

    handler 逻辑集中在本模块，main.py 只调用一次，便于测试复用同一装配函数。
    """

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        """ApiError → {detail, code, params} envelope。"""
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        """存量 HTTPException → ApiError 化：已知 (status, detail) 归码，未知走 http_error。

        task 14a：detail 保持 byte-identical（旧契约）；未知站点的 detail 即现场
        诊断原文，随 raw 双通道下发，前端只显示本地化通用文案。
        """
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        code = code_for_http_exception(exc)
        raw = getattr(exc, "raw", None)
        if raw is None and code == HTTP_ERROR_FALLBACK_CODE:
            raw = detail
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(detail=detail, code=code, raw=raw),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        """请求验证失败 → 422，复用 Pydantic 错误 type 生成 errors.validation.<type> 码。

        body.errors 保留（旧客户端兼容），并新增 envelope 字段。
        """
        errors = exc.errors()
        first_type = ""
        if errors:
            first_type = str(errors[0].get("type", ""))
        code = f"errors.validation.{first_type}" if first_type else "validation.error"
        logger.error(f"请求验证失败: {errors}")
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "detail": "请求参数验证失败",
                "code": code,
                "params": {"index": 0} if errors else {},
                "errors": errors,
            },
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """未捕获异常 → 500，code=internal.error；真实异常仅记日志，不外泄。"""
        logger.error(f"未处理的异常: {type(exc).__name__}: {str(exc)}", exc_info=True)
        detail, default_status = ERROR_REGISTRY["internal.error"]
        content = envelope(detail=detail, code="internal.error")
        if config_debug():
            # task 14a：debug 才随 raw 下发原文；生产环境原文只进日志。
            content["message"] = str(exc)  # compat alias，保留一个版本
            content["raw"] = str(exc)
        else:
            content["message"] = "请稍后重试"
        return JSONResponse(status_code=default_status, content=content)


def config_debug() -> bool:
    """debug 开关（延迟导入避免模块循环）。debug 模式下兜底 500 附带原始异常文本。"""
    try:
        from app.config import settings
        return bool(getattr(settings, "debug", False))
    except Exception:
        return False
