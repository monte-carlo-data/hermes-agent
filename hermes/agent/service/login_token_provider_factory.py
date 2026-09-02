"""Chooses the agent's login token provider from the environment.

Two independent choices combine here: the authentication *method* (OAuth
client credentials or an MCD key/token pair) and the *source* the credential is
read from (a file, or AWS Secrets Manager). The helm chart renders one env var
per configured combination, so the precedence encoded below is what an operator
gets when a values file sets more than one.

Environment is read inside the function rather than at module import so tests
can patch it.
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

ENV_OAUTH_FILE_PATH = "MCD_OAUTH_FILE_PATH"
ENV_OAUTH_AWS_SECRET_ID = "MCD_OAUTH_AWS_SECRET_ID"
ENV_OAUTH_TOKEN_ENDPOINT = "MCD_OAUTH_TOKEN_ENDPOINT"
ENV_TOKEN_FILE_PATH = "MCD_TOKEN_FILE_PATH"
ENV_TOKEN_AWS_SECRET_ID = "MCD_TOKEN_AWS_SECRET_ID"
ENV_AWS_SECRETS_MANAGER_REGION = "MCD_AWS_SECRETS_MANAGER_REGION"


def build_login_token_provider(backend_service_url: str) -> LoginTokenProvider:
    """Return the provider matching how this agent was configured.

    OAuth wins over key/token when both are configured, matching the chart,
    which mounts only the selected method's secret.
    """
    region = os.getenv(ENV_AWS_SECRETS_MANAGER_REGION)

    oauth_source = _build_source(
        file_path=os.getenv(ENV_OAUTH_FILE_PATH),
        aws_secret_id=os.getenv(ENV_OAUTH_AWS_SECRET_ID),
        region=region,
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

    token_aws_secret_id = os.getenv(ENV_TOKEN_AWS_SECRET_ID)
    token_file_path = os.getenv(ENV_TOKEN_FILE_PATH)

    if token_aws_secret_id:
        if token_file_path:
            logger.warning(
                f"Both {ENV_TOKEN_AWS_SECRET_ID} and {ENV_TOKEN_FILE_PATH} are set; "
                f"reading the key/token credential from AWS Secrets Manager"
            )
        source = AwsSecretsManagerCredentialsSource(
            secret_id=token_aws_secret_id, region=region
        )
        logger.info(f"Getting MCD token from {source.describe()}")
        return TokenLoginTokenProvider(credentials_source=source)

    if token_file_path:
        # Deliberately agent-common's provider rather than
        # TokenLoginTokenProvider over a FileCredentialsSource: this is the
        # path almost every deployment uses, and switching it would rename the
        # `token_file_path` attribute that support tooling reads out of
        # reachability results. The two report the same authentication method.
        logger.info(f"Getting MCD token from file: {token_file_path}")
        return FileLoginTokenProvider(file_path=token_file_path)

    logger.info("Getting MCD token from env vars")
    return LocalLoginTokenProvider()


def _build_source(
    file_path: Optional[str],
    aws_secret_id: Optional[str],
    region: Optional[str],
    label: str,
) -> Optional[CredentialsSource]:
    """Return the configured source, preferring AWS Secrets Manager.

    Returning None means this method was not configured at all, which is how
    the caller decides between methods.
    """
    if aws_secret_id:
        if file_path:
            logger.warning(
                f"{label} are configured both as a file and as an AWS Secrets "
                f"Manager secret; reading them from AWS Secrets Manager"
            )
        return AwsSecretsManagerCredentialsSource(
            secret_id=aws_secret_id, region=region
        )
    if file_path:
        return FileCredentialsSource(file_path=file_path)
    return None
