import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from email_notifier import EmailNotificationError, send_daily_brief_email


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: int):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.login_arguments: tuple[str, str] | None = None
        self.message = None
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def login(self, username: str, auth_code: str) -> None:
        self.login_arguments = (username, auth_code)

    def send_message(self, message) -> None:
        self.message = message


class TestEmailNotifier(unittest.TestCase):
    def setUp(self) -> None:
        FakeSMTP.instances.clear()

    @patch("email_notifier.smtplib.SMTP_SSL", FakeSMTP)
    def test_send_daily_brief_email_sends_body_and_markdown_attachment(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            brief_path = Path(temporary_directory) / "2026-09-12.md"
            brief_path.write_text("# AI 每日简报\n\n测试内容", encoding="utf-8")

            send_daily_brief_email(
                brief_path=brief_path,
                smtp_host="smtp.example.com",
                smtp_port=465,
                smtp_username="sender@example.com",
                smtp_auth_code="fake-auth-code",
                sender="sender@example.com",
                recipient="recipient@example.com",
            )

        smtp = FakeSMTP.instances[0]
        self.assertEqual(smtp.host, "smtp.example.com")
        self.assertEqual(smtp.login_arguments, ("sender@example.com", "fake-auth-code"))
        self.assertEqual(smtp.message["To"], "recipient@example.com")
        self.assertEqual(smtp.message["Subject"], "AI 每日简报｜2026-09-12")
        self.assertEqual(len(list(smtp.message.iter_attachments())), 1)

    def test_send_daily_brief_email_rejects_missing_configuration(self):
        with self.assertRaises(EmailNotificationError):
            send_daily_brief_email(
                brief_path=Path("missing.md"),
                smtp_username="",
                smtp_auth_code="",
                recipient="",
            )


if __name__ == "__main__":
    unittest.main()
