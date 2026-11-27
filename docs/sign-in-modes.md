# Sign-in Modes

| Mode | Who sets it up | How users sign in | How Lens reads repos | Who opens request PRs |
| --- | --- | --- | --- | --- |
| **A. Full login app (default)** | Group/org owner | GitHub or GitLab OAuth | App installation token (GitHub) or service token (GitLab) | User token; Lens identity if user cannot push |
| **B. Profile-only + service token** | Repo maintainer | Profile-only OAuth or one-time `read_user` token (discarded immediately) | Scoped project/group service token | Service token (`Requested-by:` recorded) |
| **C. No login** | Repo maintainer | None (trusted internal network) | Service token | Service token; approvals & edits disabled |
