# Hermes Agent Helm Chart

Deploys the Monte Carlo Hermes agent and its observability sidecars into a Kubernetes cluster.

## What Gets Deployed

| Resource | Name | Conditional |
|---|---|---|
| Deployment | `mcd-agent-deployment` | Always |
| DaemonSet | `logs-collector` | `logShipping == "fluentd"` |
| DaemonSet | `metrics-collector` | `metricsCollector.enabled` |
| Namespace | configurable (default `mcd-agent`) | `namespaceCreate` (default `true`) |
| ServiceAccount | `mcd-agent-service-account` | Always |
| SecretStore | `mcd-agent-secret-store` | `!skipExternalSecrets` |
| ExternalSecret | `mcd-agent-token-secret` | key/token auth with a `tokenSecret.remoteRef` source |
| ExternalSecret | `mcd-oauth-secret` | OAuth with an `oauthSecret.remoteRef` source |
| ExternalSecret | `mcd-integrations-secrets` | `integrationsSecrets.data` is set |
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
| `namespaceCreate` | Let the release create and own the namespace | `true` |
| `replicaCount` | Agent replicas (ignored when `autoscaling.enabled`) | `2` |

### Namespace Ownership

By default the chart renders the namespace named by `namespace`, so the release owns it and `helm uninstall` removes it.

Helm cannot adopt a namespace created outside Helm. Installing with the default against a namespace made by `kubectl create namespace` fails:

```
Error: INSTALLATION FAILED: unable to continue with install: Namespace "mcd-agent" in
namespace "" exists and cannot be imported into the current release: invalid ownership
metadata; label validation error: missing key "app.kubernetes.io/managed-by" ...
```

When the namespace is provisioned outside the release — by a platform team, by `kubectl`, or by a Terraform `kubernetes_namespace` resource — set `namespaceCreate: false` instead of hand-labelling the namespace:

```bash
kubectl create namespace mcd-agent
helm upgrade --install mcd-agent ./helm -n mcd-agent --set namespaceCreate=false -f values.yaml
```

The namespace must exist before installing: it holds both the release record and every chart resource. Chart resources are still individually owned by the release either way — only the Namespace object's ownership differs, so with `namespaceCreate: false` the namespace survives `helm uninstall`.

> **Set `namespaceCreate` at install time, not on an existing release.** Helm deletes resources that disappear from a release's manifest between revisions, so switching it from `true` to `false` on a deployed release deletes the namespace — cascading to the agent, its secrets, and the release record itself when the release lives in that same namespace. The upgrade reports success while this happens. To move an existing release onto an externally managed namespace, `helm uninstall` first, then reinstall with `namespaceCreate: false`.

#### Release namespace vs. resource namespace

These are independent. Every chart resource carries an explicit `metadata.namespace` taken from `namespace`, while the release record (`sh.helm.release.v1.<name>.vN`) lives in whatever `-n` points at — `default` when omitted. Installing without `-n` therefore puts the release in `default` and the workloads in `mcd-agent`, where `helm list -n mcd-agent` shows nothing. Pass `-n <namespace>` to keep them together.

