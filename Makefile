.PHONY: up down logs migrate run shell test lint reset

up:            ## поднять инфраструктуру + api
	cp -n .env.example .env || true
	docker compose up -d --build
	@echo "api      -> http://localhost:8080/docs"
	@echo "minio    -> http://localhost:9001 (minioadmin/minioadmin)"
	@echo "meili    -> http://localhost:7700"
	@echo "searxng  -> http://localhost:8888"

down:
	docker compose down

logs:
	docker compose logs -f api

migrate:
	docker compose exec api harvester migrate

run:           ## make run TASK=tasks/sol-lemon.yaml
	docker compose exec api harvester run $(or $(TASK),tasks/sol-lemon.yaml) -v

shell:
	docker compose exec api bash

test:
	docker compose exec api python -m pytest -q

lint:
	ruff check harvester

reset:         ## снести все данные
	docker compose down -v
