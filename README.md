# GroundWork Lens

A self-hosted web view for GroundWork projects on GitHub and GitLab. Designed for leaders and product managers on a phone: see what is waiting on a person, what needs approval, what is being built, and which bugs are open — without opening a repository or using an LLM.

## Quick start (one command, no config file)

```bash
docker run -d --name groundwork-lens -p 8080:8080 -v lens-data:/data ghcr.io/groundwork/groundwork-lens:1
```

1. Read the one-time setup code from container logs:
   ```bash
   docker logs groundwork-lens
   ```
2. Open `http://localhost:8080` in your browser, enter the setup code, and follow the setup wizard (or click **Try the demo** to explore the bundled sample workspace immediately).

## GitHub setup (Mode A, default)

1. In the setup wizard, confirm your public address and click **Create GitHub App automatically**.
2. Review the pre-filled permissions on GitHub (`Contents: Read & write`, `Pull requests: Read & write`, `Metadata: Read-only`, `Email addresses: Read-only`) and click **Create GitHub App**.
3. Install the app on the repositories you want Lens to show, then click **Test connection** in the wizard.

## GitLab setup (Mode A, default)

1. In GitLab, open **Group → Settings → Applications** (or **User Settings → Applications**) and click **Add new application**.
2. Copy the **Name**, **Redirect URI** (`https://<your-lens-address>/auth/gitlab/callback`), **Confidential** (`checked`), and **Scopes** (`read_user`, `read_api`) shown in the setup wizard.
3. Paste the Application ID and Secret back into the wizard, add a project/group service token (`read_api`, `read_repository`, `api`), and click **Test connection**.

## Documentation

- [Install & Docker](docs/install.md)
- [GitHub Setup](docs/github-setup.md)
- [GitLab Setup](docs/gitlab-setup.md)
- [Sign-in Modes (A, B, C)](docs/sign-in-modes.md)
- [Reverse Proxy (Caddy, nginx, Traefik)](docs/reverse-proxy.md)
- [Backup & Upgrade](docs/backup-and-upgrade.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Configuration Reference](docs/configuration.md)
- [Export Schema v1.0](docs/schema-v1.md)

## Running tests locally

```bash
python3 -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
