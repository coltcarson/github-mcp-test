import json
import os
import unittest
from unittest.mock import patch

import main


class FakeResponse:
    def __init__(
        self,
        *,
        status_code=200,
        reason="OK",
        payload=None,
        text=None,
        headers=None,
    ):
        self.status_code = status_code
        self.reason = reason
        self.ok = 200 <= status_code < 300
        self._payload = payload
        self.text = (
            json.dumps(payload)
            if text is None and payload is not None
            else text or ""
        )
        self.headers = headers or {}

    def json(self):
        return self._payload


class ConfigurationTests(unittest.TestCase):
    def test_require_env_rejects_missing_value(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                RuntimeError, "Missing required environment variable: REQUIRED"
            ):
                main.require_env("REQUIRED")

    def test_repository_validation(self):
        self.assertEqual(
            main.validate_repository("octo-org/example.repo"),
            ("octo-org", "example.repo"),
        )
        with self.assertRaisesRegex(ValueError, "owner/repository"):
            main.validate_repository("not-a-repository")

    def test_missing_private_key_error_does_not_expose_path(self):
        private_path = "/private/location/app-secret.pem"
        with patch.dict(
            os.environ,
            {"GITHUB_APP_PRIVATE_KEY_PATH": private_path},
            clear=True,
        ):
            with patch("main.os.path.isfile", return_value=False):
                with self.assertRaises(RuntimeError) as raised:
                    main.read_private_key()

        self.assertNotIn(private_path, str(raised.exception))

    @patch("main.jwt.encode", return_value="signed-jwt")
    @patch("main.time.time", return_value=1_800_000_000)
    def test_generate_jwt_uses_expected_claims(self, _time, encode):
        token = main.generate_jwt("12345", "private-key-material")

        self.assertEqual(token, "signed-jwt")
        payload = encode.call_args.args[0]
        self.assertEqual(payload["iss"], "12345")
        self.assertEqual(
            payload["iat"], 1_800_000_000 - main.JWT_CLOCK_SKEW_SECONDS
        )
        self.assertEqual(
            payload["exp"], 1_800_000_000 + main.JWT_LIFETIME_SECONDS
        )
        self.assertEqual(encode.call_args.kwargs["algorithm"], "RS256")


class GitHubTokenTests(unittest.TestCase):
    @patch("main.requests.post")
    def test_token_is_opaque_and_restricted(self, post):
        post.return_value = FakeResponse(
            payload={
                "token": "opaque-installation-credential",
                "expires_at": "2026-07-08T22:00:00Z",
            }
        )

        result = main.get_installation_token("app-jwt", "9876", "demo-repo")

        self.assertEqual(result["token"], "opaque-installation-credential")
        request = post.call_args
        self.assertEqual(
            request.args[0],
            "https://api.github.com/app/installations/9876/access_tokens",
        )
        self.assertEqual(
            request.kwargs["json"],
            {
                "repositories": ["demo-repo"],
                "permissions": {"pull_requests": "read"},
            },
        )
        self.assertEqual(
            request.kwargs["headers"]["Authorization"], "Bearer app-jwt"
        )

    def test_http_failure_does_not_expose_body(self):
        response = FakeResponse(
            status_code=401,
            reason="Unauthorized",
            text="sensitive-response-material",
        )
        with self.assertRaises(RuntimeError) as raised:
            main._check_response(response, "Token request")

        message = str(raised.exception)
        self.assertIn("HTTP 401 Unauthorized", message)
        self.assertNotIn("sensitive-response-material", message)


class McpTests(unittest.TestCase):
    def test_parse_sse_message(self):
        response = FakeResponse(
            text='event: message\ndata: {"jsonrpc":"2.0",\ndata: "result":{"tools":[]}}\n\n',
            headers={"Content-Type": "text/event-stream"},
        )
        self.assertEqual(
            main._parse_mcp_message(response),
            {"jsonrpc": "2.0", "result": {"tools": []}},
        )

    @patch("main.requests.post")
    def test_initialize_propagates_restricted_headers_and_session(self, post):
        post.side_effect = [
            FakeResponse(
                payload={"jsonrpc": "2.0", "id": 1, "result": {}},
                headers={"Mcp-Session-Id": "session-123"},
            ),
            FakeResponse(status_code=202),
        ]

        with self.assertLogs(main.logger, level="INFO") as logs:
            session_id = main.mcp_initialize("opaque-token")

        self.assertEqual(session_id, "session-123")
        initialize_headers = post.call_args_list[0].kwargs["headers"]
        notification_headers = post.call_args_list[1].kwargs["headers"]
        self.assertEqual(initialize_headers["X-MCP-Readonly"], "true")
        self.assertEqual(initialize_headers["X-MCP-Tools"], "search_pull_requests")
        self.assertEqual(notification_headers["Mcp-Session-Id"], "session-123")
        self.assertNotIn("opaque-token", "\n".join(logs.output))

    @patch("main.requests.post")
    def test_list_tools_uses_active_session(self, post):
        post.return_value = FakeResponse(
            payload={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"tools": [{"name": "search_pull_requests"}]},
            }
        )

        tools = main.mcp_list_tools("opaque-token", "session-123")

        self.assertEqual(tools, [{"name": "search_pull_requests"}])
        request = post.call_args
        self.assertEqual(request.kwargs["json"]["method"], "tools/list")
        self.assertEqual(
            request.kwargs["headers"]["Mcp-Session-Id"], "session-123"
        )

    @patch("main.requests.post")
    def test_search_calls_expected_read_only_tool(self, post):
        tool_payload = {
            "total_count": 1,
            "items": [{"number": 7, "title": "Example"}],
        }
        post.return_value = FakeResponse(
            payload={
                "jsonrpc": "2.0",
                "id": 3,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(tool_payload)}]
                },
            }
        )

        result = main.mcp_search_closed_pull_requests(
            "opaque-token", "session-123", "octo-org/demo"
        )

        self.assertEqual(result, tool_payload)
        params = post.call_args.kwargs["json"]["params"]
        self.assertEqual(params["name"], "search_pull_requests")
        self.assertEqual(
            params["arguments"]["query"],
            "repo:octo-org/demo is:pr state:closed",
        )


class LoggingTests(unittest.TestCase):
    @patch("main.mcp_search_closed_pull_requests")
    @patch("main.mcp_list_tools")
    @patch("main.mcp_initialize", return_value="session-id")
    @patch("main.get_installation_token")
    @patch("main.generate_jwt", return_value="app-jwt")
    @patch("main.read_private_key", return_value="private-key-material")
    def test_run_logs_no_credentials(
        self,
        _read_key,
        _generate,
        token,
        _initialize,
        list_tools,
        search,
    ):
        credential = "opaque-installation-credential"
        token.return_value = {
            "token": credential,
            "expires_at": "2026-07-08T22:00:00Z",
        }
        list_tools.return_value = [{"name": "search_pull_requests"}]
        search.return_value = {"total_count": 0, "items": []}

        with patch.dict(
            os.environ,
            {
                "GITHUB_APP_ID": "123",
                "GITHUB_INSTALLATION_ID": "456",
            },
            clear=True,
        ):
            with self.assertLogs(main.logger, level="INFO") as logs:
                self.assertEqual(main.run("octo-org/demo"), 0)

        output = "\n".join(logs.output)
        self.assertNotIn(credential, output)
        self.assertNotIn("private-key-material", output)
        self.assertNotIn("app-jwt", output)


if __name__ == "__main__":
    unittest.main()
