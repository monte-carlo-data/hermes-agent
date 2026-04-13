# Hermes Agent - Generic Egress-only Agent

This project is a generic egress-only agent that can be executed in any on-prem or cloud environment (through Kubernetes or similar tools).

## Running the Agent Locally with Kubernetes

This section explains how to run the agent locally inside a [kind](https://kind.sigs.k8s.io/) Kubernetes cluster, pointing to a local PostgreSQL database running in Docker, and using MinIO (inside docker) as S3 compatible storage.

### Prerequisites

Follow this [document](REDACTED_INTERNAL_DOC_URL to create an agent and integration key on MCD.


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
Update the Dockerfile to remove pre-installed packages from the base image so your local versions take precedence, then copies the local `code` source via `--build-context`:

Update your Dockerfile with
```
# delete the source code and pre-installed packages from the base image,
# so our requirements.txt versions take precedence
RUN rm -rf ./apollo && \
    . $VENV_DIR/bin/activate && pip uninstall -y apollo-agent agent-base 2>/dev/null; true

# Copy local agent-common for editable install.
# Pass via: --build-context agent-common=../agent-common
COPY --from=agent-common . /agent-common
```

Rebuild your docker image
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

This creates the namespace, service account, deployment, and service. The pod will initially fail because the secrets don't exist yet — that's expected.

```bash
# if you need to delete the namespace
kubectl delete namespace mcd-agent

helm upgrade --install hermes-agent ./helm \
  -f environments/local/values.yaml \
  --namespace mcd-agent \
  --create-namespace
```

### Step 6 — Create Kubernetes Secrets

The Helm chart expects two Kubernetes Secrets that are normally provisioned by the External Secrets Operator in cloud environments. For local development, create them manually in the namespace that Helm just created.

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

#### 6b — Create the integrations secret

For testing PostgreSQL connectivity, create a secret with the connection details. The key name (e.g. `pg_local.json`) must match the `file_path` configured in the Monte Carlo connection settings.

```bash
kubectl create secret generic mcd-integrations-secrets \
  --namespace mcd-agent \
  --from-literal=pg_local.json='{"connect_args": {"host":"host.docker.internal","port":5433,"database":"hermes","user":"hermes","password":"hermes"}}'
```

> **Note:** The JSON must use `connect_args` with psycopg2-compatible keys (`host`, `port`, `database`, `user`, `password`). The Monte Carlo connection must be configured with `selfHostedCredentialsType: FILE` and `filePath: /etc/secrets/integrations/pg_local.json`.


#### 6c — Restart the deployment to pick up the new secrets

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

The Helm chart deploys an optional **DaemonSets** that run on every node in the cluster. Toggled via `values.yaml` and authenticate in orchestrator using `x-mcd-id` / `x-mcd-token` headers, extracted at startup from the `mcd-agent-token-secret` Kubernetes Secret (`contents.json` → `mcd_id` + `mcd_token`).

#### Logs Collector (`logsCollector`)

| | |
|---|---|
| **Toggle** | `logsCollector.enabled: true\|false` |
| **Image** | `fluent/fluentd:v1.18-1` (configurable via `logsCollector.image`) |
| **How it works** | Uses [Fluentd](https://www.fluentd.org/) to tail container log files from the host (`/var/log/containers/*_mcd-agent_*.log`). Runs as root (`runAsUser: 0`) to read host log files. Parses the CRI log format, transforms each line into `{"timestamp": "...", "message": "..."}`, and POSTs JSON arrays to the orchestrator. |
| **Flush interval** | Every `5m` by default (`logsCollector.buffer.flushInterval`). Fluentd buffers logs to disk and flushes in batches. |
| **Log level filter** | Optional — set `logsCollector.logLevel` to a regex (e.g. `"WARN\|ERROR\|CRITICAL"`) to only forward matching lines. Omit or leave empty to send all logs. |
| **Endpoint** | `logsCollector.output.endpoint` → `POST /api/v1/agent/logs` |
| **Buffer settings** | Configurable chunk size (`8MB`), total limit (`512MB`), retry with exponential backoff (up to `30s`). See `logsCollector.buffer.*` in `values.yaml`. |

##### Configuration defaults

All properties below have defaults in the Helm templates and can be omitted from `values.yaml` unless you need to override them.

| Property | Default |
|---|---|
| `logsCollector.logLevel` | `"WARN\|WARNING\|ERROR\|CRITICAL"` |
| `logsCollector.image.repository` | `"fluent/fluentd"` |
| `logsCollector.image.tag` | `"v1.18-1"` |
| `logsCollector.buffer.flushInterval` | `"5m"` |
| `logsCollector.buffer.retryMaxTimes` | `5` |
| `logsCollector.buffer.retryWait` | `"1s"` |
| `logsCollector.buffer.chunkLimitSize` | `"8MB"` |
| `logsCollector.buffer.totalLimitSize` | `"512MB"` |
| `logsCollector.buffer.overflowAction` | `"block"` |
| `logsCollector.buffer.retryMaxInterval` | `"30s"` |


#### Metrics Collector (`metricsCollector`)

| | |
|---|---|
| **Toggle** | `metricsCollector.enabled: true\|false` |
| **Image** | `otel/opentelemetry-collector-k8s:0.147.0` (configurable via `metricsCollector.image`) |
| **How it works** | Uses the [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/) with the `kubeletstats` receiver to scrape container CPU and memory metrics from the Kubelet API. Runs as a DaemonSet with `serviceAccount` auth. An Alpine init container extracts credentials from the JSON secret into files that the collector reads via `${file:...}` syntax. |
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

#### Checking DaemonSet logs

```bash
# Logs collector
kubectl logs -n mcd-agent -l app=logs-collector --tail=50
kubectl logs -n mcd-agent -l app=logs-collector -f          # follow in real-time

# Metrics collector
kubectl logs -n mcd-agent -l app=metrics-collector --tail=50
kubectl logs -n mcd-agent -l app=metrics-collector -f
```

#### Checking DaemonSet status

```bash
kubectl get daemonsets -n mcd-agent
kubectl describe daemonset logs-collector -n mcd-agent
kubectl describe daemonset metrics-collector -n mcd-agent
```

### Firewall TLS Inspection Support

When deploying behind a corporate firewall that performs TLS inspection (e.g. Azure Firewall Premium), the agent and collectors need to trust the firewall's CA certificate. The Helm chart supports this via `firewallCa.*` values — no changes needed when TLS inspection is not in use.

| Property | Description |
|---|---|
| `firewallCa.cert` | Inline PEM certificate (stored in a ConfigMap) |
| `firewallCa.externalSecretRef` | Secret key name in the configured secret store (fetched via ExternalSecret) |

When either is set, the chart automatically:
- Adds an `alpine` init container to each workload that merges system CAs with the firewall CA
- Sets `REQUESTS_CA_BUNDLE` (agent) and `SSL_CERT_FILE` (logs collector) to the combined bundle
- Configures the metrics collector's OTel exporter with `ca_file` pointing to the combined bundle

### Restarting & Updating

```bash
# Restart all components after config/secret changes
kubectl rollout restart deployment/mcd-agent-deployment -n mcd-agent
kubectl rollout restart daemonset/logs-collector -n mcd-agent
kubectl rollout restart daemonset/metrics-collector -n mcd-agent

# Or restart everything at once
kubectl rollout restart daemonset/logs-collector daemonset/metrics-collector deployment/mcd-agent-deployment -n mcd-agent

# Apply Helm values changes (no image rebuild needed)
helm upgrade --install hermes-agent ./helm \
  -f environments/local/values.yaml \
  --namespace mcd-agent

# Scale down (pause) individual components
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
- **ExternalSecrets:** Cloud deployments use the External Secrets Operator. The local values disable it (`externalSecrets: false`), so you must create Kubernetes Secrets manually as shown above.
- **PostgreSQL from inside kind:** The Docker-for-Mac DNS name `host.docker.internal` resolves to the host machine, allowing pods to reach the PostgreSQL container running on the host's port 5432.
- **Log Collection:** The logs collector run as DaemonSets. Enabled by default in the local values. Set `logsCollector.enabled: false` in `values.yaml` and run `helm upgrade` to disable them. Both require the `mcd-agent-token-secret` to authenticate with the orchestrator.
