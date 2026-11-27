# GitHub Setup

GroundWork Lens uses a **GitHub App** on GitHub.com or GitHub Enterprise Server.

## Automatic setup (recommended)

In the browser setup wizard, click **Create GitHub App automatically**. Lens posts a manifest with:

- Homepage URL: `<lens-address>`
- Callback URL: `<lens-address>/auth/github/callback`
- Repository permissions: `Contents: Read & write`, `Pull requests: Read & write`, `Metadata: Read-only`
- Account permissions: `Email addresses: Read-only`
- Webhook: `<lens-address>/webhooks/github`, only when Lens has a publicly reachable address (see below)

### Who can install the app

By default the app is **private**: only the GitHub account that creates it can install it. Tick **Let other GitHub accounts and organizations install this app** before creating it if:

- people will sign in with a different GitHub account than the one creating the app, or
- your GroundWork repositories belong to another account or organization.

To change this later, open the app's settings on GitHub, then **Advanced → Make public**. Making the app public only lets others install it; it gets no access to anything until someone installs it, and only on the repositories they choose.

### Install it on every repository Lens should read

After clicking **Create GitHub App** on GitHub, install the app (the setup page links to the install screen) and choose the repositories Lens should show.

For a **GroundWork workspace**, include the workspace repository **and every sub-repo** listed in its `.gitmodules`. A sub-repo the app is not installed on shows as "The GroundWork Lens GitHub App is not installed on …" on the project page. Add it under the app's **Repository access** on GitHub; Lens picks it up on its next sync, or immediately with **Refresh now**.

### Running Lens on localhost or a private network

GitHub refuses App manifests whose webhook points at `localhost` or a private address. When `LENS_PUBLIC_URL` is not publicly reachable, Lens creates the app **without a webhook** and checks repositories on a timer instead (`LENS_POLL_INTERVAL`, default 5 minutes). Sign-in works normally from `http://localhost:8080`.

## Fine-grained PAT (Modes B and C)

Create a fine-grained personal access token scoped to the GroundWork repositories with `Contents: Read & write` and `Pull requests: Read & write`, and set an expiry date. Lens warns on `/admin/health` 14 days before expiry.
