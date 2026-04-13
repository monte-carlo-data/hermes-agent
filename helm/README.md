# Hermes Agent Helm Chart

Deploys the Monte Carlo Hermes agent and its observability sidecars into a Kubernetes cluster.

## What Gets Deployed

| Resource | Name | Conditional |
|---|---|---|
| Deployment | `mcd-agent-deployment` | Always |
| DaemonSet | `logs-collector` | `logsCollector.enabled` |
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
  -f environments/dev/values_az_fw.yaml \
  -n mcd-agent --create-namespace
```

## Configuration

The chart is configured via values files. See the dev environment files for working examples:

| File | Environment | Notes |
|---|---|---|
| `environments/dev/values_aws.yaml` | AWS EKS | S3 storage, AWS Secrets Manager |
| `environments/dev/values_az.yaml` | Azure AKS | Azure Blob storage, Key Vault |
| `environments/dev/values_az_fw.yaml` | Azure AKS + Firewall | Same as above with firewall CA |
| `environments/dev/values_gke.yaml` | GCP GKE | GCS storage, Google Secret Manager |
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
| `namespace` | Kubernetes namespace | `mcd-agent` |
| `replicaCount` | Agent replicas | `1` |

### Secret Store

The chart uses the [External Secrets Operator](https://external-secrets.io/) to sync secrets from cloud secret managers. Configure via `secretStore.provider` with your cloud-specific settings. Set `skipExternalSecrets: true` for local development where secrets are created manually.

### Logs Collector

A Fluentd DaemonSet that tails agent container logs and forwards them to the orchestrator.

| Property | Default |
|---|---|
| `logsCollector.enabled` | `true` |
| `logsCollector.logLevel` | `"INFO\|WARN\|WARNING\|ERROR\|CRITICAL"` |
| `logsCollector.image.repository` | `fluent/fluentd-kubernetes-daemonset` |
| `logsCollector.image.tag` | `v1.18-debian-forward-1` |
| `logsCollector.buffer.flushInterval` | `5m` |
| `logsCollector.buffer.chunkLimitSize` | `8MB` |
| `logsCollector.buffer.totalLimitSize` | `512MB` |

### Metrics Collector

An OpenTelemetry Collector DaemonSet that scrapes container CPU and memory metrics from the Kubelet API.

| Property | Default |
|---|---|
| `metricsCollector.enabled` | `true` |
| `metricsCollector.collectionIntervalSeconds` | `300` |
| `metricsCollector.image.repository` | `otel/opentelemetry-collector-k8s` |
| `metricsCollector.image.tag` | `0.147.0` |

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
