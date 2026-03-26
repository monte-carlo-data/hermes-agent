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

Create `postgres.json` from the template if it doesn't exist:

```bash
cp environments/local/secrets/postgres-template.json environments/local/secrets/postgres.json
```

Replace the empty integrations secret with the Postgres connection details:

```bash
kubectl delete secret mcd-integrations-secrets -n mcd-agent
kubectl create secret generic mcd-integrations-secrets -n mcd-agent \
  --from-file=postgres.json=environments/local/secrets/postgres.json
kubectl rollout restart deployment/mcd-agent-deployment -n mcd-agent
```

Then register the credentials in Monte Carlo:

```bash
montecarlo integrations add-self-hosted-credentials-v2 \
  --connection-type postgres \
  --self-hosted-credentials-type FILE \
  --file-path /etc/secrets/integrations/postgres.json \
  --name postgres-local
```

See the [Self-Hosted Credentials](https://docs.getmontecarlo.com/docs/self-hosted-credentials) documentation for how to define the JSON file for other integrations and how to complete the configuration in Monte Carlo.
