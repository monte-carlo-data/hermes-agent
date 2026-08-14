import json
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests
import requests.auth
from retry import retry

from apollo.egress.agent.service.login_token_provider import LoginTokenProvider

logger = logging.getLogger(__name__)

AUTH_METHOD_OAUTH_CLIENT_CREDENTIALS = "oauth_client_credentials"
ATTR_NAME_CREDENTIALS_FILE_PATH = "credentials_file_path"
ATTR_NAME_TOKEN_ENDPOINT = "token_endpoint"
_NO_CLIENT_ID = "no-client-id"

# Intentionally hardcoded — the Cognito resource server identifier is the same
# across all regional deployments.
_OAUTH_SCOPE = "https://artemis.getmontecarlo.com/connect"
_TOKEN_PATH = "/oauth2/token"
_REFRESH_FRACTION = 0.8
_REFRESH_BUFFER_SECONDS = 300
_TOKEN_REQUEST_TIMEOUT = 30
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0


class OAuthTokenError(Exception):
    pass


class _RetryableHTTPError(Exception):
    """5xx HTTP errors wrapped to opt into the retry decorator's retry list."""


class OAuthLoginTokenProvider(LoginTokenProvider):
    """OAuth 2.0 client_credentials token provider for Monte Carlo backend auth."""

    authentication_method: str = AUTH_METHOD_OAUTH_CLIENT_CREDENTIALS

    def __init__(
        self,
        file_path: str,
        backend_service_url: str,
        token_endpoint: Optional[str] = None,
    ):
        self._file_path = file_path

        if token_endpoint:
            self._token_endpoint = token_endpoint
        else:
            self._token_endpoint = self._derive_token_endpoint(backend_service_url)

        if not self._token_endpoint.startswith("https://"):
            raise ValueError("OAuth token endpoint must use HTTPS")

        self._lock = threading.Lock()
        self._access_token: Optional[str] = None
        self._acquired_at: float = 0.0
        self._expires_in: int = 0

        logger.info(
            f"OAuth token provider initialized, token endpoint: {self._token_endpoint}"
        )

    @staticmethod
    def _derive_token_endpoint(backend_service_url: str) -> str:
        parsed = urlparse(backend_service_url)
        hostname = parsed.hostname or ""
        dot_index = hostname.find(".")
        if dot_index == -1:
            remainder = hostname
        else:
            remainder = hostname[dot_index + 1 :]

        port_suffix = f":{parsed.port}" if parsed.port else ""
        return f"https://m2m.{remainder}{port_suffix}{_TOKEN_PATH}"

    def get_token(self) -> Dict[str, str]:
        if self._needs_refresh():
            with self._lock:
                if self._needs_refresh():
                    cached = self._access_token
                    try:
                        self._fetch_token()
                    except Exception:
                        if cached is None:
                            raise
                        self._access_token = cached  # restore
                        logger.warning(
                            "Proactive token refresh failed, using cached token",
                            exc_info=True,
                        )

        return {"Authorization": f"Bearer {self._access_token}"}

    def get_credential_id(self) -> Optional[str]:
        """Return the OAuth client id, for reporting only.

        Reads the credentials file directly: the id has to be reportable when
        the token request is what's failing, so this never hits the network and
        never raises.
        """
        try:
            client_id, _ = self._read_credentials()
            return client_id
        except OAuthTokenError as ex:
            logger.warning(f"Failed to resolve the OAuth client id: {ex}")
            return _NO_CLIENT_ID

    def get_credential_info(self) -> Dict[str, Any]:
        # The file path and the token endpoint are included so a misconfigured
        # secret mount or a wrong endpoint is self-evident from the output.
        return {
            **super().get_credential_info(),
            ATTR_NAME_CREDENTIALS_FILE_PATH: self._file_path,
            ATTR_NAME_TOKEN_ENDPOINT: self._token_endpoint,
        }

    def _needs_refresh(self) -> bool:
        if self._access_token is None:
            return True
        elapsed = time.monotonic() - self._acquired_at
        threshold = max(
            0.0,
            min(
                _REFRESH_FRACTION * self._expires_in,
                self._expires_in - _REFRESH_BUFFER_SECONDS,
            ),
        )
        return elapsed >= threshold

    def _read_credentials(self) -> Tuple[str, str]:
        """Read OAuth client credentials from the JSON file."""
        try:
            with open(self._file_path) as f:
                data = json.load(f)
        except FileNotFoundError:
            raise OAuthTokenError(
                f"OAuth credentials file not found: {self._file_path}"
            )
        except PermissionError:
            raise OAuthTokenError(
                f"Cannot read OAuth credentials file (permission denied): "
                f"{self._file_path}"
            )
        except json.JSONDecodeError:
            raise OAuthTokenError(
                f"OAuth credentials file is not valid JSON: {self._file_path}"
            )

        if not isinstance(data, dict):
            raise OAuthTokenError(
                f"OAuth credentials file must contain a JSON object: {self._file_path}"
            )

        client_id = data.get("client_id")
        client_secret = data.get("client_secret")
        if not client_id or not client_secret:
            raise OAuthTokenError(
                "OAuth credentials file must contain non-empty "
                "'client_id' and 'client_secret' keys"
            )
        return client_id, client_secret

    def _fetch_token(self) -> None:
        client_id, client_secret = self._read_credentials()
        self._post_token_request(client_id, client_secret)

    @retry(
        exceptions=(_RetryableHTTPError,),
        tries=_MAX_RETRIES,
        delay=_RETRY_BASE_DELAY,
        backoff=2,
        logger=logger,
    )
    def _post_token_request(self, client_id: str, client_secret: str) -> None:
        try:
            response = requests.post(
                self._token_endpoint,
                auth=requests.auth.HTTPBasicAuth(client_id, client_secret),
                data={"grant_type": "client_credentials", "scope": _OAUTH_SCOPE},
                timeout=_TOKEN_REQUEST_TIMEOUT,
            )
        except requests.ConnectionError:
            raise _RetryableHTTPError("Connection error during token request")

        if response.status_code == 401:
            self._access_token = None
            raise OAuthTokenError("Invalid OAuth credentials (401 from token endpoint)")

        if response.status_code >= 500:
            raise _RetryableHTTPError(f"Token endpoint returned {response.status_code}")

        response.raise_for_status()

        body = response.json()
        access_token = body.get("access_token")
        expires_in = body.get("expires_in")

        if not isinstance(access_token, str) or not access_token:
            self._access_token = None
            raise OAuthTokenError(
                "Token response missing or empty 'access_token' field"
            )
        if not isinstance(expires_in, (int, float)) or expires_in <= 0:
            self._access_token = None
            raise OAuthTokenError(
                "Token response missing or invalid 'expires_in' field"
            )

        self._access_token = access_token
        self._acquired_at = time.monotonic()
        self._expires_in = int(expires_in)

        if self._expires_in < _REFRESH_BUFFER_SECONDS:
            logger.warning(
                f"Token TTL ({self._expires_in}s) is shorter than refresh buffer "
                f"({_REFRESH_BUFFER_SECONDS}s); token will be refreshed on every call"
            )

        logger.info(f"OAuth token acquired, expires in {self._expires_in}s")
