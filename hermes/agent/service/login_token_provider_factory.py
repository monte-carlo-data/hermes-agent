"""Chooses the agent's login token provider from the environment.

Two independent choices combine here: the authentication *method* (OAuth
client credentials or an MCD key/token pair) and the *source* the credential is
read from (a file, or AWS Secrets Manager).

The precedence below matters only in hand-written environments — Docker
Compose, ECS: the chart rejects two sources in one block and both methods
carrying a source, and renders only the selected method's vars.

Environment is read inside the function, not at import, so tests can patch it.
"""

import logging
import os
from typing import Optional

from apollo.egress.agent.service.file_login_token_provider import FileLoginTokenProvider
from apollo.egress.agent.service.login_token_provider import (
    LocalLoginTokenProvider,
    LoginTokenProvider,
)

from hermes.agent.service.credentials_source import (
    AwsSecretsManagerCredentialsSource,
    CredentialsSource,
    FileCredentialsSource,
)
from hermes.agent.service.oauth_login_token_provider import OAuthLoginTokenProvider
from hermes.agent.service.token_login_token_provider import TokenLoginTokenProvider

logger = logging.getLogger(__name__)

# File sources are named method-first; AWS Secrets Manager sources
# source-first, so every secret the agent reads shares one prefix. The file
# vars keep their names because they are the path almost every deployment
# uses and are documented for Docker.
ENV_OAUTH_FILE_PATH = "MCD_OAUTH_FILE_PATH"
ENV_TOKEN_FILE_PATH = "MCD_TOKEN_FILE_PATH"
ENV_OAUTH_TOKEN_ENDPOINT = "MCD_OAUTH_TOKEN_ENDPOINT"

# `SecretId` is AWS's own term for what these hold: a secret's name or its
# full ARN.
ENV_AWS_SECRET_ID_OAUTH = "MCD_AWS_SECRET_ID_OAUTH"
ENV_AWS_SECRET_ID_KEY_TOKEN = "MCD_AWS_SECRET_ID_KEY_TOKEN"
ENV_AWS_SECRET_REGION = "MCD_AWS_SECRET_REGION"
ENV_AWS_SECRET_BASE64_ENCODED = "MCD_AWS_SECRET_BASE64_ENCODED"

# Retired names, kept only to detect and flag them; delete this once the
# deprecation window closes.
_RETIRED_ENV_VARS = {
    "MCD_OAUTH_AWS_SECRET_ID": ENV_AWS_SECRET_ID_OAUTH,
    "MCD_TOKEN_AWS_SECRET_ID": ENV_AWS_SECRET_ID_KEY_TOKEN,
    "MCD_AWS_SECRETS_MANAGER_REGION": ENV_AWS_SECRET_REGION,
}


def build_login_token_provider(backend_service_url: str) -> LoginTokenProvider:
    """Return the provider matching how this agent was configured.

    OAuth wins over key/token when both are configured — same
    hand-written-environment case as the source precedence above.
    """
    region = os.getenv(ENV_AWS_SECRET_REGION)
    base64_encoded = os.getenv(ENV_AWS_SECRET_BASE64_ENCODED, "").lower() == "true"

    oauth_source = _build_source(
        file_path=os.getenv(ENV_OAUTH_FILE_PATH),
        aws_secret_id=os.getenv(ENV_AWS_SECRET_ID_OAUTH),
        region=region,
        base64_encoded=base64_encoded,
        label="OAuth credentials",
    )
    if oauth_source:
        logger.info(
            f"Using OAuth client_credentials authentication, "
            f"credentials from {oauth_source.describe()}"
        )
        return OAuthLoginTokenProvider(
            credentials_source=oauth_source,
            backend_service_url=backend_service_url,
            token_endpoint=os.getenv(ENV_OAUTH_TOKEN_ENDPOINT),
        )

    token_file_path = os.getenv(ENV_TOKEN_FILE_PATH)
    token_source = _build_source(
        file_path=token_file_path,
        aws_secret_id=os.getenv(ENV_AWS_SECRET_ID_KEY_TOKEN),
        region=region,
        base64_encoded=base64_encoded,
        label="Key/token credentials",
    )
    if token_source:
        if isinstance(token_source, FileCredentialsSource):
            # agent-common's provider rather than TokenLoginTokenProvider over
            # the same source: switching the path almost every deployment uses
            # would rename the `token_file_path` attribute support tooling
            # reads out of reachability results.
            logger.info(f"Getting MCD token from file: {token_source.file_path}")
            return FileLoginTokenProvider(file_path=token_source.file_path)

        logger.info(f"Getting MCD token from {token_source.describe()}")
        return TokenLoginTokenProvider(credentials_source=token_source)

    # Presence, not value: reading it would tell us nothing more, and a
    # `getenv` on a name containing SECRET makes CodeQL treat the name we log
    # as the secret itself.
    for retired, replacement in _RETIRED_ENV_VARS.items():
        if retired in os.environ:
            logger.error(f"{retired} is retired and no longer read; use {replacement}")

    logger.info("Getting MCD token from env vars")
    return LocalLoginTokenProvider()


def _build_source(
    file_path: Optional[str],
    aws_secret_id: Optional[str],
    region: Optional[str],
    base64_encoded: bool,
    label: str,
) -> Optional[CredentialsSource]:
    """Return the configured source, preferring AWS Secrets Manager.

    None means this method was not configured at all, which is how the caller
    decides between methods.
    """
    if aws_secret_id:
        if file_path:
            logger.warning(
                f"{label} are configured both as a file and as an AWS Secrets "
                f"Manager secret; reading them from AWS Secrets Manager"
            )
        return AwsSecretsManagerCredentialsSource(
            secret_id=aws_secret_id, region=region, base64_encoded=base64_encoded
        )
    if file_path:
        return FileCredentialsSource(file_path=file_path)
    return None
