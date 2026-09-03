from unittest import TestCase
from unittest.mock import patch

from apollo.egress.agent.service.file_login_token_provider import (
    ATTR_NAME_TOKEN_FILE_PATH,
    FileLoginTokenProvider,
)
from apollo.egress.agent.service.login_token_provider import LocalLoginTokenProvider

from hermes.agent.service.credentials_source import (
    ATTR_NAME_FILE_PATH,
    ATTR_NAME_REGION,
    ATTR_NAME_BASE64_ENCODED,
    ATTR_NAME_SECRET_ID,
    SOURCE_AWS_SECRETS_MANAGER,
    SOURCE_FILE,
    ATTR_NAME_SOURCE,
)
from hermes.agent.service.login_token_provider_factory import (
    ENV_AWS_SECRET_BASE64_ENCODED,
    ENV_AWS_SECRET_ID_KEY_TOKEN,
    ENV_AWS_SECRET_ID_OAUTH,
    ENV_AWS_SECRET_REGION,
    ENV_OAUTH_FILE_PATH,
    ENV_OAUTH_TOKEN_ENDPOINT,
    ENV_AWS_SECRET_ID_KEY_TOKEN,
    ENV_TOKEN_FILE_PATH,
    build_login_token_provider,
)
from hermes.agent.service.oauth_login_token_provider import OAuthLoginTokenProvider
from hermes.agent.service.token_login_token_provider import TokenLoginTokenProvider

_BACKEND = "https://artemis.getmontecarlo.com"

# Every env var the factory consults, cleared by default so each test declares
# only the combination it is about.
_ALL_ENV = {
    ENV_OAUTH_FILE_PATH: "",
    ENV_AWS_SECRET_ID_OAUTH: "",
    ENV_OAUTH_TOKEN_ENDPOINT: "",
    ENV_TOKEN_FILE_PATH: "",
    ENV_AWS_SECRET_REGION: "",
    ENV_AWS_SECRET_BASE64_ENCODED: "",
    ENV_AWS_SECRET_ID_KEY_TOKEN: "",
}


