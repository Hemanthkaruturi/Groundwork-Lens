# GitHub Setup

GroundWork Lens uses a **GitHub App** on GitHub.com or GitHub Enterprise Server.

## Automatic setup (recommended)

In the browser setup wizard, click **Create GitHub App automatically**. Lens posts a manifest with:

- Homepage URL: `https://<lens-address>`
- Callback URL: `https://<lens-address>/auth/github/callback`
- Repository permissions: `Contents: Read & write`, `Pull requests: Read & write`, `Metadata: Read-only`
- Account permissions: `Email addresses: Read-only`
- Webhook events: `push`, `pull_request` (when publicly reachable)

After clicking **Create GitHub App** on GitHub, install the app on the repositories you want Lens to display.

## Fine-grained PAT (Modes B and C)

Create a fine-grained personal access token scoped to the GroundWork repositories with `Contents: Read & write` and `Pull requests: Read & write`, and set an expiry date. Lens warns on `/admin/health` 14 days before expiry.
