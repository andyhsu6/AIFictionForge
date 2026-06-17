"""SMTP 邮件发送服务"""
from __future__ import annotations

from email.message import EmailMessage
from typing import Optional

import aiosmtplib

from app.logger import get_logger

logger = get_logger(__name__)


class EmailService:
    """系统 SMTP 邮件发送服务"""

    async def send_mail(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        use_tls: bool,
        use_ssl: bool,
        from_email: str,
        from_name: str,
        to_email: str,
        subject: str,
        text_body: str,
        html_body: Optional[str] = None,
    ) -> None:
        if use_tls and use_ssl:
            raise ValueError("SMTP 配置错误：TLS 和 SSL 不能同时启用")

        message = EmailMessage()
        message['From'] = f'{from_name} <{from_email}>' if from_name else from_email
        message['To'] = to_email
        message['Subject'] = subject
        message.set_content(text_body)
        if html_body:
            message.add_alternative(html_body, subtype='html')

        logger.info(f"[SMTP] 准备发送测试邮件到 {self._mask_email(to_email)}，服务器: {host}:{port}")

        await aiosmtplib.send(
            message,
            hostname=host,
            port=port,
            username=username,
            password=password,
            use_tls=use_ssl,
            start_tls=use_tls,
            timeout=20,
        )

        logger.info(f"[SMTP] 测试邮件发送成功: {self._mask_email(to_email)}")

    @staticmethod
    def _mask_email(email: str) -> str:
        if '@' not in email:
            return email
        name, domain = email.split('@', 1)
        if len(name) <= 2:
            masked_name = name[0] + '*'
        else:
            masked_name = name[0] + '*' * (len(name) - 2) + name[-1]
        return f"{masked_name}@{domain}"


email_service = EmailService()
