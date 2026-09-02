"""Key/token authentication backed by a pluggable credential source.

agent-common's ``FileLoginTokenProvider`` covers the case where the credential
is a file, which is what a Kubernetes Secret mount or a Docker bind mount
produces. This provider covers the sources that are not files — today AWS
Secrets Manager, read by the agent itself using the pod's own AWS identity — so
an operator who cannot run the External Secrets Operator does not have to
materialize the credential as a Kubernetes Secret at all.
"""

import logging
from typing import Any, Dict, Optional

from apollo.egress.agent.service.login_token_provider import LoginTokenProvider
from apollo.egress.agent.utils.utils import X_MCD_ID, X_MCD_TOKEN

from hermes.agent.service.credentials_source import CredentialsSource

logger = logging.getLogger(__name__)

_MCD_ID_ATTR = "mcd_id"
_MCD_TOKEN_ATTR = "mcd_token"

# Matches agent-common's FileLoginTokenProvider: the backend rejects these at
# the gateway, and seeing them in a reachability result is how an operator
# learns the agent never got a usable credential.
_NO_TOKEN_ID = "no-token-id"
_NO_TOKEN_SECRET = "no-token-secret"


class TokenLoginTokenProvider(LoginTokenProvider):
    """Sends `mcd_id`/`mcd_token` headers read from a credentials source."""

    def __init__(self, credentials_source: CredentialsSource):
        self._credentials_source = credentials_source
        # Composed from the source rather than fixed, so reachability output
        # distinguishes a credential read from a secret manager from one read
        # off disk. Lines up with agent-common's `token_file`.
        self.authentication_method = f"token_{credentials_source.source_name}"

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

        if _MCD_ID_ATTR in credentials and _MCD_TOKEN_ATTR in credentials:
            return credentials

        logger.warning(
            f"Agent credentials are missing '{_MCD_ID_ATTR}' or "
            f"'{_MCD_TOKEN_ATTR}', keys present: {sorted(credentials.keys())}"
        )
        return None
