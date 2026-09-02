# Hermes Agent - Generic Egress-only Agent

This project is a generic egress-only agent that can be executed in any on-prem or cloud environment (through Kubernetes or similar tools).

## Running the Agent Locally with Kubernetes

This section explains how to run the agent locally inside a [kind](https://kind.sigs.k8s.io/) Kubernetes cluster, pointing to a local PostgreSQL database running in Docker, and using MinIO (inside docker) as S3 compatible storage.

### Prerequisites

Follow *Step 1 — Register the Agent* in the [Kubernetes agent documentation](https://docs.getmontecarlo.com/docs/kubernetes) to create an agent in Monte Carlo and obtain its credentials. The Key ID and Key Secret it gives you are the `mcd_id` and `mcd_token` used below.


| Tool | Install |
|------|---------|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | Required for containers and kind |
| [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) | `brew install kind` |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | `brew install kubectl` |
| [Helm](https://helm.sh/docs/intro/install/) | `brew install helm` |

### Step 1 — Start local services (PostgreSQL + MinIO)

A `docker-compose.yaml` in the project root spins up PostgreSQL 16 and [MinIO](https://min.io/) (S3-compatible object storage):

```bash
docker compose up -d
```

This creates:

**PostgreSQL:**
- **Host (from inside kind):** `host.docker.internal`
- **Port:** `5432`
- **Database / User / Password:** `hermes`

**MinIO (S3-compatible storage):**
- **API endpoint (from inside kind):** `http://host.docker.internal:9000`
- **Web console:** `http://localhost:9001`
- **Root user / password:** `minioadmin`

Verify both are running:

```bash
docker compose ps
```

#### Create the MinIO storage bucket

Open the MinIO console at [http://localhost:9001](http://localhost:9001), log in with `minioadmin` / `minioadmin`, and create a bucket named `local-storage` (matching `storageBucketName` in `values.yaml`).

Alternatively, use the MinIO client:

```bash
# Install mc (MinIO Client)
brew install minio/stable/mc

# Configure the local alias
mc alias set local http://localhost:9000 minioadmin minioadmin

# Create the bucket
mc mb local/local-storage
```

### Step 2 — Create the kind cluster

```bash
kind create cluster --name hermes-local --image kindest/node:v1.32.2
```

Confirm the cluster is ready:

```bash
kubectl cluster-info --context kind-hermes-local
```
Kubernetes control plane is running at https://127.0.0.1:58375
CoreDNS is running at https://127.0.0.1:58375/api/v1/namespaces/kube-system/services/kube-dns:dns/proxy

### Step 3 — Build the Docker image

```bash
docker build --pull --no-cache --target hermes-generic -t hermes-agent:local .
```

When developing, you can point to a local repo (e.g. `agent-common`) to test your changes.
The base image (`montecarlodata/agent:<version>-system-base`) contains only system-level
dependencies (apt packages) — no pre-installed venv — so passing your local source as a
`--build-context` and copying it from the Dockerfile is enough.

1. Add this `COPY` to the Dockerfile so the build pulls in your local source:

   ```
   # Copy local agent-common from the named build context for an editable install.
   COPY --from=agent-common . /agent-common
   ```

2. Build the image, supplying the local repo path via `--build-context`:

   ```bash
   docker build --pull --no-cache --target hermes-generic -t hermes-agent:local \
     --build-context agent-common=../agent-common .
   ```

### Step 4 — Load the image into kind

kind runs its own container registry. You must load your local image so the cluster can pull it:

```bash
kind load docker-image hermes-agent:local --name hermes-local
```

### Step 5 — Deploy with Helm

This creates the namespace, service account, deployment, and service. The pod will stay in `ContainerCreating` (with a `FailedMount` event) until the secrets exist — that's expected. Once the secrets are created in the next step, the pod starts on its own; no restart needed.

```bash
# if you need to delete the namespace
kubectl delete namespace mcd-agent

helm upgrade --install hermes-agent ./helm \
  -f environments/local/values.yaml \
  --namespace mcd-agent \
  --create-namespace
```

### Step 6 — Create Kubernetes Secrets

The Helm chart requires one Kubernetes Secret — the agent auth secret, either `mcd-agent-token-secret` (key/token) or `mcd-oauth-secret` (OAuth), depending on the authentication method you choose — normally provisioned by the External Secrets Operator in cloud environments. For local development, create it manually in the namespace that Helm just created. The integrations secret (`mcd-integrations-secrets`) is optional and only needed when testing a self-hosted integration, such as PostgreSQL below.

#### 6a — Create the agent token secret

You need a valid MCD agent token JSON. If you have one, create the secret from it. Otherwise, use a placeholder for basic startup testing:

```bash
# Option A: from a real token file
kubectl create secret generic mcd-agent-token-secret \
  --namespace mcd-agent \
  --from-file=contents.json=<path-to-your-token-file>

# Option B: placeholder (agent will start but cannot communicate with orchestrator)
kubectl create secret generic mcd-agent-token-secret \
  --namespace mcd-agent \
  --from-literal=contents.json='{"mcd_id":"<id>","mcd_token":"<secret>"}'
```

#### 6a-alt — Use OAuth authentication (alternative to token secret)

Instead of the key/token secret, you can authenticate via OAuth `client_credentials`. Create a JSON credentials file and a K8s Secret from it:

```bash
# Create the credentials file
cat > /tmp/oauth-creds.json << 'EOF'
{"client_id": "<your-client-id>", "client_secret": "<your-client-secret>"}
EOF

# Create the K8s secret (the key must be credentials.json)
kubectl create secret generic mcd-oauth-secret \
  --namespace mcd-agent \
  --from-file=credentials.json=/tmp/oauth-creds.json

# Clean up the local file
rm /tmp/oauth-creds.json
```

Then add `--set oauthSecret.enabled=true` to your `helm upgrade` command. When OAuth is configured, the chart mounts only the OAuth secret (token secret is not used). See the [Helm chart README](helm/README.md#oauth-authentication) for details.

#### 6b — Create the integrations secret (optional)

This step is only needed when testing an integration. For testing PostgreSQL connectivity, create a secret with the connection details. The key name (e.g. `pg_local.json`) must match the `file_path` configured in the Monte Carlo connection settings.

```bash
kubectl create secret generic mcd-integrations-secrets \
  --namespace mcd-agent \
  --from-literal=pg_local.json='{"connect_args": {"host":"host.docker.internal","port":5433,"database":"hermes","user":"hermes","password":"hermes"}}'
```

> **Note:** The JSON must use `connect_args` with psycopg2-compatible keys (`host`, `port`, `database`, `user`, `password`). The Monte Carlo connection must be configured with `selfHostedCredentialsType: FILE` and `filePath: /etc/secrets/integrations/pg_local.json`.


#### 6c — Restart the deployment to pick up the new secrets (optional)

This step is only needed when testing an integration, to pick up the integrations secret created in 6b.

```bash
kubectl rollout restart deployment/mcd-agent-deployment -n mcd-agent
```

### Step 7 — Verify the deployment

```bash
# Check all pods are running
kubectl get pods -n mcd-agent

# Watch agent logs
kubectl logs -n mcd-agent -l app=mcd-agent -f

# Test the health endpoint
kubectl port-forward -n mcd-agent svc/mcd-agent-loadbalancer-service 8080:8080
# In another terminal:
curl http://localhost:8080/api/v1/test/health
```

### Observability — Log Collection

The Helm chart can ship agent logs to the orchestrator (`/api/v1/agent/logs`) one of two ways, selected by the top-level `logShipping` value:

| `logShipping` | What runs | Cluster requirements |
|---|---|---|
| `in-process` (default) | The agent buffers its own logs in-process and POSTs them to the orchestrator. | None beyond the agent itself — works on clusters that disallow root pods. |
| `fluentd` | A fluentd DaemonSet (`logs-collector`) tails container log files from each node and forwards them to the same endpoint. | Requires root pods (host log paths are root-owned). |
| `none` | No MC log shipping. The agent emits structured JSON to stdout. | Bring your own logging stack. |

Both shipping modes authenticate to the orchestrator using whichever authentication method is configured — OAuth Bearer tokens (when `oauthSecret` is configured) or `x-mcd-id` / `x-mcd-token` headers from the `mcd-agent-token-secret` Secret.

#### `logShipping: in-process`

| Property | Default |
|---|---|
| `inProcessLogs.logLevel` (chart) | `INFO` — rendered as `MCD_IN_PROCESS_LOGS_LEVEL` on the agent container; allowlist: `INFO`, `WARNING`, `WARN`, `ERROR`, `CRITICAL` (`DEBUG` excluded to avoid leaking third-party-library content) |
| Buffer size | 10000 records (drops oldest on overflow; surfaces a synthetic warning on the next flush) |
| Flush cadence | Reuses the existing "Logs sender" timer (300s by default) — no separate timer |
| Persistence | None — buffer is in-memory; the agent flushes synchronously on graceful shutdown |

Records are emitted as `{timestamp, message}`. The agent's `instance_id` is attached to the request via the `x-mcd-agent-instance-id` header (set by `BackendClient` on every call) and stamped onto each record orchestrator-side, so backend visibility matches the fluentd path.

Set `logShipping: fluentd` to opt into the fluentd DaemonSet path instead — it tails container log files from the host and POSTs the same shape to `/api/v1/agent/logs`, but requires root pods (host log paths are root-owned). Tunables live under `logsCollector.*`; see [helm/README.md](helm/README.md#log-shipping) for the full property table.

#### Metrics Collector (`metricsCollector`)

| | |
|---|---|
| **Toggle** | `metricsCollector.enabled: true\|false` |
| **Image** | `otel/opentelemetry-collector-k8s:0.147.0` (configurable via `metricsCollector.image`) |
| **How it works** | Uses the [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/) with the `kubeletstats` receiver to scrape container CPU and memory metrics from the Kubelet API. Runs as a DaemonSet with `serviceAccount` auth. A busybox (`busybox:1.37`) init container extracts credentials from the JSON secret into files that the collector reads via `${file:...}` syntax. |
| **Collection interval** | Every `60s` by default (`metricsCollector.collectionIntervalSeconds`). |
| **Metrics collected** | `container.cpu.time`, `container.cpu.usage`, `container.memory.usage`, `container.memory.rss`, `container.memory.working_set` |
| **Endpoint** | `metricsCollector.output.endpoint` → `PUT /api/v1/agent/metrics` (OTLP JSON format) |
| **Namespace filter** | Only collects metrics from the agent namespace (`mcd-agent` by default). |

##### Configuration defaults

| Property | Default |
|---|---|
| `metricsCollector.collectionIntervalSeconds` | `60` |
| `metricsCollector.image.repository` | `"otel/opentelemetry-collector-k8s"` |
| `metricsCollector.image.tag` | `"0.147.0"` |

#### Checking DaemonSet logs and status

```bash
kubectl logs -n mcd-agent -l app=metrics-collector --tail=50
kubectl logs -n mcd-agent -l app=metrics-collector -f          # follow in real-time

kubectl get daemonsets -n mcd-agent
kubectl describe daemonset metrics-collector -n mcd-agent
```

If you've opted into `logShipping: fluentd`, swap `metrics-collector` for `logs-collector` to inspect the fluentd pods.

### Restarting & Updating

```bash
# Restart the agent and the metrics-collector daemonset after config/secret changes
kubectl rollout restart deployment/mcd-agent-deployment daemonset/metrics-collector -n mcd-agent

# Apply Helm values changes (no image rebuild needed)
helm upgrade --install hermes-agent ./helm \
  -f environments/local/values.yaml \
  --namespace mcd-agent

# Scale down (pause) the agent
kubectl scale deployment mcd-agent-deployment --replicas=0 -n mcd-agent
# DaemonSets can't be scaled — disable them via values.yaml and re-run helm upgrade

# Rebuild and redeploy after code changes
docker build --target hermes-generic -t hermes-agent:local .
kind load docker-image hermes-agent:local --name hermes-local
kubectl rollout restart deployment/mcd-agent-deployment -n mcd-agent
```

### Tear down

```bash
# Stop all pods
kubectl scale deployment mcd-agent-deployment --replicas=0 -n mcd-agent

# Remove the Helm release (removes all resources including DaemonSets)
helm uninstall hermes-agent --namespace mcd-agent

# Delete the kind cluster
kind delete cluster --name hermes-local

# Stop all local services
docker compose down        # keeps data
docker compose down -v     # removes data volumes too
```

### Useful commands

```bash
# Recreate a secret (e.g. after changing values)
kubectl delete secret mcd-agent-token-secret -n mcd-agent
kubectl create secret generic mcd-agent-token-secret \
  --namespace mcd-agent \
  --from-literal=contents.json='{"mcd_id":"<your-id>","mcd_token":"<your-token>"}'
kubectl rollout restart deployment/mcd-agent-deployment -n mcd-agent

# Verify integrations secret content
kubectl get secret mcd-integrations-secrets -n mcd-agent -o jsonpath='{.data}' \
  | python3 -c "import sys,json,base64; d=json.load(sys.stdin); print({k: base64.b64decode(v).decode() for k,v in d.items()})"

# Check file inside the pod
kubectl exec -n mcd-agent deploy/mcd-agent-deployment -- cat /etc/secrets/integrations/pg_local.json

# check secret id token
kubectl get secret mcd-agent-token-secret -n mcd-agent -o jsonpath='{.data.contents\.json}' | base64 -d | jq .

# Check all resources in the namespace
kubectl get all -n mcd-agent
```

### Notes

- **Apple Silicon (M1/M2/M3/M4):** The local values file sets `nodeSelector` to `arm64`. If you are on an Intel Mac, change it to `amd64` in `environments/local/values.yaml`.
- **Backend URL:** By default the local values point to the dev orchestrator (`artemis.dev.getmontecarlo.com`). Update `container.backendServiceUrl` if you need to target a different environment.
- **ExternalSecrets:** Cloud deployments use the External Secrets Operator. The local values disable it (`skipExternalSecrets: true`), so you must create Kubernetes Secrets manually as shown above.
- **PostgreSQL from inside kind:** The Docker-for-Mac DNS name `host.docker.internal` resolves to the host machine, allowing pods to reach the PostgreSQL container running on the host's port 5432.
- **Log Collection:** Default `logShipping: in-process` — the agent ships its own logs to the orchestrator. Set `logShipping: fluentd` to deploy the fluentd DaemonSet instead (requires root pods), or `logShipping: none` to disable MC log shipping entirely. All modes authenticate to the orchestrator using whichever method is configured — OAuth (`oauthSecret`) or `x-mcd-id`/`x-mcd-token` headers from the `mcd-agent-token-secret`.
