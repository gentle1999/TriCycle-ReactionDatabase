.PHONY: init frontend-install frontend-build frontend-check frontend-test-e2e serve-frontend dev dev-host dev-stack \
	dev-infra-up dev-migrate dev-bootstrap \
	format lint type test test-db test-storage test-redis test-infra audit vendor-audit check \
	db-up db-down storage-up storage-down auth-up auth-down infra-up infra-down migrate import-artifacts \
	backfill-thermodynamics reconcile-reaction-geometries bootstrap-development bootstrap-production seed-da-bench serve serve-nexusx storage-gc auth-session-cleanup \
	benchmark-upload-resources benchmark-remote-upload-resources capture-query-plan-evidence probe-shared-rate-limit probe-upload-limit \
	validate-da-bench-fixture validate-restore deployment-smoke validate-deployment-acceptance \
	stack-build stack-up stack-down stack-logs

DEV_DATABASE_URL := postgresql+psycopg://example_user:example-local-password@127.0.0.1:5432/example_reaction_db
DEV_RUSTFS_ENDPOINT := http://127.0.0.1:19000
DEV_RUNTIME_ENV = \
	OMP_NUM_THREADS=1 \
	OPENBLAS_NUM_THREADS=1 \
	MKL_NUM_THREADS=1 \
	TRICYCLE_DATABASE_URL="$(DEV_DATABASE_URL)" \
	TRICYCLE_RUSTFS_ENDPOINT_URL="$(DEV_RUSTFS_ENDPOINT)" \
	NO_PROXY="127.0.0.1,localhost,postgres,rustfs,host.docker.internal" \
	no_proxy="127.0.0.1,localhost,postgres,rustfs,host.docker.internal" \
	TRICYCLE_RUSTFS_ACCESS_KEY=example-local-access \
	TRICYCLE_RUSTFS_SECRET_KEY=example-local-secret \
	TRICYCLE_RUSTFS_BUCKET=example-reaction-raw-files \
	TRICYCLE_RUSTFS_REGION=us-east-1 \
	TRICYCLE_RUSTFS_VERIFY_TLS=true \
	TRICYCLE_ENVIRONMENT=development \
	TRICYCLE_AUTH_MODE=development \
	TRICYCLE_API_HOST=0.0.0.0 \
	TRICYCLE_API_PORT=8000

# Artifact imports default to the caller's configured runtime. Use the local
# development stack explicitly with IMPORT_MODE=development.
IMPORT_MODE ?= deployment
ifeq ($(IMPORT_MODE),development)
IMPORT_RUNTIME_ENV := $(DEV_RUNTIME_ENV)
else ifeq ($(IMPORT_MODE),deployment)
IMPORT_RUNTIME_ENV := \
	OMP_NUM_THREADS=1 \
	OPENBLAS_NUM_THREADS=1 \
	MKL_NUM_THREADS=1
else
$(error IMPORT_MODE must be either development or deployment)
endif

init:
	uv sync --python 3.12
	npm --prefix frontend ci

frontend-install:
	npm --prefix frontend ci

frontend-build:
	npm --prefix frontend run build

frontend-check:
	npm --prefix frontend run check

frontend-test-e2e:
	npm --prefix frontend run test:e2e

serve-frontend:
	npm --prefix frontend run dev

# Start the host-based development stack. Uvicorn reloads Python changes and
# Vite provides frontend HMR; Ctrl-C stops only the two foreground processes.
dev:
	@set -u; \
	$(DEV_RUNTIME_ENV) TRICYCLE_DEBUG=true uv run tricycle-api & api_pid=$$!; \
	npm --prefix frontend run dev & frontend_pid=$$!; \
	cleanup() { \
		kill "$$api_pid" "$$frontend_pid" 2>/dev/null || true; \
		wait "$$api_pid" 2>/dev/null || true; \
		wait "$$frontend_pid" 2>/dev/null || true; \
	}; \
	trap 'cleanup; exit 0' INT TERM; \
	while :; do \
		if ! kill -0 "$$api_pid" 2>/dev/null; then \
			wait "$$api_pid"; status=$$?; cleanup; exit "$$status"; \
		fi; \
		if ! kill -0 "$$frontend_pid" 2>/dev/null; then \
			wait "$$frontend_pid"; status=$$?; cleanup; exit "$$status"; \
		fi; \
		sleep 1; \
	done

