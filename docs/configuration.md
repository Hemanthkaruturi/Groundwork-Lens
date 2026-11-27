# Configuration Reference

Settings configured in the browser wizard can be overridden by environment variables (shown as read-only in the wizard):

| Variable | Default | Description |
| --- | --- | --- |
| `LENS_DATA_DIR` | `/data` | Directory holding `lens.db`, `master.key`, backups, and git clones |
| `LENS_HOST` | `0.0.0.0` | Listen address |
| `LENS_PORT` | `8080` | Listen port |
| `LENS_PUBLIC_URL` | `http://localhost:8080` | External URL used for OAuth callbacks and webhooks |
| `LENS_POLL_INTERVAL` | `300` | Poll interval in seconds (minimum 60) |
| `LENS_PR_CAP` | `20` | Maximum open PRs exported per repository |
| `LENS_IDLE_DAYS` | `14` | Default days before showing "no activity for N days" |
| `LENS_MASTER_KEY` | Auto-generated in `/data/master.key` | Fernet encryption key for SQLite secrets |
| `LENS_SESSION_SECRET` | Auto-generated | HMAC secret for CSRF tokens |
| `LENS_SETUP_CODE` | Auto-generated on first run | Optional fixed setup code for automated deployments |
