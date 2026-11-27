# Backup and Upgrade

## Backup

Everything Lens stores lives inside `/data` (`lens.db`, `master.key`, `clones/`). Back up `/data` by copying the volume directory. Deleting `lens.db` snapshots loses no repository state — everything rebuilds from git.

## Upgrade

Pull the newer image and restart the container:

```bash
docker pull ghcr.io/groundwork/groundwork-lens:1
docker stop groundwork-lens && docker rm groundwork-lens
docker run -d --name groundwork-lens -p 8080:8080 -v lens-data:/data ghcr.io/groundwork/groundwork-lens:1
```

Before applying any database schema migration, Lens automatically copies `/data/lens.db` to `/data/backups/`.