class BuildLoginTokenProviderTests(TestCase):
    def _build(self, **env):
        with patch.dict("os.environ", {**_ALL_ENV, **env}, clear=False):
            return build_login_token_provider(backend_service_url=_BACKEND)

    def test_no_configuration_falls_back_to_env_var_provider(self):
        self.assertIsInstance(self._build(), LocalLoginTokenProvider)

    def test_token_file_uses_agent_common_file_provider(self):
        provider = self._build(**{ENV_TOKEN_FILE_PATH: "/etc/secrets/contents.json"})
        # Kept on agent-common's provider so the `token_file_path` attribute
        # support tooling reads does not change.
        self.assertIsInstance(provider, FileLoginTokenProvider)

    def test_token_file_reports_token_file_path(self):
        # This is the contract the FileLoginTokenProvider/TokenLoginTokenProvider
        # asymmetry exists to protect: routing the key/token + file case through
        # the shared source-precedence helper must not lose the
        # `token_file_path` attribute support tooling reads out of reachability
        # results.
        provider = self._build(**{ENV_TOKEN_FILE_PATH: "/etc/secrets/contents.json"})
        self.assertEqual(
            "/etc/secrets/contents.json",
            provider.get_credential_info()[ATTR_NAME_TOKEN_FILE_PATH],
        )

    def test_token_aws_secret_uses_secrets_manager_source(self):
        provider = self._build(**{ENV_AWS_SECRET_ID_KEY_TOKEN: "mcd/agent/token"})
        self.assertIsInstance(provider, TokenLoginTokenProvider)
        self.assertEqual("token_aws_secrets_manager", provider.authentication_method)
        self.assertEqual(
            {
                ATTR_NAME_SOURCE: SOURCE_AWS_SECRETS_MANAGER,
                ATTR_NAME_SECRET_ID: "mcd/agent/token",
            },
            provider._credentials_source.describe(),
        )

    def test_base64_flag_is_passed_to_the_aws_source(self):
        provider = self._build(
            **{
                ENV_AWS_SECRET_ID_KEY_TOKEN: "mcd/agent/token",
                ENV_AWS_SECRET_BASE64_ENCODED: "true",
            }
        )
        self.assertEqual(
            "true", provider._credentials_source.describe()[ATTR_NAME_BASE64_ENCODED]
        )

    def test_oauth_file_selected_over_token_file(self):
        provider = self._build(
            **{
                ENV_OAUTH_FILE_PATH: "/etc/secrets/credentials.json",
                ENV_TOKEN_FILE_PATH: "/etc/secrets/contents.json",
            }
        )
        self.assertIsInstance(provider, OAuthLoginTokenProvider)
        self.assertEqual(
            {
                ATTR_NAME_SOURCE: SOURCE_FILE,
                ATTR_NAME_FILE_PATH: "/etc/secrets/credentials.json",
            },
            provider._credentials_source.describe(),
        )

    def test_oauth_aws_secret_selected_over_token_aws_secret(self):
        provider = self._build(
            **{
                ENV_AWS_SECRET_ID_OAUTH: "mcd/agent/oauth",
                ENV_AWS_SECRET_ID_KEY_TOKEN: "mcd/agent/token",
            }
        )
        self.assertIsInstance(provider, OAuthLoginTokenProvider)
        self.assertEqual(
            "mcd/agent/oauth",
            provider._credentials_source.describe()[ATTR_NAME_SECRET_ID],
        )

    def test_oauth_prefers_aws_secret_over_file_when_both_set(self):
        provider = self._build(
            **{
                ENV_OAUTH_FILE_PATH: "/etc/secrets/credentials.json",
                ENV_AWS_SECRET_ID_OAUTH: "mcd/agent/oauth",
            }
        )
        self.assertEqual(
            SOURCE_AWS_SECRETS_MANAGER,
            provider._credentials_source.describe()[ATTR_NAME_SOURCE],
        )

    def test_token_prefers_aws_secret_over_file_when_both_set(self):
        provider = self._build(
            **{
                ENV_TOKEN_FILE_PATH: "/etc/secrets/contents.json",
                ENV_AWS_SECRET_ID_KEY_TOKEN: "mcd/agent/token",
            }
        )
        self.assertIsInstance(provider, TokenLoginTokenProvider)
        self.assertEqual(
            SOURCE_AWS_SECRETS_MANAGER,
            provider._credentials_source.describe()[ATTR_NAME_SOURCE],
        )

    def test_region_is_passed_to_the_aws_source(self):
        provider = self._build(
            **{
                ENV_AWS_SECRET_ID_KEY_TOKEN: "mcd/agent/token",
                ENV_AWS_SECRET_REGION: "eu-central-1",
            }
        )
        self.assertEqual(
            "eu-central-1",
            provider._credentials_source.describe()[ATTR_NAME_REGION],
        )

    def test_region_omitted_when_unset(self):
        provider = self._build(**{ENV_AWS_SECRET_ID_KEY_TOKEN: "mcd/agent/token"})
        self.assertNotIn(ATTR_NAME_REGION, provider._credentials_source.describe())

    def test_oauth_token_endpoint_override_is_honoured(self):
        provider = self._build(
            **{
                ENV_AWS_SECRET_ID_OAUTH: "mcd/agent/oauth",
                ENV_OAUTH_TOKEN_ENDPOINT: "https://m2m.example.com/oauth2/token",
            }
        )
        self.assertEqual(
            "https://m2m.example.com/oauth2/token", provider._token_endpoint
        )

    def test_oauth_endpoint_is_derived_when_not_overridden(self):
        provider = self._build(**{ENV_AWS_SECRET_ID_OAUTH: "mcd/agent/oauth"})
        self.assertEqual(
            "https://m2m.getmontecarlo.com/oauth2/token", provider._token_endpoint
        )

    def test_building_an_aws_source_does_not_contact_aws(self):
        # `main.py` constructs the service at module level, which is what
        # makes this run at import time — long before the pod's projected
        # IRSA token is guaranteed to be readable.
        with patch(
            "apollo.integrations.aws.asm_proxy_client.SecretsManagerProxyClient"
        ) as mock_client:
            self._build(**{ENV_AWS_SECRET_ID_KEY_TOKEN: "mcd/agent/token"})
            mock_client.assert_not_called()
