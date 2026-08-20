"""Shared query budgets used by HTTP, GraphQL, MCP, and database services."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from math import ceil
from threading import Lock
from time import monotonic
from typing import Any

from graphql import GraphQLError, parse
from graphql.language import ast


class QueryBudgetExceeded(ValueError):
    """A query was rejected before execution because it exceeded a stable budget."""

    code = "query_budget_exceeded"

    def __init__(self, message: str) -> None:
        super().__init__(f"[{self.code}] {message}")
        self.message = message


class QueryRateLimitExceeded(QueryBudgetExceeded):
    """A principal or client exhausted its fixed-window request budget."""

    code = "query_rate_limit_exceeded"

    def __init__(self, *, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("query request rate limit exceeded")


class QueryStatementTimeout(RuntimeError):
    """PostgreSQL canceled a statement after the configured execution deadline."""

    code = "query_timeout"

    def __init__(self) -> None:
        super().__init__(f"[{self.code}] database statement timeout exceeded")
        self.message = "database statement timeout exceeded"


def query_error_payload(error: QueryBudgetExceeded | QueryStatementTimeout) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "code": error.code,
        "message": error.message,
    }
    if isinstance(error, QueryRateLimitExceeded):
        detail["retry_after_seconds"] = error.retry_after_seconds
    return detail


def graphql_error_result(error: QueryBudgetExceeded | QueryStatementTimeout) -> dict[str, Any]:
    return {
        "data": None,
        "errors": [
            {
                "message": error.message,
                "extensions": {"code": error.code},
            }
        ],
    }


def normalize_graphql_query_errors(result: dict[str, Any]) -> dict[str, Any]:
    """Add stable codes when a shared query exception crossed NexusX's error envelope."""

    errors = result.get("errors")
    if not isinstance(errors, list):
        return result
    for item in errors:
        if not isinstance(item, dict):
            continue
        message = item.get("message")
        if not isinstance(message, str):
            continue
        for code in (QueryBudgetExceeded.code, QueryStatementTimeout.code):
            marker = f"[{code}]"
            if marker in message:
                extensions = item.setdefault("extensions", {})
                if isinstance(extensions, dict):
                    extensions["code"] = code
                item["message"] = message.split(marker, 1)[1].strip()
                break
    return result


def enforce_structure_input_budget(
    values: dict[str, str | None],
    *,
    maximum_characters: int,
) -> None:
    for name, value in values.items():
        if value is not None and len(value) > maximum_characters:
            raise QueryBudgetExceeded(
                f"{name} exceeds the {maximum_characters}-character structure input limit"
            )


def _integer_argument(field: ast.FieldNode, name: str) -> int | None:
    for argument in field.arguments:
        if argument.name.value == name and isinstance(argument.value, ast.IntValueNode):
            return int(argument.value.value)
    return None


def _query_metrics(document: ast.DocumentNode) -> tuple[int, int]:
    fragments = {
        definition.name.value: definition
        for definition in document.definitions
        if isinstance(definition, ast.FragmentDefinitionNode)
    }

    def walk(
        selection_set: ast.SelectionSetNode,
        *,
        depth: int,
        active_fragments: frozenset[str],
    ) -> tuple[int, int]:
        maximum_depth = depth
        complexity = 0
        for selection in selection_set.selections:
            if isinstance(selection, ast.FieldNode):
                complexity += 1
                limit = _integer_argument(selection, "limit")
                if limit is not None and limit > 0:
                    complexity += ceil(limit / 25)
                if selection.selection_set is not None:
                    child_depth, child_complexity = walk(
                        selection.selection_set,
                        depth=depth + 1,
                        active_fragments=active_fragments,
                    )
                    maximum_depth = max(maximum_depth, child_depth)
                    complexity += child_complexity
            elif isinstance(selection, ast.InlineFragmentNode):
                child_depth, child_complexity = walk(
                    selection.selection_set,
                    depth=depth,
                    active_fragments=active_fragments,
                )
                maximum_depth = max(maximum_depth, child_depth)
                complexity += child_complexity
            elif isinstance(selection, ast.FragmentSpreadNode):
                fragment_name = selection.name.value
                if fragment_name in active_fragments:
                    continue
                fragment = fragments.get(fragment_name)
                if fragment is not None:
                    child_depth, child_complexity = walk(
                        fragment.selection_set,
                        depth=depth,
                        active_fragments=active_fragments | {fragment_name},
                    )
                    maximum_depth = max(maximum_depth, child_depth)
                    complexity += child_complexity
        return maximum_depth, complexity

    maximum_depth = 0
    complexity = 0
    for definition in document.definitions:
        if isinstance(definition, ast.OperationDefinitionNode):
            operation_depth, operation_complexity = walk(
                definition.selection_set,
                depth=1,
                active_fragments=frozenset(),
            )
            maximum_depth = max(maximum_depth, operation_depth)
            complexity += operation_complexity
    return maximum_depth, complexity


def validate_graphql_query_budget(
    query: str,
    *,
    maximum_characters: int,
    maximum_tokens: int,
    maximum_depth: int,
    maximum_complexity: int,
) -> None:
    if len(query) > maximum_characters:
        raise QueryBudgetExceeded(
            f"GraphQL document exceeds the {maximum_characters}-character limit"
        )
    try:
        document = parse(query, max_tokens=maximum_tokens)
    except GraphQLError as error:
        if "more than" in str(error) and "tokens" in str(error):
            raise QueryBudgetExceeded(
                f"GraphQL document exceeds the {maximum_tokens}-token limit"
            ) from error
        return
    depth, complexity = _query_metrics(document)
    if depth > maximum_depth:
        raise QueryBudgetExceeded(
            f"GraphQL depth {depth} exceeds the maximum depth {maximum_depth}"
        )
    if complexity > maximum_complexity:
        raise QueryBudgetExceeded(
            f"GraphQL complexity {complexity} exceeds the maximum complexity {maximum_complexity}"
        )


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class FixedWindowRateLimiter:
    """Bounded in-process limiter for one API worker."""

    def __init__(
        self,
        *,
        maximum_requests: int,
        window_seconds: int,
        maximum_keys: int = 10_000,
    ) -> None:
        self.maximum_requests = maximum_requests
        self.window_seconds = window_seconds
        self.maximum_keys = maximum_keys
        self._windows: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._lock = Lock()

    def check(self, key: str, *, now: float | None = None) -> RateLimitDecision:
        checked_at = monotonic() if now is None else now
        with self._lock:
            start, count = self._windows.pop(key, (checked_at, 0))
            if checked_at - start >= self.window_seconds:
                start, count = checked_at, 0
            if count >= self.maximum_requests:
                self._windows[key] = (start, count)
                retry_after = max(1, ceil(self.window_seconds - (checked_at - start)))
                return RateLimitDecision(False, 0, retry_after)
            count += 1
            self._windows[key] = (start, count)
            while len(self._windows) > self.maximum_keys:
                self._windows.popitem(last=False)
            return RateLimitDecision(True, self.maximum_requests - count, 0)


__all__ = [
    "FixedWindowRateLimiter",
    "QueryBudgetExceeded",
    "QueryRateLimitExceeded",
    "QueryStatementTimeout",
    "enforce_structure_input_budget",
    "graphql_error_result",
    "normalize_graphql_query_errors",
    "query_error_payload",
    "validate_graphql_query_budget",
]
