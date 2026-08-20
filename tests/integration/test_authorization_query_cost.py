import json
import os
from collections.abc import AsyncIterator, Callable
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, exists, text
from sqlalchemy.dialects import postgresql
from sqlmodel import col, select

from tricycle_reaction_db.application.services.authorization import (
    AuthorizationService,
    ProjectAccessDeniedError,
    ProjectPermission,
)
from tricycle_reaction_db.db.models import (
    Organization,
    Project,
    ProjectMembership,
    UserAccount,
)
from tricycle_reaction_db.db.session import engine, session_factory
from tricycle_reaction_db.domain.enums import OrganizationStatus, ProjectRole, ProjectStatus

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


@pytest_asyncio.fixture
async def authorization_cost_sample() -> AsyncIterator[tuple[UUID, UUID, UUID]]:
    suffix = uuid4().hex
    user_id = uuid4()
    organization_id = uuid4()
    project_ids = [uuid4() for _ in range(97)]
    accessible_project_id = project_ids[0]
    async with session_factory() as session:
        session.add(
            UserAccount(
                id=user_id,
                display_name="Authorization query cost",
                primary_email=f"authorization-cost-{suffix}@example.test",
            )
        )
        session.add(
            Organization(
                id=organization_id,
                slug=f"authorization-cost-{suffix}",
                name="Authorization query cost",
            )
        )
        session.add_all(
            [
                Project(
                    id=project_id,
                    organization_id=organization_id,
                    slug=f"project-{index:03d}",
                    name=f"Project {index:03d}",
                )
                for index, project_id in enumerate(project_ids)
            ]
        )
        session.add(
            ProjectMembership(
                project_id=accessible_project_id,
                user_id=user_id,
                role=ProjectRole.VIEWER,
            )
        )
        await session.commit()

    try:
        yield user_id, accessible_project_id, project_ids[-1]
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(ProjectMembership).where(col(ProjectMembership.user_id) == user_id)
            )
            await session.execute(
                delete(Project).where(col(Project.organization_id) == organization_id)
            )
            await session.execute(
                delete(Organization).where(col(Organization.id) == organization_id)
            )
            await session.execute(delete(UserAccount).where(col(UserAccount.id) == user_id))
            await session.commit()


def _capture_statements() -> tuple[
    list[str],
    Callable[[object, object, str, object, object, bool], None],
]:
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    return statements, capture


def _plan_nodes(value: object) -> list[dict[str, object]]:
    if isinstance(value, list):
        return [node for item in value for node in _plan_nodes(item)]
    if not isinstance(value, dict):
        return []
    nodes = [value] if "Node Type" in value else []
    return nodes + [node for child in value.values() for node in _plan_nodes(child)]


@pytest.mark.asyncio
async def test_single_project_permission_is_one_select_exists_with_many_unrelated_projects(
    authorization_cost_sample: tuple[UUID, UUID, UUID],
) -> None:
    user_id, accessible_project_id, unrelated_project_id = authorization_cost_sample
    statements, capture = _capture_statements()
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        await AuthorizationService.require_project_permission(
            user_id,
            accessible_project_id,
            ProjectPermission.ARTIFACT_READ,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("SELECT EXISTS")
    assert " LIMIT " not in statements[0].upper()

    statements.clear()
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(ProjectAccessDeniedError):
            await AuthorizationService.require_project_permission(
                user_id,
                unrelated_project_id,
                ProjectPermission.ARTIFACT_READ,
            )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("SELECT EXISTS")


@pytest.mark.asyncio
async def test_project_access_list_is_one_database_filtered_query(
    authorization_cost_sample: tuple[UUID, UUID, UUID],
) -> None:
    user_id, accessible_project_id, _ = authorization_cost_sample
    statements, capture = _capture_statements()
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        accesses = await AuthorizationService.project_accesses(user_id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert len(statements) == 1
    assert [access.project.id for access in accesses] == [accessible_project_id]


@pytest.mark.asyncio
async def test_single_project_authorization_plan_uses_identity_indexes(
    authorization_cost_sample: tuple[UUID, UUID, UUID],
) -> None:
    user_id, accessible_project_id, _ = authorization_cost_sample
    accessible_project = (
        select(1)
        .select_from(Project)
        .join(Organization, col(Project.organization_id) == col(Organization.id))
        .where(
            col(Project.id) == accessible_project_id,
            col(Project.status) == ProjectStatus.ACTIVE,
            col(Organization.status) == OrganizationStatus.ACTIVE,
            AuthorizationService.project_permission_predicate(
                user_id,
                col(Project.id),
                ProjectPermission.ARTIFACT_READ,
            ),
        )
    )
    statement = select(exists(accessible_project))
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    async with session_factory() as session:
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = (
            await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {compiled}"))
        ).scalar_one()

    nodes = _plan_nodes(plan)
    project_nodes = [node for node in nodes if node.get("Relation Name") == "project"]
    membership_nodes = [node for node in nodes if node.get("Relation Name") == "project_membership"]
    assert project_nodes, json.dumps(plan, sort_keys=True)
    assert membership_nodes, json.dumps(plan, sort_keys=True)
    assert all(node.get("Node Type") != "Seq Scan" for node in project_nodes)
    assert all(node.get("Node Type") != "Seq Scan" for node in membership_nodes)
    membership_index_names = {
        node.get("Index Name") for node in nodes if node.get("Index Name") is not None
    }
    assert membership_index_names & {
        "ix_project_membership_user_id",
        "uq_project_membership_project_user",
    }, json.dumps(plan, sort_keys=True)
