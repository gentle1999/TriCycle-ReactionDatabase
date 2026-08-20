.PHONY: init frontend-install frontend-build frontend-check frontend-test-e2e serve-frontend \
	format lint type test test-db test-storage test-redis test-infra audit vendor-audit check \
	db-up db-down storage-up storage-down auth-up auth-down infra-up infra-down migrate import-artifacts \
	backfill-thermodynamics bootstrap-development bootstrap-production seed-da-bench serve serve-nexusx storage-gc auth-session-cleanup \
	benchmark-upload-resources capture-query-plan-evidence probe-shared-rate-limit probe-upload-limit \
	validate-da-bench-fixture validate-restore deployment-smoke validate-deployment-acceptance \
	stack-build stack-up stack-down stack-logs

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
	docker compose build api frontend nginx

stack-up:
	docker compose up -d --build --wait

stack-down:
	docker compose down

stack-logs:
	docker compose logs --follow api frontend nginx

migrate:
	uv run alembic upgrade head

bootstrap-development:
	uv run tricycle-bootstrap --mode development

bootstrap-production:
	uv run tricycle-bootstrap --mode production

backfill-thermodynamics:
	uv run python scripts/backfill_mapped_reaction_thermodynamics.py

seed-da-bench:
	uv run tricycle-seed-da-bench

import-artifacts:
	@test -n "$(IMPORT_PROJECT_ID)" || (echo "set IMPORT_PROJECT_ID to a project UUID" >&2; exit 2)
	@test -n "$(IMPORT_ROOTS)" || (echo "set IMPORT_ROOTS='path/to/file-or-directory ...'" >&2; exit 2)
	uv run tricycle-import-artifacts \
		--project-id "$(IMPORT_PROJECT_ID)" \
		$(if $(IMPORT_USER_ID),--user-id "$(IMPORT_USER_ID)",) \
		$(if $(IMPORT_ARTIFACT_KIND),--artifact-kind "$(IMPORT_ARTIFACT_KIND)",) \
		$(if $(IMPORT_STATE_FILE),--state-file "$(IMPORT_STATE_FILE)",) \
		$(IMPORT_ROOTS)

validate-da-bench-fixture:
	uv run --frozen python scripts/validate_da_bench_fixture.py

storage-gc:
	uv run tricycle-rustfs-gc

auth-session-cleanup:
	uv run tricycle-auth-session-cleanup

benchmark-upload-resources:
	uv run --frozen python scripts/benchmark_upload_resources.py

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
