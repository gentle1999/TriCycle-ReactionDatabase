import asyncio
import ssl
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from tricycle_reaction_db.api import authentication as api_authentication
from tricycle_reaction_db.api import core
from tricycle_reaction_db.api.app import create_app
from tricycle_reaction_db.api.routes import auth as auth_routes
from tricycle_reaction_db.api.routes import uploads as upload_routes
from tricycle_reaction_db.application.dtos import (
    ArtifactBatchUploadItem,
    ArtifactBatchUploadResult,
    ArtifactPreview,
    ArtifactUploadResult,
    ArtifactValidationResult,
)
from tricycle_reaction_db.application.services import (
    ArtifactContentService,
    ArtifactDownload,
    ArtifactForbiddenError,
    ArtifactUploadService,
    AuthenticatedPrincipal,
    AuthenticationError,
    AuthenticationService,
)
from tricycle_reaction_db.application.services import authentication as authentication_module
from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadLimitError,
)
from tricycle_reaction_db.core.config import Settings
from tricycle_reaction_db.domain.enums import ArtifactKind, StorageStatus
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID

PUBLIC_ARTIFACT_ID = UUID("00000000-0000-7000-8000-000000000601")
PROJECT_ARTIFACT_ID = UUID("00000000-0000-7000-8000-000000000602")


async def _reject_authentication(_: str | None) -> None:
    raise AuthenticationError("Bearer access token required")


async def _optional_anonymous(authorization: str | None) -> None:
    if authorization is not None:
        raise AuthenticationError("invalid OIDC access token")


def test_production_oidc_discovery_is_validated_before_endpoints_are_used() -> None:
    settings = Settings(
        _env_file=None,
        auth_mode="oidc",
        oidc_issuer="https://identity.example.test",
        oidc_audience="reaction-database",
        oidc_jwks_url="https://identity.example.test/jwks.json",
    ).model_copy(update={"environment": "production"})
    payload = {
        "issuer": "https://identity.example.test",
        "authorization_endpoint": "https://identity.example.test/authorize",
        "token_endpoint": "https://identity.example.test/token",
        "end_session_endpoint": "https://identity.example.test/logout",
        "code_challenge_methods_supported": ["S256"],
    }

    metadata = authentication_module._validated_oidc_metadata(payload, settings)

    assert metadata["token_endpoint"] == "https://identity.example.test/token"


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"issuer": "https://other.example.test"}, "issuer does not match"),
        ({"token_endpoint": "http://identity.example.test/token"}, "must use HTTPS"),
        ({"code_challenge_methods_supported": []}, "PKCE S256"),
    ],
)
def test_production_oidc_discovery_rejects_unsafe_metadata(
    updates: dict[str, object],
    message: str,
) -> None:
    settings = Settings(
        _env_file=None,
        auth_mode="oidc",
        oidc_issuer="https://identity.example.test",
        oidc_audience="reaction-database",
        oidc_jwks_url="https://identity.example.test/jwks.json",
    ).model_copy(update={"environment": "production"})
    payload: dict[str, object] = {
        "issuer": "https://identity.example.test",
        "authorization_endpoint": "https://identity.example.test/authorize",
        "token_endpoint": "https://identity.example.test/token",
        "code_challenge_methods_supported": ["S256"],
        **updates,
    }

    with pytest.raises(AuthenticationError, match=message):
        authentication_module._validated_oidc_metadata(payload, settings)


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/api/depictions/geometry/example.sdf", True),
        ("GET", "/api/depictions/topology/example.mol", True),
        ("POST", "/api/depictions/geometry/example.sdf", False),
        ("GET", "/api/logical-reactions", False),
    ],
)
def test_anonymous_principal_route_intent(
    method: str,
    path: str,
    expected: bool,
) -> None:
    request = Request({"type": "http", "method": method, "path": path, "headers": []})
    assert api_authentication._allows_anonymous_principal(request) is expected


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([("host", "remote.example.test:5173")], "http://remote.example.test:5173/reactions"),
        (
            [
                ("host", "127.0.0.1:8000"),
                ("x-forwarded-host", "remote.example.test"),
                ("x-forwarded-proto", "https"),
            ],
            "https://remote.example.test/reactions",
        ),
    ],
)
def test_development_frontend_redirect_uses_request_origin(
    monkeypatch: pytest.MonkeyPatch,
    headers: list[tuple[str, str]],
    expected: str,
) -> None:
    monkeypatch.setattr(auth_routes, "get_settings", lambda: Settings(_env_file=None))
    request = Request(
        {
            "type": "http",
            "scheme": "http",
            "path": "/api/auth/login",
            "headers": [(name.encode(), value.encode()) for name, value in headers],
        }
    )

    assert auth_routes._frontend_url("/reactions", request) == expected


