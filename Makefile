.PHONY: worker api cli temporal test fmt lint reindex

worker:
	python apps/worker/main.py

api:
	uvicorn apps.api.main:app --reload --port 8080

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