`-n` alone cannot bootstrap a namespace the chart is about to create — Helm needs the release namespace to exist before writing the release record — so pair it with `--create-namespace` (Helm stamps its own ownership metadata on a namespace it creates, so the chart's `Namespace` object adopts it cleanly — unlike one made by `kubectl create namespace`) or with `namespaceCreate: false`.

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

The chart uses the [External Secrets Operator](https://external-secrets.io/) to sync secrets from cloud secret managers. Configure via `secretStore.provider` with your cloud-specific settings. Set `skipExternalSecrets: true` for local development where secrets are created manually, or when reading credentials directly from AWS Secrets Manager on a cluster without ESO (see [Reading Credentials Directly from AWS Secrets Manager](#reading-credentials-directly-from-aws-secrets-manager)). It is incompatible with a `remoteRef` still configured on `tokenSecret` or `oauthSecret` — that combination fails at template time (see [OAuth Authentication](#oauth-authentication)).

Only the authentication secret is required. `mcd-integrations-secrets` is mounted optionally, so no placeholder secret is needed when there are no self-hosted integration credentials — the `mcd-integrations-secrets` ExternalSecret is rendered only when `integrationsSecrets.data` is set.

The authentication secret is mounted non-optionally, so the agent pod stays in `ContainerCreating` until it exists. It does not have to exist before `helm install`: the kubelet retries the mount and the pod starts on its own once the secret appears, with no restart. Two exceptions where it must exist first — `helm install --wait`/`--atomic`, and the Terraform `helm_release` resource, which defaults to `wait = true` with a 300s timeout and fails the apply instead of waiting.

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

### Reading Credentials Directly from AWS Secrets Manager

On EKS the agent can read its own credential out of AWS Secrets Manager instead of having it synced into a Kubernetes Secret. Nothing materializes the credential in the cluster, and the External Secrets Operator is not involved — useful where ESO is unavailable, or where putting the credential in a Secret is not acceptable.

```yaml
# key/token
tokenSecret:
  awsSecretsManager:
    secretId: mcd/agent/token       # name or ARN
    region: us-east-1               # optional

# or OAuth
oauthSecret:
  enabled: true
  awsSecretsManager:
    secretId: mcd/agent/oauth

skipExternalSecrets: true

# Required: defaults to true, and the collectors cannot read this source.
metricsCollector:
  enabled: false
```

Set `skipExternalSecrets: true` unless ESO is also installed for another reason (e.g. syncing integration credentials): with only `awsSecretsManager` configured, nothing needs the `SecretStore` the chart otherwise renders, and on a cluster with no ESO the `SecretStore` CRD doesn't exist, so `helm install` fails with `no matches for kind "SecretStore"`. Leave `skipExternalSecrets` unset only when you also want ESO for integration credentials, and configure `secretStore` in that case. `remoteRef` and `awsSecretsManager` are mutually exclusive within a block and configuring both fails at template time.

**The metrics and logs collectors are incompatible with this source.** Both read `mcd_id`/`mcd_token` out of the `mcd-agent-token-secret` Secret with an init container, and that Secret is not created when the agent reads its own credential from a secret manager — so their pods would sit in `ContainerCreating` on every node while the install reported success. The chart rejects the combination at template time rather than rendering it. Set `metricsCollector.enabled: false` and leave `logShipping` at its default `in-process` (the agent ships its own logs, so nothing is lost there), or use a `remoteRef` credential source if you need the collectors.

Container CPU and memory metrics are the only thing given up. If you need them alongside this source, the collectors would have to learn to read the credential the same way the agent does — not currently implemented.

**Migrating an existing ESO deployment to ASM.** Removing `remoteRef` and setting `skipExternalSecrets: true` must land in the same change — either edit alone leaves an invalid intermediate state: dropping `remoteRef` while `skipExternalSecrets` is unset still renders a `SecretStore` against a cluster that may have no such CRD, and setting `skipExternalSecrets: true` while `remoteRef` remains is rejected at template time (see [OAuth Authentication](#oauth-authentication) for the full enumeration of rejected combinations).

**IAM prerequisite.** The agent's *own* service account needs `secretsmanager:GetSecretValue` on the secret. That is a different principal from the one the `secretStore` uses — with ESO it is the External Secrets Operator that reads the secret, so an existing deployment grants the permission to ESO rather than to the agent.

The permission policy is the same either way. Scope `Resource` to the single secret, not a prefix — the section above shows self-hosted integration credentials commonly read through the same pod identity, so a wildcard like `mcd/agent/*` also grants the agent read access to every other secret an operator organizes under that prefix. Secrets Manager appends a random six-character suffix to the name, so an ARN assembled from the name alone does not match. Use the secret's real ARN — `aws secretsmanager describe-secret --secret-id mcd/agent/token --query ARN --output text`. Deleting and recreating the secret produces a new suffix and a new ARN; rotating its value does not.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "<the-secret-arn>"
    },
    {
      "Effect": "Allow",
      "Action": ["kms:Decrypt"],
      "Resource": "<kms-key-arn>",
      "Sid": "OnlyRequiredForCustomerManagedKmsKeys"
    }
  ]
}
```

The `kms:Decrypt` statement is only required when the secret is encrypted with a customer-managed KMS key — Secrets Manager's default AWS-managed key needs no extra grant. Without it on a CMK-encrypted secret, the read fails with `AccessDeniedException`, which surfaces the same way a missing `GetSecretValue` grant does — see below.

How that role reaches the agent's service account is up to your cluster. Both mechanisms work — the agent resolves credentials through the standard AWS chain and does not care which is in use:

- **EKS Pod Identity** (what the Terraform modules use). Requires the `eks-pod-identity-agent` add-on, a role trusted by `pods.eks.amazonaws.com` for `sts:AssumeRole` and `sts:TagSession`, and an association. No service-account annotation is involved:

  ```bash
  aws eks create-pod-identity-association \
    --cluster-name <cluster> \
    --namespace mcd-agent \
    --service-account mcd-agent-service-account \
    --role-arn <role-arn>
  ```

- **IRSA**, for clusters already standardized on it. Requires a cluster OIDC provider and a role whose trust policy allows `sts:AssumeRoleWithWebIdentity` from `system:serviceaccount:<namespace>:mcd-agent-service-account`, then annotate the service account:

  ```yaml
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::<account-id>:role/<agent-role>
  ```

In two common cases the role already exists and only its policy needs widening:

- **Deployed with a Terraform module.** The agent's service account already has a Pod Identity association to the `<cluster>-pod-identity` role, which carries the S3 policy for agent storage. Add Secrets Manager read to it — there is no new role or association to create.
- **Already reading self-hosted integration credentials from AWS Secrets Manager.** The agent fetches those itself, through the same pod identity, so it already holds `secretsmanager:GetSecretValue`. Extend the resource scope to cover the agent's own secret.

Without the permission the agent starts and then fails to authenticate; the reachability test reports `no-token-id` (or `no-client-id`) alongside `credentials_source: aws_secrets_manager` and the secret id it tried to read. A missing `kms:Decrypt` grant on a customer-managed KMS key surfaces the same way — the `AccessDeniedException` is indistinguishable from a missing `GetSecretValue` grant in the reachability test output.

The source caches the credential for 15 minutes. Key/token reads the source on every request, so a rotated secret is picked up within that window without a restart — more promptly than the ESO path's default hourly refresh. OAuth only re-reads the source when its access token needs refreshing — roughly every 48 minutes with a one-hour token (the agent refreshes at about 80% of the token lifetime) — so for OAuth the token lifetime, not the 15-minute source cache, is the binding constraint on how quickly a rotation is picked up. Both are still faster than ESO's hourly default. A read failure with a cached credential in hand logs a warning and keeps using it, since it stays valid until rotation.

Detaching the IAM policy does not stop a running agent: it keeps using its already-cached credential until the staleness bound expires, and only then fails to refresh. Revoking access at the backend (rotating the credential's counterpart there) is the effective lever if the agent must be cut off promptly.

AWS Secrets Manager is the only direct source today. Azure Key Vault and Google Secret Manager deployments continue to use ESO.

#### How the Payload Is Stored

Plain JSON in `SecretString` is the default. Binary secrets — `--secret-binary`, or Terraform's `secret_binary` — are read as UTF-8 with no extra configuration.

Base64-encoded values need `base64Encoded: true`, which applies to every secret in the block:

```yaml
tokenSecret:
  awsSecretsManager:
    secretId: mcd/agent/token
    base64Encoded: true
```

Decoding is never automatic: without the flag an encoded payload fails with `Credentials are not valid JSON`. The agent distinguishes each way a payload can be unusable, which is the only diagnostic available where operators can write secrets but not read them back:

| Error | Cause |
|---|---|
| `Secret X exists but has no value yet` | No version yet, or a whitespace-only value |
| `not valid JSON … looks like base64-encoded JSON` | Encoded payload, often a copied `kubectl get secret -o yaml` value or a stray `base64encode(...)` on Terraform's `secret_string` |
| `not valid JSON` with no base64 note | A bare value, or malformed JSON |
| `Secret X is configured as base64-encoded but its value is not valid base64 text` | Flag set against a plain payload |
| `neither a string nor a binary value` | Neither field populated |
| `Secret X holds binary data that is not UTF-8 text, so it cannot hold agent credentials` | Binary payload that isn't UTF-8 text (e.g. compressed or DER) |
| `not valid JSON … looks doubly encoded` | Value encoded twice with `base64Encoded: true` set |

### OAuth Authentication

The agent supports OAuth 2.0 `client_credentials` authentication as an alternative to the
key/token secret (`mcd-agent-token-secret`). When configured, the agent acquires Bearer tokens
and uses them for all backend communication. The chart uses one authentication method at a time
— when OAuth is enabled, only the OAuth secret is mounted.

Set `oauthSecret.enabled: true` to use OAuth. `enabled` selects the authentication method; the credential itself comes from one of three sources — an ExternalSecret (`remoteRef`), AWS Secrets Manager (`awsSecretsManager`, see [Reading Credentials Directly from AWS Secrets Manager](#reading-credentials-directly-from-aws-secrets-manager)), or a Secret you create yourself (omit both, and set `skipExternalSecrets: true`). Keeping `enabled` separate from the source means adding a new credential source doesn't change how the method is chosen.

```yaml
# ExternalSecret deployments
oauthSecret:
  enabled: true
  remoteRef:
    key: <your-oauth-secret-name>

# Manually created Secret (skipExternalSecrets: true)
oauthSecret:
  enabled: true
```

| Property | Description | Default |
|---|---|---|
| `oauthSecret.enabled` | Selects OAuth authentication | _(unset)_ |
| `oauthSecret.remoteRef` | ExternalSecret remote reference — the credential source for ExternalSecret deployments | _(unset)_ |
| `oauthSecret.awsSecretsManager.secretId` | AWS Secrets Manager secret the agent reads directly — see [Reading Credentials Directly from AWS Secrets Manager](#reading-credentials-directly-from-aws-secrets-manager) | _(unset)_ |
| `oauthSecret.awsSecretsManager.region` | Optional region override for the above | _(unset)_ |
| `oauthSecret.awsSecretsManager.base64Encoded` | Decode every secret in this block as base64 before use — see [How the Payload Is Stored](#how-the-payload-is-stored) | `false` |
| `oauthSecret.tokenEndpoint` | Override the OAuth token endpoint URL | _(derived from `container.backendServiceUrl`)_ |

For backwards compatibility a `remoteRef` or `awsSecretsManager` on its own also selects OAuth, which is what the Terraform modules and older values files emit — `enabled: true` is simply the clearer way to express it. The following combinations fail at template time rather than silently picking a method or a source:

- `enabled: false` alongside a credential source (`remoteRef` or `awsSecretsManager`) — contradictory.
- `oauthSecret` and `tokenSecret` both carrying a credential source — two methods configured at once.
- `remoteRef` and `awsSecretsManager` in the same block (`oauthSecret` or `tokenSecret`) — see [Reading Credentials Directly from AWS Secrets Manager](#reading-credentials-directly-from-aws-secrets-manager) for why these are mutually exclusive.
- `remoteRef` together with `skipExternalSecrets: true` — nothing would sync the credential; this also applies to the migration case in the ASM section above, where removing `remoteRef` and setting `skipExternalSecrets: true` must land together.
- `awsSecretsManager` present without a `secretId` — an empty source is not a valid one.
- `awsSecretsManager` together with `metricsCollector.enabled: true` or `logShipping: fluentd` — the collectors cannot read this source; see below.

The `remoteRef` + `skipExternalSecrets` rejection is a deliberate behaviour change: an existing values file that kept a `remoteRef` after switching to a hand-created Secret will fail on its next upgrade rather than silently rendering a broken `SecretStore`.

The release prints the secret name, key, and payload shape it expects on install — `helm get notes <release>` retrieves it later. If that isn't the secret you created, the release selected the other authentication method.

**Cloud deployments** (ExternalSecret): Set `oauthSecret.remoteRef` to point to a secret in your
cloud secret manager containing JSON: `{"client_id": "...", "client_secret": "..."}`. The chart
creates an ExternalSecret that syncs it as a K8s Secret named `mcd-oauth-secret`.

```yaml
oauthSecret:
  remoteRef:
    key: <your-oauth-secret-name>
```

**Local/manual deployments** (`skipExternalSecrets: true`): Create the `mcd-oauth-secret` K8s
Secret manually and set `oauthSecret.enabled: true`:

```bash
kubectl create secret generic mcd-oauth-secret \
  --namespace mcd-agent \
  --from-file=credentials.json=/path/to/oauth-creds.json
```

```yaml
skipExternalSecrets: true
oauthSecret:
  enabled: true
```

**Docker Compose:** Mount a JSON credentials file and set `MCD_OAUTH_FILE_PATH` to its path:

```yaml
services:
  mcd-agent:
    environment:
      - MCD_OAUTH_FILE_PATH=/etc/secrets/mcd-oauth/credentials.json
    volumes:
      - ./secrets/oauth.json:/etc/secrets/mcd-oauth/credentials.json:ro
```

On EC2 or ECS, where an instance profile or task role already supplies AWS credentials, set `MCD_AWS_SECRET_ID_OAUTH` (or `MCD_AWS_SECRET_ID_KEY_TOKEN`, plus an optional `MCD_AWS_SECRET_REGION` and, for a base64-encoded payload, `MCD_AWS_SECRET_BASE64_ENCODED` — honoured only for the exact value `true`) instead of mounting a file — the role plays the same part IRSA does on EKS, and the credential stays out of the host filesystem. Don't reach for these env vars with static `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` values just to avoid the mount: that replaces one on-disk secret with a broader-scoped one.

By default, the agent derives the token endpoint from `container.backendServiceUrl` (replacing the
first hostname segment with `m2m`). Set `oauthSecret.tokenEndpoint` only for custom or private
Cognito deployments where the default derivation doesn't apply.

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
