"""mailer 单测：发送内容、鉴权快败、退避重试、重试耗尽（FR-5/FR-7）。全 mock 无网络。"""
from __future__ import annotations

import smtplib

import pytest

from news_crawler.mailer import MailAuthError, MailError, Mailer


def make_mailer(smtp_factory, sleep=None):
    return Mailer(
        host="smtp.163.com",
        port=465,
        user="me@example.com",
        auth_code="authcode",
        sender="me@example.com",
        recipient="me@example.com",
        sleep=sleep or (lambda s: None),
        smtp_factory=smtp_factory,
    )


def test_send_html_success():
    captured = {}

    class FakeSMTP:
        def login(self, user, password):
            captured["login"] = user

        def send_message(self, message):
            captured["message"] = message

        def quit(self):
            captured["quit"] = True

    make_mailer(lambda h, p: FakeSMTP()).send_html("每日情报", "<p>正文</p>")

    assert captured["login"] == "me@example.com"
    assert captured["quit"] is True
    message = captured["message"]
    assert message["Subject"] == "每日情报"
    assert message["To"] == "me@example.com"
    assert "<p>正文</p>" in message.get_content()


def test_auth_error_fails_fast_without_retry():
    attempts = {"n": 0}

    class FakeSMTP:
        def login(self, user, password):
            attempts["n"] += 1
            raise smtplib.SMTPAuthenticationError(535, b"auth failed")

        def quit(self):
            pass

    with pytest.raises(MailAuthError, match="535"):
        make_mailer(lambda h, p: FakeSMTP()).send_text("s", "b")
    assert attempts["n"] == 1


def test_retry_on_transient_error_then_success():
    outcomes = iter(["fail", "fail", "ok"])
    attempts = {"n": 0}
    sleeps: list[float] = []

    class FakeSMTP:
        def login(self, user, password):
            pass

        def send_message(self, message):
            attempts["n"] += 1
            if next(outcomes) != "ok":
                raise smtplib.SMTPServerDisconnected("boom")

        def quit(self):
            pass

    mailer = Mailer(
        host="h", port=465, user="u", auth_code="c", sender="u", recipient="r",
        sleep=sleeps.append, smtp_factory=lambda h, p: FakeSMTP(),
    )
    mailer.send_text("s", "b")
    assert attempts["n"] == 3
    assert sleeps == [1.0, 2.0]


def test_retries_exhausted_raises_mail_error():
    attempts = {"n": 0}

    class FakeSMTP:
        def login(self, user, password):
            pass

        def send_message(self, message):
            attempts["n"] += 1
            raise smtplib.SMTPServerDisconnected("down")

        def quit(self):
            pass

    with pytest.raises(MailError, match="重试 3 次"):
        make_mailer(lambda h, p: FakeSMTP()).send_text("s", "b")
    assert attempts["n"] == 4