@pytest.mark.asyncio
async def test_development_login_redirect_is_relative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_routes, "get_settings", lambda: Settings(_env_file=None))
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://remote.example.test:5173",
        follow_redirects=False,
    ) as client:
        response = await client.get("/api/auth/login?return_to=%2Freactions")

    assert response.status_code == 303
    assert response.headers["location"] == "/reactions"


@pytest.mark.asyncio
async def test_optional_authentication_only_allows_anonymous_in_oidc_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    development = Settings(_env_file=None)
    monkeypatch.setattr(authentication_module, "get_settings", lambda: development)
    principal = await AuthenticationService.authenticate_optional(None)
    assert principal is not None
    assert principal.user_id == DEVELOPMENT_USER_ID

    oidc = Settings(
        _env_file=None,
        auth_mode="oidc",
        oidc_issuer="https://identity.example.test",
        oidc_audience="reaction-database",
        oidc_jwks_url="https://identity.example.test/jwks.json",
    )
    monkeypatch.setattr(authentication_module, "get_settings", lambda: oidc)
    assert await AuthenticationService.authenticate_optional(None) is None


@pytest.mark.asyncio
async def test_mcp_bearer_is_validated_before_development_shortcut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    development = Settings(_env_file=None)
    monkeypatch.setattr(authentication_module, "get_settings", lambda: development)
    expected = AuthenticatedPrincipal(
        user_id=DEVELOPMENT_USER_ID,
        display_name="Development User",
        primary_email="developer@localhost",
        is_service_account=False,
        issuer="urn:tricycle:development",
        subject="development-user",
    )

    async def authenticate_mcp(raw_token: str) -> AuthenticatedPrincipal:
        assert raw_token == "mcp_test-token"
        return expected

    monkeypatch.setattr(
        AuthenticationService,
        "authenticate_mcp_access_token",
        staticmethod(authenticate_mcp),
    )

    principal = await AuthenticationService.authenticate("Bearer mcp_test-token")
    assert principal == expected


@pytest.mark.asyncio
async def test_oidc_logout_url_uses_provider_end_session_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        auth_mode="oidc",
        oidc_issuer="https://identity.example.test",
        oidc_audience="reaction-database",
        oidc_jwks_url="https://identity.example.test/jwks.json",
        oidc_client_id="reaction-client",
    )
    monkeypatch.setattr(authentication_module, "get_settings", lambda: settings)

    async def metadata() -> dict[str, str]:
        return {"end_session_endpoint": "https://identity.example.test/logout"}

    monkeypatch.setattr(AuthenticationService, "oidc_metadata", staticmethod(metadata))
    result = await AuthenticationService.oidc_logout_url(
        post_logout_redirect_uri="https://app.example.test/account",
        id_token_hint="signed-id-token",
    )

    assert result is not None
    parsed = urlparse(result)
    assert parsed.path == "/logout"
    assert parse_qs(parsed.query) == {
        "client_id": ["reaction-client"],
        "id_token_hint": ["signed-id-token"],
        "post_logout_redirect_uri": ["https://app.example.test/account"],
    }


@pytest.mark.asyncio
async def test_oidc_authorization_url_requires_s256_pkce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        auth_mode="oidc",
        oidc_issuer="https://identity.example.test",
        oidc_audience="reaction-database",
        oidc_jwks_url="https://identity.example.test/jwks.json",
        oidc_client_id="reaction-client",
    )
    monkeypatch.setattr(authentication_module, "get_settings", lambda: settings)

    async def metadata() -> dict[str, str]:
        return {"authorization_endpoint": "https://identity.example.test/authorize"}

    monkeypatch.setattr(AuthenticationService, "oidc_metadata", staticmethod(metadata))
    result = await AuthenticationService.oidc_authorization_url(
        state="state-value",
        nonce="nonce-value",
        code_challenge="challenge-value",
        redirect_uri="https://app.example.test/api/auth/callback",
    )

    assert parse_qs(urlparse(result).query) == {
        "client_id": ["reaction-client"],
        "code_challenge": ["challenge-value"],
        "code_challenge_method": ["S256"],
        "nonce": ["nonce-value"],
        "redirect_uri": ["https://app.example.test/api/auth/callback"],
        "response_type": ["code"],
        "scope": ["openid profile email"],
        "state": ["state-value"],
    }


