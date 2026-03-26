# Local Agent Development Environment

Run the Hermes agent locally using Rancher Desktop (k3s) or any local Kubernetes cluster.

## Prerequisites

- A local Kubernetes cluster (e.g. [Rancher Desktop](https://rancherdesktop.io/), k3s, kind, minikube)
- `kubectl` and `helm` CLI tools installed
- An agent token (MCD ID + MCD token) from Monte Carlo

> **Note:** The commands below assume your local cluster is the current kubectl context.
> If it isn't, add `--context <context-name>` to kubectl commands and `--kube-context <context-name>` to helm commands. For example, with Rancher Desktop:
> ```bash
> kubectl --context rancher-desktop get nodes
> helm --kube-context rancher-desktop list
> ```

## 1. Deploy MinIO (Object Storage)

The agent needs S3-compatible storage. MinIO runs inside the cluster for local development.

```bash
kubectl apply -f environments/local/minio/k8s.yaml
```

This creates the `minio` namespace, a PVC with persistent storage, a Deployment, and Services.

Create the storage bucket — port-forward to MinIO and use the console:

```bash
kubectl port-forward -n minio deploy/minio 9000:9000 9001:9001
```

Open http://localhost:9001/, log in with `minioadmin` / `minioadmin`, and create a bucket called `mcd-agent-storage`.

See [minio/README.md](minio/README.md) for more details and a Docker Compose alternative.

## 2. Create the Agent Namespace

```bash
kubectl create namespace mcd-agent
```

## 3. Create Secrets

The agent requires two secrets to exist before the Helm chart is deployed.

### Agent Token

1. In Monte Carlo, register a new generic agent
2. Click **Generate key** — this produces an `mcd_id` and `mcd_token`
3. Edit `environments/local/secrets/agent-token.json` with those values:

```json
{
  "mcd_id": "<your-mcd-id>",
  "mcd_token": "<your-mcd-token>"
}
```

Then create the secret:

```bash
kubectl create secret generic mcd-agent-token-secret -n mcd-agent \
  --from-file=contents.json=environments/local/secrets/agent-token.json
```

### Integrations Secrets

This secret can be empty for basic testing:

```bash
kubectl create secret generic mcd-integrations-secrets -n mcd-agent \
  --from-file=environments/local/secrets/empty.json
```

## 4. Deploy the Agent with Helm

```bash
helm upgrade --install hermes-agent-dev \
  oci://registry-1.docker.io/montecarlodata/pre-release-generic-agent-helm \
  --version 0.0.1-rc227 \
  -f ./environments/local/values.yaml
```

## 5. Verify

Check the agent pod is running:

```bash
kubectl get pods -n mcd-agent
kubectl logs -n mcd-agent -l app=mcd-agent --tail=30
```

## Updating the Agent Version

To deploy a newer version, update the `--version` flag and the `image.tag` in `values.yaml` to match, then re-run the helm upgrade command.

## Tearing Down

```bash
helm uninstall hermes-agent-dev
kubectl delete namespace mcd-agent
kubectl delete -f environments/local/minio/k8s.yaml
```
