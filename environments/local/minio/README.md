# MinIO Local Setup

## Option 1: Docker Compose

Start MinIO:
```bash
docker compose up -d
```

Create the bucket using the MinIO client:
```bash
docker run --rm --network=host -e MC_HOST_local=http://minioadmin:minioadmin@localhost:9000 quay.io/minio/mc mb local/mcd-agent-storage --ignore-existing
```

Or open the console at http://localhost:9001/, login with `minioadmin`/`minioadmin`, and create the bucket manually.

Agent config for Docker Compose:
```yaml
  storageBucketName: "mcd-agent-storage"
  storageType: "S3_COMPATIBLE"
  storageEndpointUrl: "http://host.docker.internal:9000"
  storageAccessKey: "minioadmin"
  storageSecretKey: "minioadmin"
```

## Option 2: Kubernetes (Rancher Desktop / k3s)

Deploy MinIO with persistent storage:
```bash
kubectl --context rancher-desktop apply -f environments/local/minio/k8s.yaml
```

This creates a Namespace, PVC (using k3s `local-path` StorageClass), Deployment, and Services.
Data persists across pod restarts.

Port-forward to access the console:
```bash
kubectl --context rancher-desktop port-forward -n minio deploy/minio 9000:9000 9001:9001
```

Open console at http://localhost:9001/, login with `minioadmin`/`minioadmin`, and create a bucket called `mcd-agent-storage`.

Agent config for Kubernetes:
```yaml
  storageBucketName: "mcd-agent-storage"
  storageType: "S3_COMPATIBLE"
  storageEndpointUrl: "http://minio-api.minio.svc.cluster.local:9000"
  storageAccessKey: "minioadmin"
  storageSecretKey: "minioadmin"
```