dev-host: dev

# Start the previous container-backed development stack when Compose-managed
# infrastructure is desired.
dev-stack: dev-infra-up dev-migrate dev-bootstrap
	@set -u; \
	$(DEV_RUNTIME_ENV) TRICYCLE_DEBUG=true uv run tricycle-api & api_pid=$$!; \
	npm --prefix frontend run dev & frontend_pid=$$!; \
	cleanup() { \
		kill "$$api_pid" "$$frontend_pid" 2>/dev/null || true; \
		wait "$$api_pid" 2>/dev/null || true; \
		wait "$$frontend_pid" 2>/dev/null || true; \
	}; \
	trap 'cleanup; exit 0' INT TERM; \
	while :; do \
		if ! kill -0 "$$api_pid" 2>/dev/null; then \
			wait "$$api_pid"; status=$$?; cleanup; exit "$$status"; \
		fi; \
		if ! kill -0 "$$frontend_pid" 2>/dev/null; then \
			wait "$$frontend_pid"; status=$$?; cleanup; exit "$$status"; \
		fi; \
		sleep 1; \
	done

dev-infra-up:
	KEYCLOAK_PORT=18080 docker compose --env-file .env.example -f compose.yaml \
		--project-name reaction-database-development up -d --wait postgres rustfs keycloak

dev-migrate:
	$(DEV_RUNTIME_ENV) uv run alembic upgrade head

dev-bootstrap:
	$(DEV_RUNTIME_ENV) uv run tricycle-bootstrap --mode development

format:
	uv run ruff format src tests migrations scripts

lint:
	uv run ruff check src tests migrations scripts
	uv run ruff format --check src tests migrations scripts

type:
	uv run mypy src scripts
	uv run pyright src scripts

test:
	uv run pytest

test-db:
	TRICYCLE_RUN_DATABASE_TESTS=1 uv run pytest -m integration

test-storage:
	TRICYCLE_RUN_RUSTFS_TESTS=1 uv run pytest -m rustfs

test-redis:
	TRICYCLE_RUN_REDIS_TESTS=1 uv run pytest -m redis

test-infra:
	TRICYCLE_RUN_DATABASE_TESTS=1 TRICYCLE_RUN_RUSTFS_TESTS=1 \
		uv run pytest -m integration

vendor-audit:
	uv run --frozen python scripts/audit_vendored_assets.py

audit: vendor-audit
	uv export --frozen --no-dev --no-emit-project --no-hashes \
		--format requirements-txt --output-file /tmp/tricycle-runtime-requirements.txt
	uvx --from pip-audit==2.10.1 pip-audit --disable-pip --no-deps \
		-r /tmp/tricycle-runtime-requirements.txt
	npm --prefix frontend audit --audit-level=high --registry=https://registry.npmjs.org

check: frontend-check lint type test

db-up:
	docker compose up -d --wait postgres

db-down:
	docker compose stop postgres

storage-up:
	docker compose up -d --wait rustfs

storage-down:
	docker compose stop rustfs

auth-up:
	docker compose up -d --wait keycloak

auth-down:
	docker compose stop keycloak

infra-up:
	docker compose up -d --wait postgres rustfs keycloak

infra-down:
	docker compose down

stack-build:
	docker compose build api frontend caddy

stack-up:
	docker compose up -d --build --wait

stack-down:
	docker compose down

stack-logs:
	docker compose logs --follow api frontend caddy

migrate:
	uv run alembic upgrade head

bootstrap-development:
	uv run tricycle-bootstrap --mode development

