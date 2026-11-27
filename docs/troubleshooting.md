# Troubleshooting

Check **Health (`/admin/health`)** first. Common messages:

- **"The redirect URI in GitLab does not match..."**: Confirm `LENS_PUBLIC_URL` matches your browser URL and update the GitLab application's Redirect URI to `<public-url>/auth/gitlab/callback`.
- **"GitHub rate limit" / "GitLab rate limit"**: Lens backs off automatically and resumes polling after the reset window. Configure webhooks to avoid polling calls.
- **"This repo uses GroundWork ...; Lens needs ..."**: Upgrade the GroundWork plugin in the repository or pull a compatible Lens image.
- **"Refused direct push"**: The branch Lens reads from is protected. Lens opened a fallback PR/MR automatically; grant bypass permissions or review/merge the PR.
- **"Service token expires in N days"**: Rotate the project/group token in `/setup`.
- **"Hook url is not supported because it isn't reachable over the public Internet"** (on GitHub, while creating the app): you are on an older Lens. Current versions leave the webhook out for local and private addresses; see [GitHub Setup](github-setup.md#running-lens-on-localhost-or-a-private-network).
- **"GroundWork Lens is a private GitHub App"** (when installing with another account): the app was created private. Make it public in the app's settings on GitHub (**Advanced → Make public**), then install it from the other account.
- **"The GroundWork Lens GitHub App is not installed on owner/repo"** (on a workspace page): add that sub-repo to the app's **Repository access** on GitHub. Lens retries failed sub-repos on every sync, or click **Refresh now**.
- **"Not checked yet" when choosing projects**: Lens has not read that repository yet. Follow it and Lens checks for GroundWork on the first sync.
