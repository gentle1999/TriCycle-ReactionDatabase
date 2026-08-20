import os
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlmodel import col

from tricycle_reaction_db.application.dtos import UserStatusUpdate
from tricycle_reaction_db.application.services import (
    AuthenticatedPrincipal,
    UserManagementConflictError,
    UserManagementNotFoundError,
    UserManagementService,
)
from tricycle_reaction_db.db.models import UserAccount
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import UserStatus
from tricycle_reaction_db.domain.identity import (
    DEVELOPMENT_IDENTITY_ISSUER,
    DEVELOPMENT_IDENTITY_SUBJECT,
    DEVELOPMENT_USER_ID,
    SYSTEM_PROJECT_ID,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


def _principal(user_id) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=user_id,
        display_name="User Administrator",
        primary_email=None,
        is_service_account=False,
        issuer=DEVELOPMENT_IDENTITY_ISSUER,
        subject=DEVELOPMENT_IDENTITY_SUBJECT,
    )


@pytest.mark.asyncio
async def test_user_directory_and_status_management() -> None:
    suffix = uuid4().hex
    target_id = uuid4()
    outsider_id = uuid4()
    admin = _principal(DEVELOPMENT_USER_ID)
    outsider = _principal(outsider_id)

    try:
        async with session_factory() as session:
            session.add_all(
                [
                    UserAccount(
                        id=target_id,
                        display_name=f"Managed User {suffix}",
                        primary_email=f"managed-{suffix}@example.test",
                    ),
                    UserAccount(
                        id=outsider_id,
                        display_name=f"Non Admin {suffix}",
                        primary_email=f"outsider-{suffix}@example.test",
                    ),
                ]
            )
            await session.commit()

        page = await UserManagementService.list_users(admin, query=suffix, limit=10)
        assert target_id in {user.id for user in page.items}
        assert outsider_id in {user.id for user in page.items}

        with pytest.raises(UserManagementNotFoundError, match="user not found"):
            await UserManagementService.list_users(outsider, query=suffix)
        with pytest.raises(UserManagementConflictError, match="own account"):
            await UserManagementService.update_status(
                DEVELOPMENT_USER_ID,
                UserStatusUpdate(status=UserStatus.SUSPENDED),
                admin,
            )

        suspended = await UserManagementService.update_status(
            target_id,
            UserStatusUpdate(status=UserStatus.SUSPENDED),
            admin,
        )
        assert suspended.status is UserStatus.SUSPENDED
        project_users = await UserManagementService.list_users(
            admin,
            query=suffix,
            project_id=SYSTEM_PROJECT_ID,
        )
        assert target_id not in {user.id for user in project_users.items}

        restored = await UserManagementService.update_status(
            target_id,
            UserStatusUpdate(status=UserStatus.ACTIVE),
            admin,
        )
        assert restored.status is UserStatus.ACTIVE
        project_users = await UserManagementService.list_users(
            admin,
            query=suffix,
            project_id=SYSTEM_PROJECT_ID,
        )
        target = next(user for user in project_users.items if user.id == target_id)
        assert target.project_role is None
        detail = await UserManagementService.get_user(target_id, admin)
        assert detail.primary_email == f"managed-{suffix}@example.test"
    finally:
        async with session_factory() as session:
            await session.exec(
                delete(UserAccount).where(col(UserAccount.id).in_({target_id, outsider_id}))
            )
            await session.commit()
