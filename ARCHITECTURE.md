# Architecture — GroundWork Lens

## Core Boundaries

1. **Bundled GroundWork Engine (`engine/`)**: Standard-library-only Python CLI (`groundwork.py`) that parses GroundWork repositories and produces schema-versioned JSON snapshots (`export --json`).
2. **Host Adapters (`lens/hosts/`)**: Isolated HTTP/Git boundary for GitHub, GitLab, and local git test/demo repositories.
3. **Sync Worker (`lens/sync.py`)**: Performs blobless sparse clones/fetches and runs the bundled engine in an isolated subprocess to update the SQLite snapshot cache.
4. **Snapshot Store (`lens/store.py`)**: SQLite cache storing encrypted tokens, tracked projects, sessions, and JSON snapshots.
5. **Web App (`lens/app.py`)**: Server-rendered Jinja2 HTML pages reading snapshots only, plus request and approval forms delegating writes back through the engine and host adapter.