bootstrap-production:
	uv run tricycle-bootstrap --mode production

backfill-thermodynamics:
	uv run python scripts/backfill_mapped_reaction_thermodynamics.py

reconcile-reaction-geometries:
	uv run tricycle-reconcile-reaction-geometries \
		$(if $(RECONCILE_BATCH_SIZE),--batch-size "$(RECONCILE_BATCH_SIZE)",) \
		$(if $(RECONCILE_LIMIT),--limit "$(RECONCILE_LIMIT)",) \
		$(if $(RECONCILE_START_AFTER),--start-after "$(RECONCILE_START_AFTER)",) \
		$(if $(RECONCILE_MAPPED_REACTION_ID),--mapped-reaction-id "$(RECONCILE_MAPPED_REACTION_ID)",) \
		$(if $(RECONCILE_STATEMENT_TIMEOUT_MS),--statement-timeout-ms "$(RECONCILE_STATEMENT_TIMEOUT_MS)",) \
		$(if $(RECONCILE_REACTION_TIMEOUT_SECONDS),--reaction-timeout-seconds "$(RECONCILE_REACTION_TIMEOUT_SECONDS)",) \
		$(if $(filter true 1 yes,$(RECONCILE_SCAN_ALL)),--scan-all,) \
		$(if $(filter true 1 yes,$(RECONCILE_DRY_RUN)),--dry-run,)

seed-da-bench:
	uv run tricycle-seed-da-bench

import-artifacts:
	@test -n "$(IMPORT_PROJECT_ID)" || (echo "set IMPORT_PROJECT_ID to a project UUID" >&2; exit 2)
	@test -n "$(IMPORT_ROOTS)" || (echo "set IMPORT_ROOTS='path/to/file-or-directory ...'" >&2; exit 2)
	$(IMPORT_RUNTIME_ENV) uv run tricycle-import-artifacts \
		--project-id "$(IMPORT_PROJECT_ID)" \
		$(if $(IMPORT_USER_ID),--user-id "$(IMPORT_USER_ID)",) \
		$(if $(IMPORT_ARTIFACT_KIND),--artifact-kind "$(IMPORT_ARTIFACT_KIND)",) \
		$(if $(IMPORT_STATE_FILE),--state-file "$(IMPORT_STATE_FILE)",) \
		$(if $(IMPORT_COMMIT_BATCH_FILES),--commit-batch-files "$(IMPORT_COMMIT_BATCH_FILES)",) \
		$(if $(IMPORT_PIPELINE_WINDOW_FILES),--pipeline-window-files "$(IMPORT_PIPELINE_WINDOW_FILES)",) \
		$(if $(IMPORT_STREAM_QUEUE_SIZE),--stream-queue-size "$(IMPORT_STREAM_QUEUE_SIZE)",) \
		$(IMPORT_ROOTS)

validate-da-bench-fixture:
	uv run --frozen python scripts/validate_da_bench_fixture.py

storage-gc:
	uv run tricycle-rustfs-gc

auth-session-cleanup:
	uv run tricycle-auth-session-cleanup

benchmark-upload-resources:
	@test -n "$(UPLOAD_BENCHMARK_FIXTURE)" || (echo "set UPLOAD_BENCHMARK_FIXTURE=/path/to/real-gaussian-orca-file-or-directory" >&2; exit 2)
	uv run --frozen python scripts/benchmark_upload_resources.py --fixture "$(UPLOAD_BENCHMARK_FIXTURE)"

