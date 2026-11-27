# Reverse Proxy Setup

Always terminate TLS in front of Lens in production and forward `Host`, `X-Forwarded-Proto`, and `X-Forwarded-Host`.

Example configs are provided in `deploy/`:

- `deploy/Caddyfile`
- `deploy/nginx.conf`
- `deploy/traefik.yml`
- `deploy/kubernetes.yaml`
