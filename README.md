# GitHub App Installation Token Generator

A small Python utility that authenticates as a GitHub App and generates a
short-lived **installation access token** (the `ghs_`-prefixed token), printing
it to the logs.

## How it works

It runs the standard 3-step GitHub App auth flow:

1. Builds a short-lived JWT signed (RS256) with the app's private key.
2. Calls `GET /app/installations` to find an installation id.
3. Calls `POST /app/installations/{id}/access_tokens` to mint the `ghs_` token.

Installation tokens expire **1 hour** after creation.

> ⚠️ This is a local dev/test utility. It deliberately prints the token to the
> logs — don't do that in production code.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

Configure credentials in the project root:

- `.env` with `GITHUB_APP_ID` (and `GITHUB_CLIENT_ID`).
- Your GitHub App private key as a `*.pem` file. It is auto-detected if there's
  exactly one; otherwise set `GITHUB_APP_PRIVATE_KEY_PATH` to choose one.

Install dependencies:

```bash
uv sync
```

## Run

```bash
uv run main.py
```

Expected output: the number of installations found, the installation id used,
and a token beginning with `ghs_` plus its `expires_at`.

## Verify the token

```bash
curl -H "Authorization: Bearer ghs_..." \
     -H "Accept: application/vnd.github+json" \
     https://api.github.com/installation/repositories
```

Returns the repositories the installation can access.

## Configuration reference

| Variable | Required | Description |
| --- | --- | --- |
| `GITHUB_APP_ID` | yes | The GitHub App's numeric App ID (used as the JWT issuer). |
| `GITHUB_CLIENT_ID` | no | Present for reference; not used by this script. |
| `GITHUB_APP_PRIVATE_KEY_PATH` | no | Explicit path to the `.pem`; overrides auto-detection. |

## Security notes

- `.gitignore` already excludes `*.pem` and `.env`, so credentials aren't committed.
- Tokens are short-lived but still sensitive; treat the logged value as a secret.
