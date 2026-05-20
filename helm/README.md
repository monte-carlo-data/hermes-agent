# Hermes Agent Helm Chart

Deploys the Monte Carlo Hermes agent and its observability sidecars into a Kubernetes cluster.

## What Gets Deployed

| Resource | Name | Conditional |
|---|---|---|
| Deployment | `mcd-agent-deployment` | Always |
| DaemonSet | `logs-collector` | `logShipping == "fluentd"` |
| DaemonSet | `metrics-collector` | `metricsCollector.enabled` |
| Namespace | configurable (default `mcd-agent`) | Always |
| ServiceAccount | `mcd-agent-service-account` | Always |
| SecretStore | `mcd-agent-secret-store` | `!skipExternalSecrets` |
| ExternalSecret | `mcd-agent-token-secret` | `!skipExternalSecrets` |
| ExternalSecret | `mcd-integrations-secrets` | `!skipExternalSecrets` |
| ClusterRole + Binding | `mcd-agent-metrics-reader` | `metricsCollector.enabled` |

## Quick Start

```bash
# Install External Secrets Operator (required for cloud deployments)
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets -n external-secrets --create-namespace

# Deploy the agent
helm upgrade --install mcd-agent ./helm \
  -f environments/examples/azure/values.yaml \
  -n mcd-agent --create-namespace
```

## Cloud Platform Setup