@pytest.mark.asyncio
async def test_oidc_code_exchange_forwards_pkce_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        auth_mode="oidc",
        oidc_issuer="https://identity.example.test",
        oidc_audience="reaction-database",
        oidc_jwks_url="https://identity.example.test/jwks.json",
        oidc_client_id="reaction-client",
        oidc_client_secret="client-secret",
    )
    monkeypatch.setattr(authentication_module, "get_settings", lambda: settings)

    async def metadata() -> dict[str, str]:
        return {"token_endpoint": "https://identity.example.test/token"}

    monkeypatch.setattr(AuthenticationService, "oidc_metadata", staticmethod(metadata))
    monkeypatch.setattr(
        authentication_module,
        "_decode_oidc_token",
        lambda token, _settings: {"sub": token, "iss": settings.oidc_issuer},
    )
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"access_token": "signed-access-token"}

    class Client:
        def __init__(self, **kwargs: object) -> None:
            captured["verify"] = kwargs.get("verify")
            return None

        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, url: str, *, data: dict[str, str]) -> Response:
            captured["url"] = url
            captured["data"] = data
            return Response()

    monkeypatch.setattr(authentication_module.httpx, "AsyncClient", Client)
    claims, id_token = await AuthenticationService.exchange_oidc_code(
        code="one-time-code",
        code_verifier="pkce-verifier",
        redirect_uri="https://app.example.test/api/auth/callback",
    )

    assert id_token is None
    assert claims == {"sub": "signed-access-token", "iss": settings.oidc_issuer}
    assert isinstance(captured.pop("verify"), ssl.SSLContext)
    assert captured == {
        "url": "https://identity.example.test/token",
        "data": {
            "grant_type": "authorization_code",
            "code": "one-time-code",
            "client_id": "reaction-client",
            "client_secret": "client-secret",
            "redirect_uri": "https://app.example.test/api/auth/callback",
            "code_verifier": "pkce-verifier",
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_query",
    [
        "error=access_denied",
        "state=state-without-code",
    ],
)
async def test_oidc_callback_failure_clears_one_time_state_cookie(
    monkeypatch: pytest.MonkeyPatch,
    callback_query: str,
) -> None:
    settings = Settings(
        _env_file=None,
        auth_mode="oidc",
        oidc_issuer="https://identity.example.test",
        oidc_audience="reaction-database",
        oidc_jwks_url="https://identity.example.test/jwks.json",
        oidc_frontend_url="https://app.example.test",
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)

    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
        cookies={"tricycle_oidc_state": "one-time-state"},
    ) as client:
        response = await client.get(f"/api/auth/callback?{callback_query}")

    assert response.status_code == 303
    assert response.headers["location"].startswith("https://app.example.test/login?error=")
    state_cookie = next(
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith("tricycle_oidc_state=")
    )
    assert "Max-Age=0" in state_cookie


