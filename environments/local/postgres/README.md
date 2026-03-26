# PostgreSQL Local Setup

Start PostgreSQL:

```bash
docker compose up -d
```

This starts PostgreSQL 16 on port `5433` (host) with:
- Database: `mcd`
- User: `mcd`
- Password: `mcd`

Data is persisted in a Docker volume (`pgdata`) across restarts.

## Connecting

From the host:

```bash
psql -h localhost -p 5433 -U mcd -d mcd
```

From inside the Kubernetes cluster (e.g. the agent pod), PostgreSQL is reachable at `host.docker.internal:5433`.

## Adding as an Agent Integration

Replace the empty integrations secret with the Postgres connection details:

```bash
kubectl delete secret mcd-integrations-secrets -n mcd-agent
kubectl create secret generic mcd-integrations-secrets -n mcd-agent \
  --from-file=postgres.json=environments/local/secrets/postgres.json
kubectl rollout restart deployment/mcd-agent-deployment -n mcd-agent
```
