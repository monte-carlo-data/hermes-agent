### Start minio
```bash
docker compose up -d
```

### Create the minio bucket
```bash
docker run --rm --network=host -e MC_HOST_local=http://minioadmin:minioadmin@localhost:9000 quay.io/minio/mc mb local/mcd-agent-storage --ignore-existing
```