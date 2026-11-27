# Install GroundWork Lens

Run a single container with persistent storage at `/data`:

```bash
docker run -d \
  --name groundwork-lens \
  -p 8080:8080 \
  -v lens-data:/data \
  ghcr.io/groundwork/groundwork-lens:1
```

On first start, check the logs for your one-time setup code:

```bash
docker logs groundwork-lens
```

Open `http://localhost:8080`, enter the code, and follow the browser setup wizard.
