"""Key/token authentication over a pluggable ``CredentialsSource``.

agent-common's ``FileLoginTokenProvider`` remains the provider for
file-backed credentials — see the note in ``login_token_provider_factory``
for why. This provider exists for the sources that are not files.
"""

import logging
from typing import Any, Dict, Optional

from apollo.egress.agent.service.login_token_provider import (
    AUTH_METHOD_TOKEN_FILE,
    LoginTokenProvider,
)
from apollo.egress.agent.utils.utils import X_MCD_ID, X_MCD_TOKEN

from hermes.agent.service.credentials_source import (
    SOURCE_AWS_SECRETS_MANAGER,
    SOURCE_FILE,
    CredentialsSource,
)

logger = logging.getLogger(__name__)

_MCD_ID_ATTR = "mcd_id"
_MCD_TOKEN_ATTR = "mcd_token"

# Matches agent-common's FileLoginTokenProvider: the backend rejects these at
# the gateway, and seeing them in a reachability result is how an operator
# learns the agent never got a usable credential.
_NO_TOKEN_ID = "no-token-id"
_NO_TOKEN_SECRET = "no-token-secret"

AUTH_METHOD_TOKEN_AWS_SECRETS_MANAGER = "token_aws_secrets_manager"

# The authentication method is reported to the backend and grouped on by
# support tooling, so it must exist as a literal rather than be composed from
# a source's internal label — renaming that label must not silently change
# the externally observed value. `SOURCE_FILE` is included even though the
# factory never builds this provider over a file source, so the mapping stays
# total for every source this package knows about.
_AUTH_METHOD_BY_SOURCE: Dict[str, str] = {
    SOURCE_FILE: AUTH_METHOD_TOKEN_FILE,
    SOURCE_AWS_SECRETS_MANAGER: AUTH_METHOD_TOKEN_AWS_SECRETS_MANAGER,
}


class TokenLoginTokenProvider(LoginTokenProvider):
    """Sends `mcd_id`/`mcd_token` headers read from a credentials source."""

    def __init__(self, credentials_source: CredentialsSource):
        self._credentials_source = credentials_source
        # Falls back to the composed string for a source this mapping does
        # not (yet) know about, rather than raising.
        self.authentication_method = _AUTH_METHOD_BY_SOURCE.get(
            credentials_source.source_name,
            f"token_{credentials_source.source_name}",
        )

    def get_token(self) -> Dict[str, str]:
        credentials = self._read_credentials()
        if credentials:
            return {
                X_MCD_ID: credentials[_MCD_ID_ATTR],
                X_MCD_TOKEN: credentials[_MCD_TOKEN_ATTR],
            }
        return {X_MCD_ID: _NO_TOKEN_ID, X_MCD_TOKEN: _NO_TOKEN_SECRET}

    def get_credential_id(self) -> Optional[str]:
        """Return the configured `mcd_id`, for reporting only.

        Reads the credential rather than inspecting a sent header: the id is
        what the agent is configured with, independent of whether a usable
        token can be produced from it. Never raises — this is called on the
        startup path and when authentication is already failing.
        """
        credentials = self._read_credentials()
        if credentials:
            return credentials[_MCD_ID_ATTR]
        return _NO_TOKEN_ID

    def get_credential_info(self) -> Dict[str, Any]:
        return {
            **super().get_credential_info(),
            **self._credentials_source.describe(),
        }

    def _read_credentials(self) -> Optional[Dict[str, Any]]:
        """Return the parsed credential, or None when it can't be used.

        Catches everything rather than only ``CredentialsSourceError``: this
        runs on the startup path and while authentication is already failing,
        and a source can fail in ways it does not convert — a boto client that
        cannot resolve the pod's AWS credentials raises its own exceptions.
        Those are misconfigurations, exactly what the `no-token-id` sentinel
        exists to report, so they must not propagate out of a reporting call.
        """
        try:
            credentials = self._credentials_source.read()
        except Exception as ex:
            logger.error(f"Failed to read agent credentials: {ex}")
            return None

        if credentials.get(_MCD_ID_ATTR) and credentials.get(_MCD_TOKEN_ATTR):
            return credentials

        logger.warning(
            f"Agent credentials are missing or empty '{_MCD_ID_ATTR}' or "
            f"'{_MCD_TOKEN_ATTR}', keys present: {sorted(credentials.keys())}"
        )
        return None
