"""SMTP delivery for Kiyra sign-in codes.

Email delivery is mandatory. There is no console fallback and no code in API
responses: if SMTP is unconfigured the auth router fails closed with HTTP 503,
and if a send fails the issued code is deleted so it cannot be redeemed by
anyone who never received it.
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

from config import settings


def smtp_configured() -> bool:
    """True only when real authenticated SMTP delivery is possible.

    A username without a password is a half-configured relay: it would issue
    and persist an OTP before ``login()`` failed. Fail closed with 503 instead.
    """
    if not (settings.smtp_host and settings.smtp_username):
        return False
    return bool(settings.smtp_password)


def _sender() -> str:
    return settings.smtp_from or settings.smtp_username or "no-reply@kiyra.local"


def _text_body(full_name: str, code: str, ttl_minutes: int) -> str:
    greeting = f"Hi {full_name.split()[0]}," if full_name.strip() else "Hi,"
    return (
        f"{greeting}\n\n"
        f"Your Kiyra verification code is:\n\n"
        f"    {code}\n\n"
        f"It expires in {ttl_minutes} minutes. If you didn't request this code, "
        f"you can safely ignore this email.\n\n"
        f"— Kiyra\n"
    )


def _html_body(full_name: str, code: str, ttl_minutes: int) -> str:
    greeting = f"Hi {full_name.split()[0]}," if full_name.strip() else "Hi,"
    return f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#0f1219;font-family:Segoe UI,Arial,sans-serif">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:32px 0">
      <tr>
        <td align="center">
          <table role="presentation" width="440" cellspacing="0" cellpadding="0"
                 style="background:#171b25;border:1px solid #2b3342;border-radius:14px;padding:32px">
            <tr>
              <td style="color:#3ddc84;font-size:20px;font-weight:700;padding-bottom:8px">
                ◆ Kiyra
              </td>
            </tr>
            <tr>
              <td style="color:#e8ecf3;font-size:15px;padding-bottom:18px">
                {greeting}
              </td>
            </tr>
            <tr>
              <td style="color:#8d97ab;font-size:14px;padding-bottom:18px">
                Use this code to finish signing in. It expires in
                <strong style="color:#e8ecf3">{ttl_minutes} minutes</strong>.
              </td>
            </tr>
            <tr>
              <td align="center" style="padding:8px 0 22px 0">
                <div style="display:inline-block;background:#0f131b;border:1px solid #2b3342;
                            border-radius:10px;padding:16px 28px;color:#3ddc84;
                            font-size:30px;letter-spacing:8px;font-weight:700;
                            font-family:Consolas,monospace">
                  {code}
                </div>
              </td>
            </tr>
            <tr>
              <td style="color:#8d97ab;font-size:12px;line-height:1.5">
                If you didn't request this code, you can safely ignore this email.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


def send_otp_email(to_email: str, full_name: str, code: str,
                   ttl_seconds: int) -> Optional[str]:
    """Send the code over SMTP.

    Returns ``None`` on success, or a human-readable error string on failure.
    """
    if not smtp_configured():
        return "SMTP is not configured"

    ttl_minutes = max(1, round(ttl_seconds / 60))
    msg = EmailMessage()
    msg["Subject"] = f"Your Kiyra verification code: {code}"
    msg["From"] = _sender()
    msg["To"] = to_email
    msg.set_content(_text_body(full_name, code, ttl_minutes))
    msg.add_alternative(_html_body(full_name, code, ttl_minutes), subtype="html")

    try:
        if settings.smtp_use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                  timeout=settings.smtp_timeout,
                                  context=context) as smtp:
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port,
                              timeout=settings.smtp_timeout) as smtp:
                smtp.ehlo()
                if settings.smtp_use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(msg)
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller
        return f"{type(exc).__name__}: {exc}"
    return None
