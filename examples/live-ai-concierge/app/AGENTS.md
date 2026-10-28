# Live AI Concierge

Build the app described in `spec/REQUIREMENTS.md`. `spec/ACCEPTANCE.md` says when it is done, and `spec/ui/` shows the target look.

## Commands

- `make verify`: lint, unit tests and browser tests. This is the definition of "done" for any change. It must pass before you commit.
- `make scripted`: run the app on :8000 in scripted mode with `spec/fixtures/script.json`. No credentials needed.
- `make run`: run the app on :8000 against the real Gemini Live API. Copy `.env.example` to `.env` first and sign in with `gcloud auth application-default login`.

## Rules

- Python dependencies are managed with uv. Use `uv add`, never pip.
- Browser tests use pytest-playwright and run the server in scripted mode (`tests/conftest.py`: `server_url` shared, `fresh_server_url` per test). Use the `data-testid` hooks from REQUIREMENTS §12.
- Do not hard-code values from `spec/fixtures/script.json`. The UX runs use a different script (`ux/ux_script.json`).
- `spec/` is the product decision and `ux/` is how the app is checked: change neither to make a change pass.
- Before writing or changing code that uses Google ADK or the Gemini Live API, check the current API documentation. Your training data may be out of date.
- Commit in small steps with clear messages.
