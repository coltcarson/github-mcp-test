"""Use a GitHub App installation token with GitHub's remote MCP server.

The program signs a short-lived GitHub App JWT, exchanges it for an
installation access token restricted to one repository, initializes a
read-only MCP session, and calls the ``search_pull_requests`` tool.
"""

import json
import logging
import os
import re
import sys
import time
from argparse import ArgumentParser, Namespace
from typing import Any

import jwt
import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("github-app-mcp")

GITHUB_API = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_ACCEPT = "application/vnd.github+json"
MCP_ENDPOINT = "https://api.githubcopilot.com/mcp/"
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_TOOL = "search_pull_requests"

JWT_LIFETIME_SECONDS = 600
JWT_CLOCK_SKEW_SECONDS = 60
REPOSITORY_PATTERN = re.compile(
    r"^(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)$"
)


def require_env(name: str) -> str:
    """Return a required environment variable without logging its value."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def validate_repository(value: str) -> tuple[str, str]:
    """Validate and split an ``owner/repository`` argument."""
    match = REPOSITORY_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError("--repo must use the owner/repository format.")
    return match.group("owner"), match.group("repo")


def read_private_key() -> str:
    """Read the explicitly configured private key without exposing its path."""
    path = require_env("GITHUB_APP_PRIVATE_KEY_PATH")
    if not os.path.isfile(path):
        raise RuntimeError(
            "GITHUB_APP_PRIVATE_KEY_PATH does not reference a readable file."
        )
    try:
        with open(path, "r", encoding="utf-8") as private_key_file:
            return private_key_file.read()
    except OSError as exc:
        raise RuntimeError("Unable to read the configured GitHub App private key.") from exc


def generate_jwt(app_id: str, private_key: str) -> str:
    """Build a short-lived RS256 JWT for GitHub App authentication."""
    now = int(time.time())
    payload = {
        "iat": now - JWT_CLOCK_SKEW_SECONDS,
        "exp": now + JWT_LIFETIME_SECONDS,
        "iss": app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": GITHUB_ACCEPT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


def _mcp_headers(token: str, session_id: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "X-MCP-Readonly": "true",
        "X-MCP-Tools": MCP_TOOL,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return headers


def _check_response(response: requests.Response, action: str) -> None:
    """Raise a sanitized HTTP error without logging response or request bodies."""
    if response.ok:
        return
    reason = response.reason or "request failed"
    raise RuntimeError(f"{action} failed with HTTP {response.status_code} {reason}.")


def get_installation_token(
    app_jwt: str,
    installation_id: str,
    repository_name: str,
) -> dict[str, Any]:
    """Mint a token limited to one repository and read-only PR access."""
    response = requests.post(
        f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
        headers=_github_headers(app_jwt),
        json={
            "repositories": [repository_name],
            "permissions": {"pull_requests": "read"},
        },
        timeout=30,
    )
    _check_response(response, "Creating the installation access token")
    payload = response.json()
    if not isinstance(payload.get("token"), str) or not payload["token"]:
        raise RuntimeError("GitHub returned an invalid installation token response.")
    return payload


def _parse_mcp_message(response: requests.Response) -> dict[str, Any] | None:
    """Parse a plain JSON or server-sent-event MCP response."""
    if not response.text.strip():
        return None

    if "text/event-stream" not in response.headers.get("Content-Type", ""):
        return response.json()

    normalized = response.text.replace("\r\n", "\n").replace("\r", "\n")
    messages: list[dict[str, Any]] = []
    for event in normalized.split("\n\n"):
        data_lines = [
            line[len("data:") :].lstrip(" ")
            for line in event.split("\n")
            if line.startswith("data:")
        ]
        if data_lines:
            messages.append(json.loads("\n".join(data_lines)))
    return messages[-1] if messages else None


def _mcp_result(response: requests.Response, action: str) -> dict[str, Any]:
    _check_response(response, action)
    message = _parse_mcp_message(response)
    if not message:
        raise RuntimeError(f"{action} returned an empty MCP response.")
    if "error" in message:
        error = message["error"]
        code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
        raise RuntimeError(f"{action} returned MCP error code {code}.")
    result = message.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"{action} returned an invalid MCP result.")
    return result


def mcp_initialize(token: str) -> str:
    """Complete the MCP handshake and return its session identifier."""
    logger.info("Initializing the remote GitHub MCP session.")
    response = requests.post(
        MCP_ENDPOINT,
        headers=_mcp_headers(token),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "github-app-remote-mcp-demo", "version": "1.0"},
            },
        },
        timeout=30,
    )
    _mcp_result(response, "MCP initialize")

    session_id = response.headers.get("Mcp-Session-Id")
    if not session_id:
        raise RuntimeError("MCP initialize did not return a session identifier.")

    notification = requests.post(
        MCP_ENDPOINT,
        headers=_mcp_headers(token, session_id),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        timeout=30,
    )
    _check_response(notification, "MCP initialized notification")
    return session_id


def mcp_list_tools(token: str, session_id: str) -> list[dict[str, Any]]:
    """List tools exposed by the restricted MCP session."""
    response = requests.post(
        MCP_ENDPOINT,
        headers=_mcp_headers(token, session_id),
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        timeout=30,
    )
    result = _mcp_result(response, "MCP tools/list")
    tools = result.get("tools", [])
    if not isinstance(tools, list):
        raise RuntimeError("MCP tools/list returned an invalid tool collection.")
    return tools


def mcp_search_closed_pull_requests(
    token: str,
    session_id: str,
    repository: str,
) -> dict[str, Any]:
    """Call the MCP search tool for closed pull requests in one repository."""
    response = requests.post(
        MCP_ENDPOINT,
        headers=_mcp_headers(token, session_id),
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": MCP_TOOL,
                "arguments": {
                    "query": f"repo:{repository} is:pr state:closed",
                    "perPage": 100,
                },
            },
        },
        timeout=60,
    )
    result = _mcp_result(response, f"MCP tools/call {MCP_TOOL}")
    for block in result.get("content", []):
        if block.get("type") == "text":
            parsed = json.loads(block.get("text", "{}"))
            return parsed if isinstance(parsed, dict) else {"items": []}
    return {"items": []}


def print_pull_requests(search_result: dict[str, Any]) -> None:
    """Print a concise, credential-free result summary."""
    items = search_result.get("items", [])
    total = search_result.get("total_count", len(items))
    logger.info("Found %s closed pull request(s).", total)
    for pull_request in items:
        logger.info(
            "#%-5s %s",
            pull_request.get("number", "?"),
            pull_request.get("title", "<untitled>"),
        )


def run(repository: str) -> int:
    _, repository_name = validate_repository(repository)
    app_id = require_env("GITHUB_APP_ID")
    installation_id = require_env("GITHUB_INSTALLATION_ID")
    private_key = read_private_key()

    app_jwt = generate_jwt(app_id, private_key)
    token_data = get_installation_token(
        app_jwt,
        installation_id,
        repository_name,
    )
    installation_token = token_data["token"]
    logger.info(
        "Acquired an installation token (expires_at=%s).",
        token_data.get("expires_at", "<unknown>"),
    )

    session_id = mcp_initialize(installation_token)
    tools = mcp_list_tools(installation_token, session_id)
    advertised_names = {
        tool.get("name") for tool in tools if isinstance(tool, dict)
    }
    if MCP_TOOL not in advertised_names:
        raise RuntimeError(
            f"The remote MCP server did not advertise the required {MCP_TOOL} tool."
        )

    search_result = mcp_search_closed_pull_requests(
        installation_token,
        session_id,
        repository,
    )
    print_pull_requests(search_result)
    return 0


def parse_args() -> Namespace:
    parser = ArgumentParser(
        description=(
            "Search closed pull requests through GitHub's remote MCP server "
            "using a GitHub App installation token."
        )
    )
    parser.add_argument(
        "--repo",
        required=True,
        metavar="OWNER/REPOSITORY",
        help="Repository installed for the GitHub App.",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv(override=False)
    args = parse_args()
    return run(args.repo)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - return a concise CLI error
        logger.error("%s", exc)
        sys.exit(1)