Each cloud platform requires specific resources and identity configuration before deploying the agent. This section covers the prerequisites — ESO installation and the `helm upgrade` deployment command are covered in [Quick Start](#quick-start).

### AWS (EKS)

**Prerequisites:**
- An S3 bucket for agent storage
- An AWS Secrets Manager secret containing `{"mcd_id": "...", "mcd_token": "..."}`
- An IAM role granting the ESO service account access to Secrets Manager

**Identity/Auth (IRSA):**
- The chart's `secretStore.provider.aws.role` must be set to the IAM role ARN that can read Secrets Manager secrets
- The IAM role's trust policy must allow the EKS cluster's OIDC provider to assume it from the `mcd-agent-service-account` service account in the `mcd-agent` namespace

**Storage:**
- The IAM role (or a separate role attached to the node instance profile) needs `s3:GetObject`, `s3:PutObject`, and `s3:ListBucket` on the storage bucket

### Azure (AKS)

**Prerequisites:**
- An Azure Key Vault with a secret containing `{"mcd_id": "...", "mcd_token": "..."}`
- A User Assigned Managed Identity with "Get" access to Key Vault secrets
- An Azure Storage Account with a blob container
- AKS cluster with OIDC issuer and Workload Identity enabled

**Identity/Auth (Workload Identity):**
- Create a federated credential on the managed identity, trusting the AKS OIDC issuer for subject `system:serviceaccount:mcd-agent:mcd-agent-service-account`:
  ```bash
  az identity federated-credential create \
    --name "kubernetes-federated-credential" \
    --identity-name "<identity-name>" \
    --resource-group "<resource-group>" \
    --issuer "$(az aks show -n <cluster> -g <rg> --query oidcIssuerProfile.issuerUrl -o tsv)" \
    --subject "system:serviceaccount:mcd-agent:mcd-agent-service-account"
  ```
- Set the managed identity's client ID in the values file:
  ```yaml
  serviceAccount:
    annotations:
      azure.workload.identity/client-id: <client-id>
  deploymentTemplateLabels:
    azure.workload.identity/use: "true"
  ```

**Storage:**
- Grant the managed identity the "Storage Blob Data Contributor" role on the storage account or container

### GCP (GKE)

**Prerequisites:**
- A GCS bucket for agent storage
- A Google Secret Manager secret containing `{"mcd_id": "...", "mcd_token": "..."}`
- A GCP IAM service account with `roles/secretmanager.secretAccessor`
- GKE cluster with Workload Identity enabled

**Identity/Auth (Workload Identity):**
- Bind the GCP service account to the Kubernetes service account:
  ```bash
  gcloud iam service-accounts add-iam-policy-binding "<sa-email>" \
    --role="roles/iam.workloadIdentityUser" \
    --member="serviceAccount:<project-id>.svc.id.goog[mcd-agent/mcd-agent-service-account]"
  ```
- Set the GCP service account annotation in the values file:
  ```yaml
  serviceAccount:
    annotations:
      iam.gke.io/gcp-service-account: <sa-email>
  ```

**Storage:**
- Grant the service account "Storage Object Admin" and "Storage Bucket Viewer (Beta)" roles on the GCS bucket (`storage.objects.*` and `storage.buckets.get` are both required)

## Configuration

The chart is configured via values files. See the example files for each platform:

| File | Environment | Notes |
|---|---|---|
| `environments/examples/aws/values.yaml` | AWS EKS | S3 storage, AWS Secrets Manager |
| `environments/examples/azure/values.yaml` | Azure AKS | Azure Blob storage, Key Vault |
| `environments/examples/gcp/values.yaml` | GCP GKE | GCS storage, Google Secret Manager |
| `environments/local/values.yaml` | Local (kind/k3s) | MinIO storage, manual secrets |

### Core Values

| Property | Description | Default |
|---|---|---|
| `image.repository` | Agent container image | `montecarlodata/agent` |
| `image.tag` | Image tag | `latest-generic` |
| `image.pullPolicy` | Pull policy | `Always` |
| `container.backendServiceUrl` | Orchestrator URL | _(required)_ |
| `container.storageType` | Storage backend (`S3`, `GCS`, `AZURE_BLOB`) | _(required)_ |
| `container.storageBucketName` | Bucket/container name | _(required)_ |
| `container.storageAccountName` | Azure storage account name | _(Azure only)_ |
| `container.opsRunnerThreadCount` | Concurrent operation threads | `"18"` |
| `container.publisherThreadCount` | Concurrent result publisher threads | `"3"` |
| `container.resources` | Pod CPU/memory requests and limits | `{}` (cluster defaults) |
| `podSecurityContext` | Pod-level security context | non-root, UID/GID 1000 (see [Pod Security](#pod-security)) |
| `containerSecurityContext` | Agent container security context | privilege escalation off, all capabilities dropped |
| `namespace` | Kubernetes namespace | `mcd-agent` |
| `replicaCount` | Agent replicas (ignored when `autoscaling.enabled`) | `2` |

### Pod Security

The agent image bakes a non-root user (`mcdagent`, UID/GID 1000) and the chart sets pod- and container-level security contexts that satisfy the common K8s hardening checks: `runAsNonRoot: true`, `runAsUser: 1000`, `runAsGroup: 1000`, `fsGroup: 1000`, `allowPrivilegeEscalation: false`, and `capabilities.drop: [ALL]`.

`readOnlyRootFilesystem` is intentionally **off** by default — the agent writes temporary files (CA bundles, DB certs, git checkouts) under `/tmp` at runtime. Enabling it requires mounting an `emptyDir` at `/tmp`.

Override the defaults to match your cluster's policies:

```yaml
podSecurityContext:
  runAsUser: 2000
  runAsGroup: 2000
  fsGroup: 2000
containerSecurityContext:
  allowPrivilegeEscalation: false
  capabilities:
    drop: [ALL]
  seccompProfile:
    type: RuntimeDefault
```

The bundled `metrics-collector` daemonset is also hardened: the upstream `otel/opentelemetry-collector-k8s` image is distroless and bakes `USER 10001:10001`, so the chart applies a matching pod-level securityContext (`runAsNonRoot: true`, UID/GID/fsGroup `10001`) and drops capabilities on both the init and main containers. The init container's security context is exposed separately as `metricsCollector.initContainerSecurityContext` so you can set `readOnlyRootFilesystem: true` on the OTel main container without breaking the busybox init's writes to the shared emptyDir. Override via `metricsCollector.podSecurityContext`, `metricsCollector.containerSecurityContext`, and `metricsCollector.initContainerSecurityContext`.

The firewall-CA init container (rendered when `firewallCa.cert` or `firewallCa.externalSecretRef` is set) has its own overridable context at `firewallCa.securityContext` — useful on managed K8s tiers that reject `seccompProfile: RuntimeDefault` or mandate specific UID ranges.

#### Values structure note
The agent's security contexts live at the top level (`podSecurityContext`, `containerSecurityContext`) for backwards compatibility with values files that pre-date the metrics-collector hardening. The metrics-collector and firewall-CA settings nest under `metricsCollector.*` / `firewallCa.*`. When overriding both, remember to set the top-level keys for the agent and the nested keys for the others.

#### `seccompProfile: RuntimeDefault` compatibility
Defaults include `seccompProfile.type: RuntimeDefault`, which requires Kubernetes **1.19+** at the field level (and is reliably available on managed distros from 1.22+). Clusters that disallow it will fail admission with a `seccompProfile` validation error. To opt out:

```yaml
containerSecurityContext:
  seccompProfile: null
metricsCollector:
  containerSecurityContext:
    seccompProfile: null
  initContainerSecurityContext:
    seccompProfile: null
firewallCa:
  securityContext:
    seccompProfile: null
```

> Note: The `logs-collector` (fluentd) daemonset is **not** hardened — fluentd reads host paths (`/var/log/pods`, `/var/log/containers`) that are typically root-owned, so it must run as root. If your cluster enforces `runAsNonRoot` for daemonsets, leave `logShipping` at its default (`in-process`): the agent ships its own logs to the same `/api/v1/agent/logs` endpoint and no DaemonSet is required. To opt out of MC log shipping entirely and route agent logs through your own logging stack, set `logShipping: none` (the agent emits structured JSON to stdout).

### Resource Requests and Limits

By default the agent container ships with no `resources` block, so the pod runs with whatever the cluster's default LimitRange provides. To pin CPU/memory, set `container.resources`:

```yaml
container:
  resources:
    requests:
      cpu: "500m"
      memory: "512Mi"
    limits:
      cpu: "2"
      memory: "2Gi"
```

Requests must be set if you plan to enable autoscaling — HPA computes utilization against requests.

### Autoscaling

The chart can render a `HorizontalPodAutoscaler` that scales the agent deployment based on CPU (and optionally memory) utilization.

| Property | Description | Default |
|---|---|---|
| `autoscaling.enabled` | Render the HPA | `false` |
| `autoscaling.minReplicas` | Lower bound on replicas | `2` |
| `autoscaling.maxReplicas` | Upper bound on replicas | `5` |
| `autoscaling.targetCPUUtilizationPercentage` | Target CPU utilization (%) | `70` |
| `autoscaling.targetMemoryUtilizationPercentage` | Target memory utilization (%) | `null` (CPU-only) |

Prerequisites:
- `container.resources.requests` must be set (CPU and/or memory matching the HPA target).
- `metrics-server` must be installed in the cluster (standard in EKS/AKS/GKE).

When `autoscaling.enabled` is `true`, the Deployment omits `replicas:` so the HPA fully owns the replica count.

### Secret Store

The chart uses the [External Secrets Operator](https://external-secrets.io/) to sync secrets from cloud secret managers. Configure via `secretStore.provider` with your cloud-specific settings. Set `skipExternalSecrets: true` for local development where secrets are created manually.

### Log Shipping

Top-level `logShipping` selects how agent logs reach Monte Carlo. Pick one:

| Mode | What it does |
|---|---|
| `in-process` (default) | The agent buffers its own logs and POSTs them to `/api/v1/agent/logs`. Works in any cluster — no DaemonSet, no host paths, no root containers. |
| `fluentd` | A fluentd DaemonSet tails container logs from the host and forwards them to the same endpoint. Requires root pods (host log paths are root-owned). |
| `none` | No MC log shipping. The agent emits structured JSON to stdout — forward it through your own stack (CloudWatch, Splunk, Azure Monitor, etc.). |

The `fluentd` mode honours the `logsCollector.*` settings below. The other modes ignore them.

| Property | Default |
|---|---|
| `logShipping` | `in-process` |
| `inProcessLogs.logLevel` | `"INFO"` (in-process only; allowlist: `INFO`, `WARNING`, `WARN`, `ERROR`, `CRITICAL` — `DEBUG` is excluded to avoid leaking third-party-library content) |
| `logsCollector.logLevel` | `"INFO\|WARN\|WARNING\|ERROR\|CRITICAL"` (fluentd only) |
| `logsCollector.image.repository` | `fluent/fluentd-kubernetes-daemonset` |
| `logsCollector.image.tag` | `v1.18-debian-forward-1` |
| `logsCollector.buffer.flushInterval` | `5m` |
| `logsCollector.buffer.chunkLimitSize` | `8MB` |
| `logsCollector.buffer.totalLimitSize` | `512MB` |
| `logsCollector.resources` | CPU/memory requests and limits (`{}` = cluster defaults) |

When `logShipping: in-process` is selected, the chart renders `MCD_IN_PROCESS_LOGS_LEVEL` on the agent container from `inProcessLogs.logLevel` (default `INFO`). `DEBUG` is intentionally not in the allowlist — it would surface third-party-library content (request bodies, tokens) into shipped logs.

### OAuth Authentication

The agent supports OAuth 2.0 `client_credentials` authentication as an alternative to the
key/token secret (`mcd-agent-token-secret`). When configured, the agent acquires Bearer tokens
and uses them for all backend communication. OAuth takes precedence over key/token auth when
both are configured — this makes migration easy (deploy with both, then remove the token secret).

| Property | Description | Default |
|---|---|---|
| `oauth.existingSecret` | Name of a pre-created K8s Secret containing `client_id` and `client_secret` keys | _(unset — OAuth disabled)_ |
| `oauth.tokenEndpoint` | Override the OAuth token endpoint URL | _(derived from `container.backendServiceUrl`)_ |

The `existingSecret` must be a Kubernetes Secret in the same namespace as the agent, with two keys:
- `client_id` — the OAuth client ID
- `client_secret` — the OAuth client secret

By default, the agent derives the token endpoint from `container.backendServiceUrl` (replacing the
first hostname segment with `m2m`). Set `oauth.tokenEndpoint` only for custom or private Cognito
deployments where the default derivation doesn't apply.

### Metrics Collector

An OpenTelemetry Collector DaemonSet that scrapes container CPU and memory metrics from the Kubelet API.

| Property | Default |
|---|---|
| `metricsCollector.enabled` | `true` |
| `metricsCollector.collectionIntervalSeconds` | `300` |
| `metricsCollector.image.repository` | `otel/opentelemetry-collector-k8s` |
| `metricsCollector.image.tag` | `0.147.0` |
| `metricsCollector.resources` | CPU/memory requests and limits (`{}` = cluster defaults) |

### Firewall TLS Inspection Support

When deploying behind a corporate firewall that performs TLS inspection (e.g. Azure Firewall Premium), the agent and collectors need to trust the firewall's CA certificate. Configure via `firewallCa.*` values — no changes needed when TLS inspection is not in use.

| Property | Description |
|---|---|
| `firewallCa.cert` | Inline PEM certificate (stored in a ConfigMap) |
| `firewallCa.externalSecretRef` | Secret key name in the configured secret store (fetched via ExternalSecret) |

These are **mutually exclusive** — setting both will fail at template time.

When either is set, the chart automatically:
- Adds an `alpine` init container to each workload that merges system CAs with the firewall CA into a combined bundle
- Sets `REQUESTS_CA_BUNDLE` (agent deployment) and `SSL_CERT_FILE` (logs collector) to the combined bundle path
- Configures the metrics collector's OTel exporter with `ca_file` pointing to the combined bundle

### Extension Points

The deployment template supports generic escape hatches for custom configuration:

| Property | Description |
|---|---|
| `container.extraEnv` | Additional env vars for the agent container |
| `extraInitContainers` | Additional init containers |
| `extraVolumeMounts` | Additional volume mounts for the agent container |
| `extraVolumes` | Additional volumes |
