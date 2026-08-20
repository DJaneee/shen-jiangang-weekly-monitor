#!/usr/bin/env python3
"""Send the latest generated digest using environment-provided SMTP secrets."""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
REPORT_DIR = APP_DIR / "reports"


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing GitHub secret/environment variable: {name}")
    return value


def main() -> int:
    host = required("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "465"))
    security = os.environ.get("SMTP_SECURITY", "ssl").lower()
    username = required("SMTP_USERNAME")
    password = required("SMTP_PASSWORD_OR_APP_TOKEN")
    sender = required("FROM_EMAIL")
    recipient = required("TO_EMAIL")

    reports = sorted(REPORT_DIR.glob("shen_weekly_digest_*.html"))
    if not reports:
        raise RuntimeError("No generated HTML digest found")
    report = reports[-1]

    message = EmailMessage()
    message["Subject"] = "沈剑刚教授公开动态周报"
    message["From"] = sender
    message["To"] = recipient
    message.set_content("本周HTML周报已作为附件发送，请使用支持HTML的邮件客户端查看。")
    message.add_attachment(report.read_bytes(), maintype="text", subtype="html", filename=report.name)

    if security in {"ssl", "smtps"}:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context()) as client:
            client.login(username, password)
            client.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as client:
            client.ehlo()
            if security in {"starttls", "tls"}:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            client.login(username, password)
            client.send_message(message)

    print("DIGEST_EMAIL_SENT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

