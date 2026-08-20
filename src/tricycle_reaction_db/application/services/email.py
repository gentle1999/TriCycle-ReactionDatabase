"""Small, synchronous SMTP adapter used by invitation workflows."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from enum import StrEnum

from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.core.observability import SMTP_DELIVERIES
from tricycle_reaction_db.core.tls import verified_tls_context
from tricycle_reaction_db.domain.enums import ProjectRole


class EmailDeliveryStatus(StrEnum):
    LINK_ONLY = "link_only"
    SENT = "sent"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EmailDeliveryResult:
    status: EmailDeliveryStatus
    error: str | None = None


def smtp_tls_context(*, ca_bundle: str | None) -> ssl.SSLContext:
    """Build a verifying context instead of smtplib's permissive fallback."""

    return verified_tls_context(ca_bundle=ca_bundle)


def _send_smtp(
    *,
    recipient: str,
    subject: str,
    text: str,
) -> None:
    settings = get_settings()
    if not settings.smtp_host or not settings.smtp_from_email:
        raise RuntimeError("SMTP configuration is incomplete")
    message = EmailMessage()
    message["From"] = settings.smtp_from_email
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(text)
    with smtplib.SMTP(
        settings.smtp_host,
        settings.smtp_port,
        timeout=settings.smtp_timeout_seconds,
    ) as client:
        if settings.smtp_starttls:
            client.starttls(context=smtp_tls_context(ca_bundle=settings.smtp_ca_bundle))
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password or "")
        client.send_message(message)


class EmailDeliveryService:
    @staticmethod
    async def send_project_invitation(
        *,
        recipient: str,
        project_name: str,
        role: ProjectRole,
        accept_url: str,
        expires_at: datetime,
    ) -> EmailDeliveryResult:
        settings = get_settings()
        if settings.email_delivery_mode == "link":
            return EmailDeliveryResult(EmailDeliveryStatus.LINK_ONLY)
        body = (
            f"你已被邀请加入项目“{project_name}”。\n\n"
            f"项目角色：{role.value}\n"
            "请在 "
            f"{expires_at.astimezone().strftime('%Y-%m-%d %H:%M %Z')} 前打开以下链接接受邀请：\n"
            f"{accept_url}\n"
        )
        try:
            await asyncio.to_thread(
                _send_smtp,
                recipient=recipient,
                subject=f"{settings.brand_name} 项目邀请：{project_name}",
                text=body,
            )
        except Exception as error:  # SMTP libraries expose many provider-specific errors.
            SMTP_DELIVERIES.labels(outcome="failed").inc()
            return EmailDeliveryResult(
                EmailDeliveryStatus.FAILED,
                error=str(error)[:500] or "SMTP delivery failed",
            )
        SMTP_DELIVERIES.labels(outcome="sent").inc()
        return EmailDeliveryResult(EmailDeliveryStatus.SENT)


__all__ = ["EmailDeliveryResult", "EmailDeliveryService", "EmailDeliveryStatus"]
