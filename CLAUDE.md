# Hermes Agent

Generic egress-only agent for on-premises/cloud environments. Flask HTTP service
that runs inside Kubernetes via Helm, handling communication with the Monte Carlo
backend.

## Tech Stack

- **Python 3.12** / Flask / Gunicorn
- **Deployment:** Docker -> Kubernetes (Helm charts in `helm/`)
- **CI:** CircleCI (`.circleci/config.yml`)
- **Dependencies:** managed with pip-compile — edit `requirements.in`, run `pip-compile`,
  then regenerate the attribution file with `python scripts/generate_notice.py` and
  commit the updated `NOTICE`

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Local dev with kind cluster: see `environments/local/README.md`.

## Testing & Verification

```bash
pytest tests
```

CI also runs linting and type checking:

```bash
black --check .
pyright
```

All three (pytest, black, pyright) must pass before merging.

## Building

```bash
# Production image
docker build --target hermes-generic -t hermes-agent:local .

# Run tests during build
docker build --target tests -t hermes-agent:tests .

# Local agent-common development
docker build --target hermes-generic -t hermes-agent:local \
  --build-context agent-common=../agent-common .
```

## Key Paths

| Path | Purpose |
|---|---|
| `hermes/agent/main.py` | Flask app entry point |
| `hermes/agent/service/` | Core service logic (OnPremService, MetricsService), credential sourcing (`credentials_source.py`), and login-token-provider selection (`login_token_provider_factory.py`, `OAuthLoginTokenProvider`, `TokenLoginTokenProvider`) — provider selection lives in the factory, not `OnPremService.__init__` |
| `gunicorn.conf.py` | Gunicorn config (signal handler registration) |
| `tests/` | Unit tests (pytest) |
| `helm/` | Kubernetes Helm charts |
| `environments/` | Per-environment config (local, dev) |
