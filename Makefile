# All targets run INSIDE the container (constraint C1: host needs only Docker).
COMPOSE := docker compose -f docker/compose.yml

.PHONY: build up down logs test lint check shell pull-model prod prod-down

build: ## Build the self-contained image
	$(COMPOSE) --profile dev build app

up: ## Dev server on :8080 (bind-mounts src, syncs deps in-container, reload)
	$(COMPOSE) --profile dev up --build app

prod: ## Hardened deployment on :8080 (read-only rootfs, non-root, data volume)
	$(COMPOSE) --profile prod up -d --build prod

prod-down: ## Stop the hardened deployment
	$(COMPOSE) --profile prod down prod

down: ## Stop the stack
	$(COMPOSE) --profile dev --profile test --profile prod --profile ollama down

logs: ## Tail app logs
	$(COMPOSE) logs -f app

test: ## Run the test suite inside the container
	$(COMPOSE) --profile test run --rm test

lint: ## Ruff check inside the container
	$(COMPOSE) --profile test run --rm --entrypoint sh test -c "uv run ruff check ."

check: lint test ## Lint + tests, all in-container (M0 acceptance gate)
	@echo "check: green"

shell: ## Debug shell inside the dev image (e.g. uv run python -m rpg.cli ping)
	$(COMPOSE) --profile dev run --rm --entrypoint sh app

pull-model: ## Pull a model into the sibling ollama service (model=<name>)
	$(COMPOSE) --profile ollama exec ollama ollama pull $(model)
