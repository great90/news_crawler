"""SMTP 邮件发送（FR-5/FR-7）。

SSL(465) 连接；鉴权失败（授权码错）不重试直接抛 MailAuthError，
连接/临时错误按指数退避重试至多 3 次。日志不输出任何凭据。
"""
from __future__ import annotations

import logging
import smtplib
import socket
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

logger = logging.getLogger(__name__)

BACKOFF_BASE_SECONDS = 1.0
CONNECT_TIMEOUT_SECONDS = 30.0


class MailError(Exception):
    """邮件发送失败基类。"""


class MailAuthError(MailError):
    """SMTP 鉴权失败（账号/授权码错误），重试无意义。"""


class Mailer:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        auth_code: str,
        sender: str,
        recipient: str,
        max_retries: int = 3,
        sleep=time.sleep,
        smtp_factory=None,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._auth_code = auth_code
        self._sender = sender
        self._recipient = recipient
        self._max_retries = max_retries
        self._sleep = sleep
        self._smtp_factory = smtp_factory or (
            lambda host_, port_: smtplib.SMTP_SSL(host_, port_, timeout=CONNECT_TIMEOUT_SECONDS)
        )

    def send_html(self, subject: str, html: str) -> None:
        self._send(subject, html, subtype="html")

    def send_text(self, subject: str, body: str) -> None:
        self._send(subject, body, subtype="plain")

    def _send(self, subject: str, content: str, subtype: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self._sender
        message["To"] = self._recipient
        message["Date"] = formatdate(localtime=True)
        domain = self._sender.rsplit("@", 1)[-1] if "@" in self._sender else "localhost"
        message["Message-ID"] = make_msgid(domain=domain)
        message.set_content(content, subtype=subtype, charset="utf-8")

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            smtp = None
            try:
                smtp = self._smtp_factory(self._host, self._port)
                smtp.login(self._user, self._auth_code)
                smtp.send_message(message)
                logger.info("邮件已发送：%s", subject)
                return
            except smtplib.SMTPAuthenticationError as exc:
                raise MailAuthError(f"SMTP 鉴权失败（{exc.smtp_code}）：请检查 SMTP_USER 与授权码") from exc
            except (smtplib.SMTPException, socket.timeout, OSError) as exc:
                last_error = exc
                logger.warning("邮件发送错误（第 %d 次）：%s", attempt + 1, exc)
                self._backoff(attempt)
            finally:
                if smtp is not None:
                    try:
                        smtp.quit()
                    except Exception:
                        pass
        raise MailError(f"重试 {self._max_retries} 次后仍失败：{last_error}")

    def _backoff(self, attempt: int) -> None:
        if attempt < self._max_retries:
            delay = BACKOFF_BASE_SECONDS * (2**attempt)
            logger.info("退避 %.1fs 后重试", delay)
            self._sleep(delay)
