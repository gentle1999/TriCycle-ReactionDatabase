"""Low-cardinality Prometheus metrics shared by API transports and services."""

from prometheus_client import Counter, Gauge

DATABASE_POOL_CONNECTIONS = Gauge(
    "tricycle_database_pool_connections",
    "SQLAlchemy connection pool state for this API worker.",
    ("state",),
)
ARTIFACT_STORAGE_ROWS = Gauge(
    "tricycle_artifact_storage_rows",
    "Artifact rows by durable storage status.",
    ("status",),
)
ARTIFACT_INGESTION_ROWS = Gauge(
    "tricycle_artifact_ingestion_rows",
    "Artifact ingestion rows by durable workflow status.",
    ("status",),
)
METRICS_COLLECTION_FAILURES = Counter(
    "tricycle_metrics_collection_failures_total",
    "Failures while refreshing runtime metrics.",
    ("component",),
)
STATEMENT_TIMEOUTS = Counter(
    "tricycle_database_statement_timeouts_total",
    "PostgreSQL statements cancelled by statement_timeout.",
)
UPLOAD_OPERATIONS = Counter(
    "tricycle_upload_operations_total",
    "Artifact upload HTTP operations by outcome.",
    ("outcome",),
)
STORAGE_FAILURES = Counter(
    "tricycle_storage_failures_total",
    "RustFS/S3 object failures detected by the application.",
    ("reason",),
)
OIDC_CALLBACKS = Counter(
    "tricycle_oidc_callbacks_total",
    "OIDC callbacks by outcome.",
    ("outcome",),
)
SMTP_DELIVERIES = Counter(
    "tricycle_smtp_deliveries_total",
    "Project invitation delivery attempts by outcome.",
    ("outcome",),
)
MCP_ACTIVE_CONNECTIONS = Gauge(
    "tricycle_mcp_active_connections",
    "MCP Streamable HTTP requests currently active in this worker.",
)
RATE_LIMIT_DECISIONS = Counter(
    "tricycle_rate_limit_decisions_total",
    "Rate-limit decisions by policy and outcome.",
    ("policy", "outcome"),
)


__all__ = [
    "ARTIFACT_INGESTION_ROWS",
    "ARTIFACT_STORAGE_ROWS",
    "DATABASE_POOL_CONNECTIONS",
    "MCP_ACTIVE_CONNECTIONS",
    "METRICS_COLLECTION_FAILURES",
    "OIDC_CALLBACKS",
    "RATE_LIMIT_DECISIONS",
    "SMTP_DELIVERIES",
    "STATEMENT_TIMEOUTS",
    "STORAGE_FAILURES",
    "UPLOAD_OPERATIONS",
]
