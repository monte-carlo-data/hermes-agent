import logging
import threading
import time
from typing import Dict, Optional
from urllib.parse import urlparse

import requests
import requests.auth

from apollo.egress.agent.service.login_token_provider import LoginTokenProvider

logger = logging.getLogger(__name__)

# Intentionally hardcoded — the Cognito resource server identifier is the same
# across all regional deployments.
_OAUTH_SCOPE = "https://artemis.getmontecarlo.com/connect"
_TOKEN_PATH = "/oauth2/token"
_REFRESH_FRACTION = 0.8
_REFRESH_BUFFER_SECONDS = 300
_TOKEN_REQUEST_TIMEOUT = 30


class OAuthTokenError(Exception):
    pass


class OAuthLoginTokenProvider(LoginTokenProvider):
    """OAuth 2.0 client_credentials token provider for Monte Carlo backend auth."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        backend_service_url: str,
        token_endpoint: Optional[str] = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret

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
                    try:
                        self._fetch_token()
                    except Exception:
                        if self._access_token is None:
                            raise
                        logger.warning(
                            "Proactive token refresh failed, using cached token",
                            exc_info=True,
                        )

        return {"Authorization": f"Bearer {self._access_token}"}

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

    def _fetch_token(self) -> None:
        response = requests.post(
            self._token_endpoint,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=requests.auth.HTTPBasicAuth(self._client_id, self._client_secret),
            data=f"grant_type=client_credentials&scope={_OAUTH_SCOPE}",
            timeout=_TOKEN_REQUEST_TIMEOUT,
        )

        if response.status_code == 401:
            self._access_token = None
            raise OAuthTokenError("Invalid OAuth credentials (401 from token endpoint)")

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

        logger.info(f"OAuth token acquired, expires in {self._expires_in}s")
