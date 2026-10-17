"""将生成的 Markdown 日报通过 SMTP 发送到邮箱。"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


class EmailNotificationError(RuntimeError):
    """日报邮件无法发送时抛出的统一异常。"""


def send_daily_brief_email(
    brief_path: Path,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    smtp_username: str | None = None,
    smtp_auth_code: str | None = None,
    sender: str | None = None,
    recipient: str | None = None,
) -> None:
    """发送日报正文，并附上原始 Markdown 文件。"""
    smtp_host = smtp_host or os.getenv("SMTP_HOST", "smtp.qq.com")
    smtp_port = smtp_port or int(os.getenv("SMTP_PORT", "465"))
    smtp_username = smtp_username or os.getenv("SMTP_USERNAME")
    smtp_auth_code = smtp_auth_code or os.getenv("SMTP_AUTH_CODE")
    sender = sender or os.getenv("SMTP_FROM") or smtp_username
    recipient = recipient or os.getenv("DAILY_BRIEF_EMAIL_TO")

    required_config = {
        "SMTP_USERNAME": smtp_username,
        "SMTP_AUTH_CODE": smtp_auth_code,
        "DAILY_BRIEF_EMAIL_TO": recipient,
    }
    missing_names = [name for name, value in required_config.items() if not value]
    if missing_names:
        missing_text = "、".join(missing_names)
        raise EmailNotificationError(f"缺少邮件配置：{missing_text}")

    if not brief_path.is_file():
        raise EmailNotificationError(f"日报文件不存在：{brief_path}")

    brief_markdown = brief_path.read_text(encoding="utf-8")
    message = EmailMessage()
    message["Subject"] = f"AI 每日简报｜{brief_path.stem}"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(
        "这是一份自动生成的 AI 每日简报。Markdown 原文件已作为附件发送。\n\n"
        f"{brief_markdown}"
    )
    message.add_attachment(
        brief_markdown.encode("utf-8"),
        maintype="text",
        subtype="markdown",
        filename=brief_path.name,
    )

    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as smtp:
            smtp.login(smtp_username, smtp_auth_code)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        raise EmailNotificationError("SMTP 邮件发送失败，请检查配置和网络。") from error
