.PHONY: up down demo baf-smoke test-client test-infra test-ci
.env:
	@umask 077; token=$$(openssl rand -hex 32) && printf 'CLIENT_TOKEN=%s\nGATEWAY_PORT=18086\n' "$$token" > $@
up: .env
	docker compose up -d --build --wait gateway
down:
	docker compose down -v
demo: up
	@$(MAKE) --no-print-directory baf-smoke
baf-smoke:
	@set -a; . ./.env; set +a; python3 -m runtime.client --checkpoint1
test-infra:
	docker compose exec -T sandbox-manager python -m unittest runtime.test_manager
test-client:
	docker compose exec -T gateway python -m unittest runtime.test_gateway runtime.test_smoke runtime.test_suite
test-ci: test-client test-infra baf-smoke