benchmark-remote-upload-resources:
	@test -n "$(REMOTE_BENCHMARK_FIXTURE)" || (echo "set REMOTE_BENCHMARK_FIXTURE=/path/to/real-gaussian-orca-file-or-directory" >&2; exit 2)
	docker compose -f compose.yaml -f compose.compute.yaml build api
	docker compose -f compose.yaml -f compose.compute.yaml run --rm --no-deps \
		-e TRICYCLE_MAX_BATCH_FILES=$(or $(REMOTE_BENCHMARK_MAX_BATCH_FILES),1024) \
		-e TRICYCLE_MAX_BATCH_BYTES=$(or $(REMOTE_BENCHMARK_MAX_BATCH_BYTES),1073741824) \
		-v "$(CURDIR):/workspace:ro" \
		-v "$(REMOTE_BENCHMARK_FIXTURE):/remote-fixture:ro" api \
		/app/.venv/bin/python /workspace/scripts/benchmark_remote_upload_batch.py \
		--fixture /remote-fixture \
		$(if $(REMOTE_BATCH_SIZES),--batch-sizes $(REMOTE_BATCH_SIZES),)

capture-query-plan-evidence:
	@test -n "$(DATASET_SCALE)" || (echo "set DATASET_SCALE to the snapshot ID/scale" >&2; exit 2)
	@test -n "$(QUERY_PLAN_OUTPUT)" || (echo "set QUERY_PLAN_OUTPUT=/path/to/query-plans.json" >&2; exit 2)
	uv run --frozen python scripts/capture_query_plan_evidence.py \
		--dataset-scale "$(DATASET_SCALE)" --output "$(QUERY_PLAN_OUTPUT)"

probe-shared-rate-limit:
	@test -n "$(RATE_LIMIT_MODE)" || (echo "set RATE_LIMIT_MODE=shared or fail-closed" >&2; exit 2)
	@test -n "$(API_URLS)" || (echo "set API_URLS='https://api-01 https://api-02'" >&2; exit 2)
	@test -n "$(RATE_LIMIT_OUTPUT)" || (echo "set RATE_LIMIT_OUTPUT=/path/to/rate-limit.json" >&2; exit 2)
	@test -n "$(ACCEPTANCE_BEARER_TOKEN)" || (echo "set ACCEPTANCE_BEARER_TOKEN from the secret manager" >&2; exit 2)
	uv run --frozen python scripts/probe_shared_rate_limit.py \
		--mode "$(RATE_LIMIT_MODE)" \
		$(foreach url,$(API_URLS),--api-url "$(url)") \
		--output "$(RATE_LIMIT_OUTPUT)"

probe-upload-limit:
	@test -n "$(UPLOAD_API_URL)" || (echo "set UPLOAD_API_URL=https://api.example.test" >&2; exit 2)
	@test -n "$(UPLOAD_PROJECT_ID)" || (echo "set UPLOAD_PROJECT_ID to an authorized project UUID" >&2; exit 2)
	@test -n "$(UPLOAD_MAX_BYTES)" || (echo "set UPLOAD_MAX_BYTES to the configured per-file limit" >&2; exit 2)
	@test -n "$(ACCEPTANCE_BEARER_TOKEN)" || (echo "set ACCEPTANCE_BEARER_TOKEN from the secret manager" >&2; exit 2)
	@test -n "$(UPLOAD_LIMIT_OUTPUT)" || (echo "set UPLOAD_LIMIT_OUTPUT=/path/to/upload-limit.json" >&2; exit 2)
	uv run --frozen python scripts/probe_upload_limit.py \
		--api-url "$(UPLOAD_API_URL)" \
		--project-id "$(UPLOAD_PROJECT_ID)" \
		--maximum-upload-bytes "$(UPLOAD_MAX_BYTES)" \
		--output "$(UPLOAD_LIMIT_OUTPUT)"

validate-restore:
	uv run --frozen tricycle-validate-restore

deployment-smoke:
	uv run --frozen tricycle-deployment-smoke

validate-deployment-acceptance:
	@test -n "$(ACCEPTANCE_RECORD)" || (echo "set ACCEPTANCE_RECORD=/path/to/acceptance-record.json" >&2; exit 2)
	uv run --frozen tricycle-validate-deployment-acceptance "$(ACCEPTANCE_RECORD)"

serve:
	uv run tricycle-api

serve-nexusx:
	uv run tricycle-nexusx-services
