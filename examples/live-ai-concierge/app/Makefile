verify: lint test e2e

lint:
	uv run ruff check src tests

test:
	uv run pytest -q tests/unit

e2e:
	uv run pytest -q tests/e2e

run:
	uv run concierge --port 8000

scripted:
	CONCIERGE_SCRIPT=spec/fixtures/script.json uv run concierge --port 8000

.PHONY: verify lint test e2e run scripted