@pytest.mark.asyncio
async def test_oidc_logout_redirect_revokes_cookie_and_returns_to_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        auth_mode="oidc",
        oidc_issuer="https://identity.example.test",
        oidc_audience="reaction-database",
        oidc_jwks_url="https://identity.example.test/jwks.json",
        oidc_frontend_url="https://app.example.test",
    )
    revoked: list[str | None] = []

    async def revoke(raw_token: str | None) -> None:
        revoked.append(raw_token)

    async def logout_url(
        *,
        post_logout_redirect_uri: str,
        id_token_hint: str | None = None,
    ) -> str:
        assert post_logout_redirect_uri == "https://app.example.test/account"
        assert id_token_hint is None
        return "https://identity.example.test/logout?client_id=reaction-client"

    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(AuthenticationService, "revoke_session", staticmethod(revoke))
    monkeypatch.setattr(AuthenticationService, "oidc_logout_url", staticmethod(logout_url))
    csrf_token = AuthenticationService.csrf_token("session-token")

    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
        cookies={
            settings.session_cookie_name: "session-token",
            settings.csrf_cookie_name: csrf_token,
        },
    ) as client:
        response = await client.get(
            f"/api/auth/logout?return_to=%2Faccount&csrf_token={csrf_token}"
        )

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == "https://identity.example.test/logout?client_id=reaction-client"
    )
    assert revoked == ["session-token"]
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_session_state_change_requires_bound_double_submit_csrf_token() -> None:
    settings = Settings(_env_file=None)
    raw_session = "opaque-session"
    token = AuthenticationService.csrf_token(raw_session)

    missing = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/sessions/revoke-all",
            "headers": [],
        }
    )
    valid = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/sessions/revoke-all",
            "headers": [
                (b"cookie", f"{settings.csrf_cookie_name}={token}".encode()),
                (settings.csrf_header_name.encode(), token.encode()),
            ],
        }
    )

    assert "CSRF token is required" in (
        api_authentication._csrf_failure(missing, raw_session) or ""
    )
    assert api_authentication._csrf_failure(valid, raw_session) is None
    assert api_authentication._csrf_failure(valid, None) is None


@pytest.mark.asyncio
async def test_anonymous_user_can_preview_and_download_public_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def preview(artifact_id: UUID, **_: object) -> ArtifactPreview:
        assert artifact_id == PUBLIC_ARTIFACT_ID
        return ArtifactPreview(
            id=artifact_id,
            original_filename="public.log",
            media_type="text/plain",
            size_bytes=16,
            content_sha256="a" * 64,
            preview_text="Gaussian output\n",
            preview_bytes=16,
            truncated=False,
        )

    async def download(artifact_id: UUID, **_: object) -> ArtifactDownload:
        assert artifact_id == PUBLIC_ARTIFACT_ID
        return ArtifactDownload(
            id=artifact_id,
            original_filename="public.log",
            media_type="text/plain",
            size_bytes=16,
            content_sha256="a" * 64,
            bucket="public-artifacts",
            object_key="raw/public.log",
            version_id="version-1",
        )

    monkeypatch.setattr(AuthenticationService, "authenticate", _reject_authentication)
    monkeypatch.setattr(AuthenticationService, "authenticate_optional", _optional_anonymous)
    monkeypatch.setattr(ArtifactContentService, "preview", staticmethod(preview))
    monkeypatch.setattr(ArtifactContentService, "download", staticmethod(download))
    monkeypatch.setattr(core, "iter_artifact_download", lambda _: iter([b"Gaussian output\n"]))

    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        preview_response = await client.get(f"/api/artifacts/{PUBLIC_ARTIFACT_ID}/preview")
        download_response = await client.get(f"/api/artifacts/{PUBLIC_ARTIFACT_ID}/download")

    assert preview_response.status_code == 200
    assert preview_response.json()["preview_text"] == "Gaussian output\n"
    assert download_response.status_code == 200
    assert download_response.content == b"Gaussian output\n"
    assert download_response.headers["content-disposition"].startswith(
        'attachment; filename="public.log"'
    )


@pytest.mark.asyncio
async def test_anonymous_project_artifact_uses_not_found_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(*_: object, **__: object) -> ArtifactPreview:
        raise ArtifactForbiddenError("authentication is required for project artifacts")

    monkeypatch.setattr(AuthenticationService, "authenticate", _reject_authentication)
    monkeypatch.setattr(AuthenticationService, "authenticate_optional", _optional_anonymous)
    monkeypatch.setattr(ArtifactContentService, "preview", staticmethod(forbidden))

    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/api/artifacts/{PROJECT_ARTIFACT_ID}/preview")

    assert response.status_code == 404
    assert response.json() == {"detail": "authentication is required for project artifacts"}


