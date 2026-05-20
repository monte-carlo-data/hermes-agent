import threading
from unittest import TestCase
from unittest.mock import Mock, call, patch

import requests

from hermes.agent.service.oauth_login_token_provider import (
    OAuthLoginTokenProvider,
    OAuthTokenError,
)


class OAuthLoginTokenProviderTests(TestCase):
    def _make_provider(self, **kwargs):
        defaults = {
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
            "backend_service_url": "https://artemis.getmontecarlo.com:443",
        }
        defaults.update(kwargs)
        return OAuthLoginTokenProvider(**defaults)

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
        provider = self._make_provider(
            backend_service_url="http://artemis.local:8080"
        )
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
            "grant_type=client_credentials&scope=https://artemis.getmontecarlo.com/connect",
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
    def test_short_ttl_refreshes_immediately(self, mock_post, mock_monotonic):
        first_response = self._make_success_response(
            access_token="token-1", expires_in=60
        )
        second_response = self._make_success_response(
            access_token="token-2", expires_in=60
        )
        mock_post.side_effect = [first_response, second_response]
        # Short TTL: threshold = min(0.8*60, 60-300) = min(48, -240) = -240
        # So any elapsed >= -240 triggers refresh, i.e., always refreshes
        # 1st: _fetch_token _acquired_at = 1000
        # 2nd: _needs_refresh outer → elapsed = 1000 - 1000 = 0 >= -240 → True
        # 3rd: _needs_refresh inner → same → True
        # 4th: _fetch_token _acquired_at = 1000
        mock_monotonic.side_effect = [1000.0, 1000.0, 1000.0, 1000.0]
        provider = self._make_provider()

        provider.get_token()
        result2 = provider.get_token()

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

    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_network_error_propagates(self, mock_post):
        mock_post.side_effect = requests.ConnectionError("connection refused")
        provider = self._make_provider()

        with self.assertRaises(requests.ConnectionError):
            provider.get_token()

    # ── No credential logging ──

    @patch("hermes.agent.service.oauth_login_token_provider.requests.post")
    def test_no_credentials_in_logs(self, mock_post):
        mock_post.return_value = self._make_success_response()

        with self.assertLogs(
            "hermes.agent.service.oauth_login_token_provider", level="DEBUG"
        ) as cm:
            provider = OAuthLoginTokenProvider(
                client_id="test-client-id",
                client_secret="test-client-secret",
                backend_service_url="https://artemis.getmontecarlo.com:443",
            )
            provider.get_token()

        all_output = "\n".join(cm.output)
        self.assertNotIn("test-client-secret", all_output)
        self.assertNotIn("test-client-id", all_output)
