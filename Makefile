.PHONY: up down demo baf-record baf-replay baf-smoke test-client test-infra test-ci
export LOCAL_UID := $(shell id -u)
export LOCAL_GID := $(shell id -g)
.env:
	@umask 077; token=$$(openssl rand -hex 32) && printf 'CLIENT_TOKEN=%s\nGATEWAY_PORT=18086\n' "$$token" > $@
up: .env
	mkdir -p results/baf
	docker compose up -d --build --wait gateway
down:
	docker compose --profile record --profile experiment down -v
demo: up
	@$(MAKE) --no-print-directory baf-smoke
baf-smoke:
	@set -a; . ./.env; set +a; python3 -m runtime.client --checkpoint1
test-infra:
	docker compose exec -T sandbox-manager python -m unittest runtime.test_manager
test-client:
	docker compose exec -T gateway python -m unittest runtime.test_gateway runtime.test_smoke runtime.test_suite runtime.test_experiment
test-ci: test-client test-infra

baf-record: .env
	docker compose up -d --build --wait sandbox-manager model-proxy
	docker compose run --build --rm --no-deps --user "$$(id -u):$$(id -g)" baf-runner record

export RUN
baf-replay: .env
	@test -n "$$RUN" || { echo "Usage: make baf-replay RUN=<record-id>"; exit 2; }
	docker compose up -d --wait sandbox-manager
	docker compose run --build --rm --no-deps -e RUN --user "$$(id -u):$$(id -g)" baf-runner replay
