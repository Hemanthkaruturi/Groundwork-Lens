# GitLab Setup

1. Open **Group → Settings → Applications** (or **User Settings → Applications**).
2. Enter:
   - **Name**: `GroundWork Lens`
   - **Redirect URI**: `https://<lens-address>/auth/gitlab/callback`
   - **Confidential**: Checked
   - **Device authorization grant**: Unchecked
   - **Scopes**: `read_user`, `read_api` (add `api` if requests should be opened under the user's own token)
3. Paste the **Application ID** and **Secret** into the setup wizard.
4. Create a Group or Project Access Token with `read_api`, `read_repository`, and `api` scopes and paste it into the service token field.
