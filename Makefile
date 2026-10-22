.PHONY: worker worker-docker api web web-build cli temporal test fmt lint reindex

worker:
	python apps/worker/main.py

worker-docker:
	docker compose -f docker-compose.dev.yml up -d worker

api:
	uvicorn apps.api.main:app --reload --port 8080

web:
	cd apps/web && npm run dev

web-build:
	cd apps/web && npm ci && npm run build

cli:
	python apps/cli/main.py --help

temporal:
	docker compose -f docker-compose.dev.yml up -d

test:
	pytest -q

fmt:
	ruff format .

lint:
	ruff check .

reindex:
	python apps/cli/main.py reindex --repo-root . --compose-path docker-compose.yml
