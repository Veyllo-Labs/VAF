# GitHub Integration

VAF links to your GitHub account to enable the agent to list repositories, read code, and manage issues or pull requests. By default, VAF uses the **GitHub Device Flow**, which is the most secure and reliable method for local applications.

## Setup Methods

VAF offers three ways to connect your GitHub account, ranked by recommendation:

### 1. Device Login (Recommended)
This is the standard flow for local agents. It does not require a complex "Redirect URI" setup.
1. Go to **Settings → Connections → Developer → GitHub**.
2. Click **Connect** and choose **Device Login**.
3. VAF displays an 8-character code.
4. **Click the code**: It will be copied to your clipboard, and GitHub's verification page will open automatically.
5. Paste the code on GitHub and authorize. VAF will link the account instantly.

### 2. Personal Access Token (PAT)
For users who want granular control over permissions without using OAuth.
1. Generate a token at [GitHub Settings → Developer settings → Personal access tokens](https://github.com/settings/tokens).
2. Ensure you grant the `repo` scope (or `public_repo` for read-only).
3. Paste the token into the VAF GitHub Wizard.

### 3. Browser Login (Legacy)
The classic OAuth redirect flow. This often requires additional network configuration (matching ports and IPs) to work correctly in local environments.

## Admin Configuration (Once per Instance)

To enable OAuth-based logins (Device or Browser), an admin must register an application on GitHub:

1. Create a **GitHub OAuth App** at [GitHub Developer Settings](https://github.com/settings/developers).
2. **Crucial Step:** Scroll to the bottom of your App settings and check **"Enable Device Flow"**.
3. Set the **Authorization callback URL** to `http://127.0.0.1:8001/api/github/oauth/callback` (or your specific backend port).
4. Copy the **Client ID** and save it in VAF Settings. 
5. *Note: A Client Secret is only required if you use the Legacy Browser Login.*

## Architecture: How Device Flow Works

Unlike standard OAuth, the Device Flow is designed for "headless" or local devices:
1. **Request:** VAF requests a unique code from GitHub using only the Client ID.
2. **Display:** VAF shows the `user_code` and provides a link to GitHub.
3. **Polling:** While you enter the code on GitHub, VAF "polls" (checks) GitHub's servers every few seconds.
4. **Completion:** Once you authorize on GitHub, the next poll returns the access token. VAF stores this token securely in your local OS keyring.

## Scopes and Permissions

- **Standard:** Requests `read:user`, `user:email`, and `repo`. This allows the agent full access to your repositories.
- **Privacy:** Tokens are stored locally and encrypted. They never leave your machine except to communicate directly with the GitHub API.

## Dashboard (Settings → Connections → GitHub)

Opening the GitHub dashboard from the Connections panel shows:

- **Rights overview:** A strip below the header lists each connected account with a quick toggle between read-only and write access (commit/push). You can change permissions without opening each account card.
- **Connected accounts:** Left column lists accounts with avatar, scopes, and per-card permission controls.
- **Event timeline:** Left side shows a chronological list of agent actions (e.g. read, edit, commit, push), newest first. Each entry shows time, action type, and a short detail line.
- **Repositories:** Right column shows repositories for the selected connected account (name, description, stars, last updated, link to GitHub). With multiple accounts, a dropdown switches the account. Data is loaded from the GitHub API via `GET /api/github/repos?account_id=...`.

## Tools in the Web UI

The six GitHub tools (e.g., `github_list_repos`, `github_get_file`) are automatically discovered at startup. They appear in **Settings → Advanced → Tools** only if the **PyGithub** package is installed in the Python environment used by VAF. 

VAF uses a **robust loading mechanism**: if the GitHub module fails to load (e.g., due to a missing dependency), VAF will log a `[WARN]` message but continue loading all other tools.

### Enabling GitHub Tools
1. **Verify Installation:** Ensure `PyGithub` is installed: `pip install PyGithub`.
2. **Restart VAF:** The agent scans for tools only during the initialization phase.
3. **Check UI:** Open **Settings → Advanced → Tools**. If they are still missing, check the console/logs for a warning like `[WARN] GitHub tools module ... failed to load`.

## Troubleshooting

- **"Device Flow must be explicitly enabled"**  
  You must enable "Device Flow" in your GitHub OAuth App settings (Step 2 in Admin Configuration).

- **"Redirect URI mismatch"**  
  This only affects the **Browser Login (Legacy)**. Switch to **Device Login** to avoid this error.

- **"GitHub OAuth client ID not configured"**  
  Ensure an admin has entered the Client ID in the Connections tab.

- **GitHub tools missing from the Tools list**  
  The most common cause is a missing `PyGithub` package. VAF isolates tool loading, so a failure here won't crash the app, but the tools will be skipped. Install the dependency and restart. You can click the **Refresh** icon in the Tools modal to force the UI to refetch the tool list from the running agent.
