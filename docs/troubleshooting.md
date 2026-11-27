# Troubleshooting

Check **Health (`/admin/health`)** first. Common messages:

- **"The redirect URI in GitLab does not match..."**: Confirm `LENS_PUBLIC_URL` matches your browser URL and update the GitLab application's Redirect URI to `<public-url>/auth/gitlab/callback`.
- **"GitHub rate limit" / "GitLab rate limit"**: Lens backs off automatically and resumes polling after the reset window. Configure webhooks to avoid polling calls.
- **"This repo uses GroundWork ...; Lens needs ..."**: Upgrade the GroundWork plugin in the repository or pull a compatible Lens image.
- **"Refused direct push"**: The branch Lens reads from is protected. Lens opened a fallback PR/MR automatically; grant bypass permissions or review/merge the PR.
- **"Service token expires in N days"**: Rotate the project/group token in `/setup`.
