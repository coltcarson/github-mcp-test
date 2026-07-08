# 🔐 GitHub App Remote MCP Demo

This repository is a minimal, read-only demonstration of this authentication
and tool-call sequence:

```text
GitHub App
  → signed app JWT
  → repository-scoped installation access token
  → remote GitHub MCP session
  → search_pull_requests
```

The Python client never prints the JWT, installation token, private key,
authorization header, or HTTP response body.

## 📋 Prerequisites

- A GitHub account that can create and install a GitHub App.
- One disposable or approved repository for the demonstration.
- Python 3.11 or later.
- [uv](https://docs.astral.sh/uv/).

## 1. 🛠️ Create a least-privilege GitHub App

1. Open **Settings → Developer settings → GitHub Apps → New GitHub App**.
2. Give the app a unique name and use an appropriate internal or project URL
   for its homepage.
3. Disable **Webhook → Active**. This example does not receive webhooks.
4. Under **Repository permissions**, set **Pull requests** to **Read-only**.
   Leave every other optional permission at **No access**.
5. Create the app and record its numeric **App ID**. The App ID is configuration,
   not a secret.

GitHub automatically grants the metadata access required for an installation.
Do not add write permissions for this demo.

## 2. 📦 Install the app on one repository

1. On the app settings page, select **Install App**.
2. Choose the intended user or organization.
3. Select **Only select repositories** and choose one disposable or approved
   repository.
4. Complete the installation.
5. Record the numeric installation ID from the installation settings URL. It is
   the number after `/settings/installations/`.

The owner in the `--repo owner/repository` argument must match the account where
this installation exists.

## 3. 🔑 Generate and store a private key

1. Return to the app's **General** settings.
2. Under **Private keys**, select **Generate a private key**.
3. Move the downloaded PEM file outside this repository.
4. Restrict it to the current operating-system user:

   ```bash
   chmod 600 /absolute/path/to/app.private-key.pem
   ```

Never commit, paste, upload, or log this file. If a private key may have been
exposed, generate its replacement, verify the replacement, and then delete the
old key in the GitHub App settings.

## 4. ⚙️ Configure the demo

Copy the safe placeholder file:

```bash
cp .env.example .env
```

Set these values in the ignored `.env` file:

```dotenv
GITHUB_APP_ID=123456
GITHUB_INSTALLATION_ID=12345678
GITHUB_APP_PRIVATE_KEY_PATH=/absolute/path/outside/this/repository/app.private-key.pem
```

Environment variables supplied by your shell or secret manager take precedence
over `.env`.

## 5. 🚀 Install and run

```bash
uv sync
uv run main.py --repo owner/repository
```

The command reports the installation-token expiration time and lists closed
pull requests. It does not display the token. Installation tokens expire after
one hour.

## 🔄 What the client does

1. Signs an RS256 JSON Web Token using the App ID and private key.
2. Sends the JWT to
   `POST /app/installations/{installation_id}/access_tokens`.
3. Requests a token limited to the named repository and
   `pull_requests: read`.
4. Sends that opaque token as an `Authorization: Bearer` credential to
   `https://api.githubcopilot.com/mcp/`.
5. Completes MCP `initialize` and `notifications/initialized`.
6. Calls `tools/list` and verifies that `search_pull_requests` is available.
7. Calls `tools/call` with:

   ```text
   repo:owner/repository is:pr state:closed
   ```

Every MCP request also sends `X-MCP-Readonly: true` and limits the advertised
tools to `search_pull_requests`.

GitHub currently describes remote MCP authentication primarily in terms of
OAuth and personal access tokens. This project demonstrates the same Bearer
header with a short-lived GitHub App installation token. Run the live smoke test
against your environment before depending on that behavior in production.

Do not validate installation tokens by length or by a fixed token pattern.
GitHub began rolling out a new stateless installation-token format in 2026;
clients should treat the returned value as opaque.

## 🧪 Tests and security checks

Unit tests use mocked HTTP responses and do not require credentials:

```bash
uv run python -m unittest discover -s tests -v
```

Run the local secret checks with:

```bash
gitleaks git --redact --verbose .
gitleaks dir --redact --verbose .
git status --short
```

The repository also runs a full-history Gitleaks scan for pushes and pull
requests. Organization-owned repositories may require a Gitleaks license secret;
see the action's documentation before enabling the workflow.

## 📚 Official references

- [Authenticating as a GitHub App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app)
- [Generating an installation access token](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
- [Remote GitHub MCP server](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md)
