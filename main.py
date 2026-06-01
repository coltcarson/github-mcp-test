"""Generate a GitHub App installation access token (a ``ghs_`` token).

Runs the standard 3-step GitHub App auth flow:

1. Build a short-lived JWT signed (RS256) with the app's private key.
2. List the app's installations to find an installation id.
3. Exchange the JWT for an installation access token (the ``ghs_`` value).

The resulting token is printed to the logs. Installation tokens expire one
hour after creation. This is a local dev/test utility -- avoid logging tokens
in production.
"""

import glob
import logging
import os
import sys
import time

import jwt
import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("github-app-token")

GITHUB_API = "https://api.github.com"
API_VERSION = "2022-11-28"
ACCEPT = "application/vnd.github+json"

# GitHub allows a JWT to live at most 10 minutes; back-date iat to tolerate
# clock skew between this machine and GitHub's servers.
JWT_LIFETIME_SECONDS = 600
JWT_CLOCK_SKEW_SECONDS = 60


def find_private_key_path() -> str:
    """Return the path to the GitHub App private key.

    Uses ``GITHUB_APP_PRIVATE_KEY_PATH`` if set, otherwise auto-detects a
    single ``*.pem`` file in the project root.
    """
    override = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
    if override:
        if not os.path.isfile(override):
            raise FileNotFoundError(
                f"GITHUB_APP_PRIVATE_KEY_PATH points to a missing file: {override}"
            )
        return override

    root = os.path.dirname(os.path.abspath(__file__))
    pem_files = sorted(glob.glob(os.path.join(root, "*.pem")))
    if not pem_files:
        raise FileNotFoundError(
            "No *.pem private key found in the project root. "
            "Add your GitHub App private key, or set GITHUB_APP_PRIVATE_KEY_PATH."
        )
    if len(pem_files) > 1:
        names = ", ".join(os.path.basename(p) for p in pem_files)
        raise RuntimeError(
            f"Multiple *.pem files found ({names}). "
            "Set GITHUB_APP_PRIVATE_KEY_PATH to choose one."
        )
    return pem_files[0]


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def generate_jwt(app_id: str, private_key: str) -> str:
    """Build a JWT signed with the app private key (RS256)."""
    now = int(time.time())
    payload = {
        "iat": now - JWT_CLOCK_SKEW_SECONDS,
        "exp": now + JWT_LIFETIME_SECONDS,
        "iss": app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


def _auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": ACCEPT,
        "X-GitHub-Api-Version": API_VERSION,
    }


def _check(response: requests.Response, action: str) -> None:
    if not response.ok:
        logger.error(
            "%s failed: %s %s -- %s",
            action,
            response.status_code,
            response.reason,
            response.text,
        )
        response.raise_for_status()


def get_installation_id(app_jwt: str) -> int:
    """Return the first installation id for the app."""
    response = requests.get(
        f"{GITHUB_API}/app/installations",
        headers=_auth_headers(app_jwt),
        timeout=30,
    )
    _check(response, "Listing installations")

    installations = response.json()
    if not installations:
        raise RuntimeError(
            "This GitHub App has no installations. "
            "Install it on a user or organization account first."
        )

    logger.info("Found %d installation(s).", len(installations))
    for inst in installations:
        account = inst.get("account") or {}
        logger.info(
            "  installation id=%s account=%s",
            inst.get("id"),
            account.get("login", "<unknown>"),
        )

    installation_id = installations[0]["id"]
    logger.info("Using installation id=%s", installation_id)
    return installation_id


def get_installation_token(app_jwt: str, installation_id: int) -> dict:
    """Create an installation access token (the ``ghs_`` token)."""
    response = requests.post(
        f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
        headers=_auth_headers(app_jwt),
        timeout=30,
    )
    _check(response, "Creating installation access token")
    return response.json()


def main() -> int:
    load_dotenv()

    app_id = require_env("GITHUB_APP_ID")
    private_key_path = find_private_key_path()
    logger.info("Using private key: %s", os.path.basename(private_key_path))

    with open(private_key_path, "r", encoding="utf-8") as fh:
        private_key = fh.read()

    app_jwt = generate_jwt(app_id, private_key)
    installation_id = get_installation_id(app_jwt)
    token_data = get_installation_token(app_jwt, installation_id)

    token = token_data["token"]
    expires_at = token_data.get("expires_at", "<unknown>")

    logger.info("Installation access token (expires_at=%s):", expires_at)
    logger.info("%s", token)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - surface a clean message to the logs
        logger.error("%s", exc)
        sys.exit(1)
