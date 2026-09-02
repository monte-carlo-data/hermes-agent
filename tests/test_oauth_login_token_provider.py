import json
import os
import tempfile
import threading
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

from apollo.egress.agent.service.login_token_provider import (
    ATTR_NAME_AUTH_METHOD,
    ATTR_NAME_KEY_ID,
)

from hermes.agent.service.credentials_source import (
    ATTR_NAME_FILE_PATH,
    ATTR_NAME_SOURCE,
    SOURCE_FILE,
    FileCredentialsSource,
)
from hermes.agent.service.oauth_login_token_provider import (
    ATTR_NAME_TOKEN_ENDPOINT,
    AUTH_METHOD_OAUTH_CLIENT_CREDENTIALS,
    OAuthLoginTokenProvider,
    OAuthTokenError,
    _RetryableHTTPError,
)


class OAuthLoginTokenProviderTests(TestCase):
    def setUp(self):
        self._creds_fd, self._creds_path = tempfile.mkstemp(suffix=".json")
        with open(self._creds_fd, "w") as f:
            json.dump(
                {"client_id": "test-client-id", "client_secret": "test-client-secret"},
                f,
            )

    def tearDown(self):
        os.unlink(self._creds_path)

    def _make_provider(self, **kwargs):
        # `file_path` stays the test-side knob: these cases are about the
        # provider's token handling and its reporting of an unreadable
        # credential, both of which are exercised through the file source.
        defaults = {
            "file_path": self._creds_path,
            "backend_service_url": "https://artemis.getmontecarlo.com:443",
        }
        defaults.update(kwargs)
        file_path = defaults.pop("file_path")
        return OAuthLoginTokenProvider(
            credentials_source=FileCredentialsSource(file_path), **defaults
        )

    def _make_success_response(self, access_token="test-jwt", expires_in=3600):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "access_token": access_token,
            "expires_in": expires_in,
            "token_type": "Bearer",
        }
        response.raise_for_status = Mock()
        return response

    # ── Token endpoint derivation ──

    def test_derives_token_endpoint_simple(self):
        provider = self._make_provider(
            backend_service_url="https://artemis.getmontecarlo.com"
        )
        self.assertEqual(
            provider._token_endpoint,
            "https://m2m.getmontecarlo.com/oauth2/token",
        )

    def test_derives_token_endpoint_multi_level_subdomain(self):
        provider = self._make_provider(
            backend_service_url="https://artemis.eu1.getmontecarlo.com"
        )
        self.assertEqual(
            provider._token_endpoint,
            "https://m2m.eu1.getmontecarlo.com/oauth2/token",
        )

    def test_derives_token_endpoint_with_custom_port(self):
        provider = self._make_provider(
            backend_service_url="https://artemis.getmontecarlo.com:8443"
        )
        self.assertEqual(
            provider._token_endpoint,
            "https://m2m.getmontecarlo.com:8443/oauth2/token",
        )

    def test_derives_token_endpoint_with_standard_port(self):
        provider = self._make_provider(
            backend_service_url="https://artemis.getmontecarlo.com:443"
        )
        self.assertEqual(
            provider._token_endpoint,
            "https://m2m.getmontecarlo.com:443/oauth2/token",
        )

    def test_explicit_token_endpoint_overrides_derivation(self):
        provider = self._make_provider(
            token_endpoint="https://custom.example.com/oauth2/token"
        )
        self.assertEqual(
            provider._token_endpoint,
            "https://custom.example.com/oauth2/token",
        )

    # ── HTTPS enforcement ──

    def test_derived_endpoint_always_uses_https(self):
        # Even when backend URL is http://, derivation produces https://
        provider = self._make_provider(backend_service_url="http://artemis.local:8080")
        self.assertTrue(provider._token_endpoint.startswith("https://"))

    def test_non_https_explicit_endpoint_raises(self):
        with self.assertRaises(ValueError):
            self._make_provider(
                token_endpoint="http://m2m.getmontecarlo.com/oauth2/token"
            )

    # ── Token acquisition ──

    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_get_token_returns_bearer_header(self, mock_post):
        mock_post.return_value = self._make_success_response()
        provider = self._make_provider()

        result = provider.get_token()

        self.assertEqual(result, {"Authorization": "Bearer test-jwt"})

    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_get_token_posts_correct_request(self, mock_post):
        mock_post.return_value = self._make_success_response()
        provider = self._make_provider()

        provider.get_token()

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        self.assertEqual(
            call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("url"),
            "https://m2m.getmontecarlo.com:443/oauth2/token",
        )
        self.assertEqual(
            call_kwargs.kwargs["data"],
            {
                "grant_type": "client_credentials",
                "scope": "https://artemis.getmontecarlo.com/connect",
            },
        )
        auth = call_kwargs.kwargs["auth"]
        self.assertIsInstance(auth, requests.auth.HTTPBasicAuth)
        self.assertEqual(auth.username, "test-client-id")
        self.assertEqual(auth.password, "test-client-secret")

    # ── Token caching ──

    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_second_call_uses_cached_token(self, mock_post):
        mock_post.return_value = self._make_success_response()
        provider = self._make_provider()

        result1 = provider.get_token()
        result2 = provider.get_token()

        mock_post.assert_called_once()
        self.assertEqual(result1, {"Authorization": "Bearer test-jwt"})
        self.assertEqual(result2, {"Authorization": "Bearer test-jwt"})

    # ── Proactive refresh ──

    @patch("hermes.agent.service.oauth_login_token_provider.time.monotonic")
    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_no_refresh_before_threshold(self, mock_post, mock_monotonic):
        mock_post.return_value = self._make_success_response(expires_in=3600)
        # _needs_refresh() skips monotonic() when _access_token is None, so:
        # 1st monotonic call: _fetch_token sets _acquired_at = 1000
        # 2nd monotonic call: _needs_refresh check → elapsed = 3879 - 1000 = 2879 < 2880
        mock_monotonic.side_effect = [1000.0, 3879.0]
        provider = self._make_provider()

        provider.get_token()
        provider.get_token()

        mock_post.assert_called_once()

    @patch("hermes.agent.service.oauth_login_token_provider.time.monotonic")
    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_refresh_at_threshold(self, mock_post, mock_monotonic):
        first_response = self._make_success_response(
            access_token="token-1", expires_in=3600
        )
        second_response = self._make_success_response(
            access_token="token-2", expires_in=3600
        )
        mock_post.side_effect = [first_response, second_response]
        # 1st: _fetch_token sets _acquired_at = 1000
        # 2nd: _needs_refresh outer → elapsed = 3880 - 1000 = 2880 >= 2880 → True
        # 3rd: _needs_refresh inner (after lock) → elapsed = 3880 - 1000 = 2880 → True
        # 4th: _fetch_token sets new _acquired_at = 3880
        mock_monotonic.side_effect = [1000.0, 3880.0, 3880.0, 3880.0]
        provider = self._make_provider()

        result1 = provider.get_token()
        result2 = provider.get_token()

        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(result1, {"Authorization": "Bearer token-1"})
        self.assertEqual(result2, {"Authorization": "Bearer token-2"})

    @patch("hermes.agent.service.oauth_login_token_provider.time.monotonic")
    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_short_ttl_refreshes_proactively(self, mock_post, mock_monotonic):
        first_resp = self._make_success_response(access_token="token-1", expires_in=60)
        second_resp = self._make_success_response(access_token="token-2", expires_in=60)
        mock_post.side_effect = [first_resp, second_resp]
        # Short TTL (< _REFRESH_BUFFER_SECONDS):
        # threshold = max(0, min(0.8*60, 60-300)) = max(0, min(48, -240)) = 0
        # At any elapsed >= 0, _needs_refresh() returns True → refresh on every call.
        # 1st: _fetch_token sets _acquired_at = 1000
        # 2nd: _needs_refresh (outer) → elapsed = 1047 - 1000 = 47 >= 0 → True
        # 3rd: _needs_refresh (inner, after lock) → same → True
        # 4th: _fetch_token sets new _acquired_at = 1047
        mock_monotonic.side_effect = [1000.0, 1047.0, 1047.0, 1047.0]
        provider = self._make_provider()

        provider.get_token()
        result2 = provider.get_token()

        # Short-lived tokens (< _REFRESH_BUFFER_SECONDS) proactively refresh each
        # call; threshold is clamped to 0 by max(0, ...) — never negative.
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(result2, {"Authorization": "Bearer token-2"})

    # ── Concurrent token refresh ──

    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_concurrent_refresh_calls_post_once(self, mock_post):
        mock_post.return_value = self._make_success_response()
        provider = self._make_provider()

        num_threads = 10
        barrier = threading.Barrier(num_threads)
        results = [None] * num_threads

        def worker(index):
            barrier.wait()
            results[index] = provider.get_token()

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        for t in threads:
            self.assertFalse(t.is_alive(), f"Thread {t.name} is still alive after join")

        mock_post.assert_called_once()
        for i in range(num_threads):
            self.assertIsNotNone(results[i])
            self.assertIn("Authorization", results[i])
            self.assertTrue(results[i]["Authorization"].startswith("Bearer "))

    # ── Error handling ──

    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_401_raises_oauth_token_error(self, mock_post):
        response = Mock()
        response.status_code = 401
        mock_post.return_value = response
        provider = self._make_provider()

        with self.assertRaises(OAuthTokenError):
            provider.get_token()

    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_401_clears_cache_allows_reacquisition(self, mock_post):
        error_response = Mock()
        error_response.status_code = 401

        success_response = self._make_success_response(access_token="fresh-token")

        mock_post.side_effect = [error_response, success_response]
        provider = self._make_provider()

        with self.assertRaises(OAuthTokenError):
            provider.get_token()

        result = provider.get_token()
        self.assertEqual(result, {"Authorization": "Bearer fresh-token"})
        self.assertEqual(mock_post.call_count, 2)

    @patch("hermes.agent.service.oauth_login_token_provider.time.sleep")
    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_network_error_retries_then_propagates(self, mock_post, mock_sleep):
        mock_post.side_effect = requests.ConnectionError("connection refused")
        provider = self._make_provider()

        with self.assertRaises(_RetryableHTTPError):
            provider.get_token()
        self.assertEqual(mock_post.call_count, 3)

    # ── No credential logging ──

    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_no_credentials_in_logs(self, mock_post):
        mock_post.return_value = self._make_success_response()

        with self.assertLogs(
            "hermes.agent.service.oauth_login_token_provider", level="DEBUG"
        ) as cm:
            provider = self._make_provider()
            provider.get_token()

        all_output = "\n".join(cm.output)
        self.assertNotIn("test-client-secret", all_output)
        self.assertNotIn("test-client-id", all_output)

    # ── Empty token endpoint ──

    def test_empty_string_token_endpoint_falls_through_to_derivation(self):
        provider = self._make_provider(
            token_endpoint="",
            backend_service_url="https://artemis.getmontecarlo.com:443",
        )
        self.assertEqual(
            provider._token_endpoint,
            "https://m2m.getmontecarlo.com:443/oauth2/token",
        )

    # ── Non-401 HTTP errors ──

    @patch("hermes.agent.service.oauth_login_token_provider.time.sleep")
    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_server_error_retries_then_propagates(self, mock_post, mock_sleep):
        response = Mock()
        response.status_code = 500
        mock_post.return_value = response
        provider = self._make_provider()

        with self.assertRaises(_RetryableHTTPError):
            provider.get_token()
        self.assertEqual(mock_post.call_count, 3)

    # ── Malformed response body ──

    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_missing_access_token_raises_oauth_error(self, mock_post):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"expires_in": 3600, "token_type": "Bearer"}
        response.raise_for_status = Mock()
        mock_post.return_value = response
        provider = self._make_provider()

        with self.assertRaises(OAuthTokenError) as ctx:
            provider.get_token()
        self.assertIn("access_token", str(ctx.exception))
        self.assertIsNone(provider._access_token)

    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_missing_expires_in_raises_oauth_error(self, mock_post):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "access_token": "some-token",
            "token_type": "Bearer",
        }
        response.raise_for_status = Mock()
        mock_post.return_value = response
        provider = self._make_provider()

        with self.assertRaises(OAuthTokenError) as ctx:
            provider.get_token()
        self.assertIn("expires_in", str(ctx.exception))
        self.assertIsNone(provider._access_token)

    # ── Proactive refresh failure returns cached token ──

    @patch("hermes.agent.service.oauth_login_token_provider.time.sleep")
    @patch("hermes.agent.service.oauth_login_token_provider.time.monotonic")
    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_proactive_refresh_failure_returns_cached_token(
        self, mock_post, mock_monotonic, mock_sleep
    ):
        success_response = self._make_success_response(
            access_token="original-token", expires_in=3600
        )
        # ConnectionError is now retried 3 times before giving up
        mock_post.side_effect = [
            success_response,
            requests.ConnectionError("refused"),
            requests.ConnectionError("refused"),
            requests.ConnectionError("refused"),
        ]
        # 1st: _fetch_token _acquired_at = 1000
        # 2nd: _needs_refresh outer → elapsed = 4000 - 1000 = 3000 >= 2880 → True
        # 3rd: _needs_refresh inner → same → True
        mock_monotonic.side_effect = [1000.0, 4000.0, 4000.0]
        provider = self._make_provider()

        result1 = provider.get_token()
        self.assertEqual(result1, {"Authorization": "Bearer original-token"})

        # Second call triggers proactive refresh which fails — cached token returned
        result2 = provider.get_token()
        self.assertEqual(result2, {"Authorization": "Bearer original-token"})
        self.assertEqual(mock_post.call_count, 4)

    # ── Credential file reading ──

    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_missing_credentials_file_raises(self, mock_post):
        provider = self._make_provider(file_path="/nonexistent/path/creds.json")
        with self.assertRaises(OAuthTokenError) as ctx:
            provider.get_token()
        self.assertIn("not found", str(ctx.exception))
        mock_post.assert_not_called()

    def test_invalid_json_credentials_file_raises(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with open(fd, "w") as f:
                f.write("not json")
            provider = self._make_provider(file_path=path)
            with self.assertRaises(OAuthTokenError) as ctx:
                provider.get_token()
            self.assertIn("not valid JSON", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_missing_client_id_in_file_raises(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with open(fd, "w") as f:
                json.dump({"client_secret": "secret"}, f)
            provider = self._make_provider(file_path=path)
            with self.assertRaises(OAuthTokenError) as ctx:
                provider.get_token()
            self.assertIn("client_id", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_missing_client_secret_in_file_raises(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with open(fd, "w") as f:
                json.dump({"client_id": "id"}, f)
            provider = self._make_provider(file_path=path)
            with self.assertRaises(OAuthTokenError) as ctx:
                provider.get_token()
            self.assertIn("client_secret", str(ctx.exception))
        finally:
            os.unlink(path)

    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_credential_rotation_picks_up_new_credentials(self, mock_post):
        mock_post.return_value = self._make_success_response()
        provider = self._make_provider()

        provider.get_token()
        auth1 = mock_post.call_args.kwargs["auth"]
        self.assertEqual(auth1.username, "test-client-id")

        # Rotate credentials on disk
        with open(self._creds_path, "w") as f:
            json.dump({"client_id": "rotated-id", "client_secret": "rotated-secret"}, f)

        # Force a refresh by clearing the cached token
        provider._access_token = None
        provider.get_token()
        auth2 = mock_post.call_args.kwargs["auth"]
        self.assertEqual(auth2.username, "rotated-id")
        self.assertEqual(auth2.password, "rotated-secret")

    # ── Retry on 5xx ──

    @patch("hermes.agent.service.oauth_login_token_provider.time.sleep")
    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_retry_on_5xx_succeeds_after_retry(self, mock_post, mock_sleep):
        error_response = Mock()
        error_response.status_code = 500
        success_response = self._make_success_response()
        mock_post.side_effect = [error_response, success_response]
        provider = self._make_provider()

        result = provider.get_token()

        self.assertEqual(result, {"Authorization": "Bearer test-jwt"})
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once()

    @patch("hermes.agent.service.oauth_login_token_provider.time.sleep")
    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_retry_on_5xx_exhausted_raises(self, mock_post, mock_sleep):
        error_response = Mock()
        error_response.status_code = 503
        mock_post.return_value = error_response
        provider = self._make_provider()

        with self.assertRaises(_RetryableHTTPError):
            provider.get_token()
        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("hermes.agent.service.oauth_login_token_provider.time.sleep")
    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_401_does_not_retry(self, mock_post, mock_sleep):
        response = Mock()
        response.status_code = 401
        mock_post.return_value = response
        provider = self._make_provider()

        with self.assertRaises(OAuthTokenError):
            provider.get_token()
        mock_post.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("hermes.agent.service.oauth_login_token_provider.time.sleep")
    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_retry_backoff_doubles(self, mock_post, mock_sleep):
        error_response = Mock()
        error_response.status_code = 502
        mock_post.return_value = error_response
        provider = self._make_provider()

        with self.assertRaises(_RetryableHTTPError):
            provider.get_token()
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)


class OAuthCredentialReportingTests(TestCase):
    """The client id must be reportable without fetching (or leaking) a token."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._creds_path = os.path.join(self._dir.name, "credentials.json")

    def _write_credentials(self, contents: str):
        with open(self._creds_path, "w") as f:
            f.write(contents)

    def _make_provider(self):
        return OAuthLoginTokenProvider(
            credentials_source=FileCredentialsSource(self._creds_path),
            backend_service_url="https://artemis.getmontecarlo.com",
        )

    def test_credential_info_reports_client_id_and_never_the_secret(self):
        self._write_credentials(
            json.dumps({"client_id": "a-client-id", "client_secret": "a-client-secret"})
        )
        provider = self._make_provider()

        credential_info = provider.get_credential_info()

        self.assertEqual(
            {
                ATTR_NAME_KEY_ID: "a-client-id",
                ATTR_NAME_AUTH_METHOD: AUTH_METHOD_OAUTH_CLIENT_CREDENTIALS,
                # `credentials_file_path` is retained for support tooling that
                # already reads it; `credentials_source` is what distinguishes
                # a file-backed credential from one read out of a secret
                # manager.
                ATTR_NAME_SOURCE: SOURCE_FILE,
                ATTR_NAME_FILE_PATH: self._creds_path,
                ATTR_NAME_TOKEN_ENDPOINT: "https://m2m.getmontecarlo.com/oauth2/token",
            },
            credential_info,
        )
        self.assertNotIn("a-client-secret", json.dumps(credential_info))

    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_credential_id_does_not_fetch_a_token(self, mock_post):
        self._write_credentials(
            json.dumps({"client_id": "a-client-id", "client_secret": "a-client-secret"})
        )
        provider = self._make_provider()

        self.assertEqual("a-client-id", provider.get_credential_id())
        mock_post.assert_not_called()

    def test_credential_id_reports_no_client_id_when_file_is_missing(self):
        provider = self._make_provider()

        self.assertEqual("no-client-id", provider.get_credential_id())
        self.assertEqual(
            self._creds_path,
            provider.get_credential_info()[ATTR_NAME_FILE_PATH],
        )

    def test_credential_id_reports_no_client_id_when_file_is_unparseable(self):
        self._write_credentials("not json")
        provider = self._make_provider()

        self.assertEqual("no-client-id", provider.get_credential_id())

    def test_credential_id_reports_no_client_id_when_path_is_a_directory(self):
        # Docker creates a directory at the container path when the host file
        # in a bind mount is missing — the misconfigured mount this reports on.
        os.mkdir(self._creds_path)
        provider = self._make_provider()

        self.assertEqual("no-client-id", provider.get_credential_id())
        self.assertEqual(
            self._creds_path,
            provider.get_credential_info()[ATTR_NAME_FILE_PATH],
        )

    def test_credential_id_reports_no_client_id_when_file_is_not_utf8(self):
        with open(self._creds_path, "wb") as f:
            f.write(b"\xff\xfe\x00")
        provider = self._make_provider()

        self.assertEqual("no-client-id", provider.get_credential_id())

    def test_credential_id_reports_no_client_id_on_unconverted_source_error(self):
        # Guards the bare `except Exception` in get_credential_id: a file
        # source's failures are always converted to CredentialsSourceError by
        # _read_credentials, so every other case in this class exercises that
        # path, not the catch-all. A secret-manager source can raise
        # something the source layer never converts (e.g. boto failing to
        # resolve the pod's AWS credentials) — narrowing the except clause to
        # OAuthTokenError would keep this suite green while breaking that
        # case in production.
        source = Mock()
        source.read.side_effect = RuntimeError("boto blew up")
        source.describe.return_value = {
            ATTR_NAME_SOURCE: "aws_secrets_manager",
            "credentials_secret_id": "mcd/agent/oauth",
        }
        provider = OAuthLoginTokenProvider(
            credentials_source=source,
            backend_service_url="https://artemis.getmontecarlo.com",
        )

        self.assertEqual("no-client-id", provider.get_credential_id())
        credential_info = provider.get_credential_info()
        self.assertEqual("aws_secrets_manager", credential_info[ATTR_NAME_SOURCE])
        self.assertEqual("mcd/agent/oauth", credential_info["credentials_secret_id"])
