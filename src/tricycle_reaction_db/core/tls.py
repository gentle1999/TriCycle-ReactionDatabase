"""TLS helpers shared by external service adapters."""

from __future__ import annotations

import ssl


def verified_tls_context(*, ca_bundle: str | None) -> ssl.SSLContext:
    """Return a context that verifies both the certificate chain and hostname."""

    return ssl.create_default_context(cafile=ca_bundle)


__all__ = ["verified_tls_context"]