@pytest.mark.asyncio
async def test_protected_routes_and_invalid_tokens_still_require_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(AuthenticationService, "authenticate", _reject_authentication)
    monkeypatch.setattr(AuthenticationService, "authenticate_optional", _optional_anonymous)

    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        protected = await client.get("/api/logical-reactions")
        invalid_public = await client.get(
            f"/api/artifacts/{PUBLIC_ARTIFACT_ID}/preview",
            headers={"Authorization": "Bearer invalid"},
        )

    assert protected.status_code == 401
    assert invalid_public.status_code == 401
    assert protected.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_authenticated_artifact_upload_uses_unified_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = UUID("00000000-0000-7000-8000-000000000201")
    artifact_id = UUID("00000000-0000-7000-8000-000000000603")

    async def upload(**values: object) -> ArtifactUploadResult:
        assert values["payload"] == b"supporting data\n"
        assert values["filename"] == "notes.txt"
        assert values["project_id"] == project_id
        assert values["user_id"] == DEVELOPMENT_USER_ID
        assert values["artifact_kind"] is ArtifactKind.AUXILIARY
        return ArtifactUploadResult(
            artifact_id=artifact_id,
            artifact_kind=ArtifactKind.AUXILIARY,
            storage_status=StorageStatus.AVAILABLE,
            inferred_reaction_count=0,
            inferences=[],
        )

    monkeypatch.setattr(ArtifactUploadService, "upload", staticmethod(upload))
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/artifacts",
            data={"project_id": str(project_id), "artifact_kind": "auxiliary"},
            files={"file": ("notes.txt", b"supporting data\n", "text/plain")},
        )

    assert response.status_code == 200
    assert response.json() == {
        "artifact_id": str(artifact_id),
        "artifact_kind": "auxiliary",
        "storage_status": "available",
        "ingestion_id": None,
        "parse_revision_id": None,
        "parse_revision_created": None,
        "ingestion_status": None,
        "source_frame_count": None,
        "transition_state_frame_count": None,
        "inferred_reaction_count": 0,
        "inferences": [],
    }


@pytest.mark.asyncio
async def test_authenticated_artifact_uploads_are_processed_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = UUID("00000000-0000-7000-8000-000000000201")
    artifact_id = UUID("00000000-0000-7000-8000-000000000603")
    release = asyncio.Event()
    active = 0
    peak = 0
    started = 0

    async def upload(**_: object) -> ArtifactUploadResult:
        nonlocal active, peak, started
        active += 1
        started += 1
        peak = max(peak, active)
        if started == 3:
            release.set()
        try:
            await asyncio.wait_for(release.wait(), timeout=1)
            return ArtifactUploadResult(
                artifact_id=artifact_id,
                artifact_kind=ArtifactKind.AUXILIARY,
                storage_status=StorageStatus.AVAILABLE,
                inferred_reaction_count=0,
                inferences=[],
            )
        finally:
            active -= 1

    monkeypatch.setattr(ArtifactUploadService, "upload", staticmethod(upload))
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/artifacts",
                    data={"project_id": str(project_id), "artifact_kind": "auxiliary"},
                    files={"file": (f"parallel-{index}.txt", b"data", "text/plain")},
                )
                for index in range(3)
            )
        )

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert peak == 3


