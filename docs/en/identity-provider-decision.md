# Identity-Provider Decision Record

[中文](../identity-provider-decision.md) | [Documentation index](README.md)

> Dated architecture decision record.

## Decision

The recorded decision keeps Keycloak as the supported OIDC provider rather than
introducing a replacement identity service during the pre-1.0 work. The
application owns project authorization and local session state, while the OIDC
provider owns passwords, authentication, user verification, and token issuance.

## Operational Boundary

Development Keycloak uses `start-dev` and is only a local acceptance fixture.
Production uses authorization-code + PKCE against a stable external issuer with
TLS, protected administration, durable signing keys, and independent backup.
The configured issuer must exactly match discovery/JWKS/token issuer claims;
an issuer mismatch is a deployment configuration error.

The paired Chinese record preserves the original candidates, recovery exercise,
migration boundary, and dated rationale.