@pytest.mark.asyncio
async def test_authenticated_batch_upload_preserves_each_raw_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = UUID("00000000-0000-7000-8000-000000000201")

    async def upload_batch(**values: object) -> ArtifactBatchUploadResult:
        files = values["files"]
        assert isinstance(files, list)
        assert [item.filename for item in files] == ["first.log", "second.orcaout"]
        assert [item.payload for item in files] == [None, None]
        spool_paths = [item.spool_path for item in files]
        assert all(path is not None for path in spool_paths)
        assert [path.read_bytes() for path in spool_paths if path is not None] == [
            b"gaussian\n",
            b"orca\n",
        ]
        return ArtifactBatchUploadResult(
            total_count=2,
            succeeded_count=1,
            failed_count=1,
            source_frame_count=3,
            transition_state_frame_count=1,
            inferred_reaction_count=1,
            items=[
                ArtifactBatchUploadItem(filename="first.log", succeeded=True),
                ArtifactBatchUploadItem(
                    filename="second.orcaout",
                    succeeded=False,
                    error_code="molop_parse_failed",
                    error_message="invalid output",
                ),
            ],
        )

    monkeypatch.setattr(ArtifactUploadService, "upload_batch", staticmethod(upload_batch))
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/artifacts/batch",
            data={"project_id": str(project_id)},
            files=[
                ("files", ("first.log", b"gaussian\n", "text/plain")),
                ("files", ("second.orcaout", b"orca\n", "text/plain")),
            ],
        )

    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == 2
    assert body["succeeded_count"] == body["inferred_reaction_count"] == 1
    assert body["failed_count"] == 1
    assert body["items"][1]["error_code"] == "molop_parse_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("files", "max_batch_files", "max_batch_bytes", "expected_detail"),
    [
        (
            [
                ("files", ("one.log", b"a", "text/plain")),
                ("files", ("two.log", b"b", "text/plain")),
            ],
            1,
            2048,
            "file limit",
        ),
        (
            [
                ("files", ("one.log", b"a" * 700, "text/plain")),
                ("files", ("two.log", b"b" * 700, "text/plain")),
            ],
            4,
            1024,
            "byte limit",
        ),
        (
            [("files", ("one.log", b"a" * 1025, "text/plain"))],
            4,
            2048,
            "byte limit",
        ),
    ],
)
async def test_authenticated_batch_upload_rejects_resource_limits(
    monkeypatch: pytest.MonkeyPatch,
    files: list[tuple[str, tuple[str, bytes, str]]],
    max_batch_files: int,
    max_batch_bytes: int,
    expected_detail: str,
) -> None:
    settings = Settings(
        _env_file=None,
        max_upload_bytes=1024,
        max_batch_files=max_batch_files,
        max_batch_bytes=max_batch_bytes,
    )
    monkeypatch.setattr(upload_routes, "get_settings", lambda: settings)

    async def fail_if_called(**_: object) -> ArtifactBatchUploadResult:
        raise AssertionError("resource-rejected batch reached the upload service")

    monkeypatch.setattr(ArtifactUploadService, "upload_batch", staticmethod(fail_if_called))
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/artifacts/batch",
            data={"project_id": str(UUID("00000000-0000-7000-8000-000000000201"))},
            files=files,
        )

    assert response.status_code == 413
    assert expected_detail in response.json()["detail"]
    assert response.headers["X-Upload-Rejection-Stage"] == "preflight"


@pytest.mark.asyncio
async def test_batch_route_preserves_service_level_limit_as_http_413(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_after_route_preflight(**_: object) -> ArtifactBatchUploadResult:
        raise ArtifactUploadLimitError("upload batch exceeds the service byte limit")

    monkeypatch.setattr(ArtifactUploadService, "upload_batch", reject_after_route_preflight)
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/artifacts/batch",
            data={"project_id": "00000000-0000-7000-8000-000000000201"},
            files=[("files", ("one.log", b"a", "text/plain"))],
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "upload batch exceeds the service byte limit"}


@pytest.mark.asyncio
async def test_authenticated_validate_does_not_require_an_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = UUID("00000000-0000-7000-8000-000000000201")

    async def validate(**values: object) -> ArtifactValidationResult:
        assert values["payload"] == b"orca output\n"
        assert values["filename"] == "calculation.orcaout"
        assert values["project_id"] == project_id
        assert values["user_id"] == DEVELOPMENT_USER_ID
        return ArtifactValidationResult(
            filename="calculation.orcaout",
            source_format="orcaout",
            source_frame_count=1,
            transition_state_frame_count=0,
            successful_inference_count=0,
            failed_inference_count=0,
            inferences=[],
        )

    monkeypatch.setattr(ArtifactUploadService, "validate", staticmethod(validate))
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/artifacts/validate",
            data={"project_id": str(project_id)},
            files={"file": ("calculation.orcaout", b"orca output\n", "text/plain")},
        )

    assert response.status_code == 200
    assert response.json()["source_format"] == "orcaout"
    assert response.json()["source_frame_count"] == 1


@pytest.mark.asyncio
async def test_authenticated_reparse_uses_stored_artifact_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id = UUID("00000000-0000-7000-8000-000000000603")
    revision_id = UUID("00000000-0000-7000-8000-000000000604")

    async def reparse(**values: object) -> ArtifactUploadResult:
        assert values == {
            "artifact_id": artifact_id,
            "user_id": DEVELOPMENT_USER_ID,
        }
        return ArtifactUploadResult(
            artifact_id=artifact_id,
            artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
            storage_status=StorageStatus.AVAILABLE,
            parse_revision_id=revision_id,
            parse_revision_created=False,
            inferred_reaction_count=0,
            inferences=[],
        )

    monkeypatch.setattr(ArtifactUploadService, "reparse", staticmethod(reparse))
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(f"/api/artifacts/{artifact_id}/reparse")

    assert response.status_code == 200
    assert response.json()["parse_revision_id"] == str(revision_id)
    assert response.json()["parse_revision_created"] is False
