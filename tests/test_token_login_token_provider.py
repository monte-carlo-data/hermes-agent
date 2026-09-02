from unittest import TestCase
from unittest.mock import Mock

from apollo.egress.agent.service.login_token_provider import (
    ATTR_NAME_AUTH_METHOD,
    ATTR_NAME_KEY_ID,
)
from apollo.egress.agent.utils.utils import X_MCD_ID, X_MCD_TOKEN

from hermes.agent.service.credentials_source import (
    ATTR_NAME_SECRET_ID,
    ATTR_NAME_SOURCE,
    SOURCE_AWS_SECRETS_MANAGER,
    CredentialsSourceError,
)
from hermes.agent.service.token_login_token_provider import TokenLoginTokenProvider

_CREDS = {"mcd_id": "an-mcd-id", "mcd_token": "an-mcd-token"}


class TokenLoginTokenProviderTests(TestCase):
    @staticmethod
    def _source(read_result=None, read_error=None, describe=None):
        source = Mock()
        source.source_name = SOURCE_AWS_SECRETS_MANAGER
        if read_error is not None:
            source.read.side_effect = read_error
        else:
            source.read.return_value = read_result
        source.describe.return_value = describe or {
            ATTR_NAME_SOURCE: SOURCE_AWS_SECRETS_MANAGER,
            ATTR_NAME_SECRET_ID: "mcd/agent/token",
        }
        return source

    def test_sends_id_and_token_headers(self):
        provider = TokenLoginTokenProvider(self._source(_CREDS))
        self.assertEqual(
            {X_MCD_ID: "an-mcd-id", X_MCD_TOKEN: "an-mcd-token"},
            provider.get_token(),
        )

    def test_authentication_method_names_the_source(self):
        provider = TokenLoginTokenProvider(self._source(_CREDS))
        self.assertEqual("token_aws_secrets_manager", provider.authentication_method)

    def test_credential_id_is_the_mcd_id(self):
        provider = TokenLoginTokenProvider(self._source(_CREDS))
        self.assertEqual("an-mcd-id", provider.get_credential_id())

    def test_credential_info_reports_source_and_id_but_not_the_token(self):
        provider = TokenLoginTokenProvider(self._source(_CREDS))

        credential_info = provider.get_credential_info()

        self.assertEqual(
            {
                ATTR_NAME_KEY_ID: "an-mcd-id",
                ATTR_NAME_AUTH_METHOD: "token_aws_secrets_manager",
                ATTR_NAME_SOURCE: SOURCE_AWS_SECRETS_MANAGER,
                ATTR_NAME_SECRET_ID: "mcd/agent/token",
            },
            credential_info,
        )
        self.assertNotIn("an-mcd-token", str(credential_info))

    def test_unreadable_credential_yields_no_token_sentinels(self):
        provider = TokenLoginTokenProvider(
            self._source(read_error=CredentialsSourceError("access denied"))
        )
        self.assertEqual(
            {X_MCD_ID: "no-token-id", X_MCD_TOKEN: "no-token-secret"},
            provider.get_token(),
        )

    def test_unreadable_credential_reports_no_token_id(self):
        provider = TokenLoginTokenProvider(
            self._source(read_error=CredentialsSourceError("access denied"))
        )
        self.assertEqual("no-token-id", provider.get_credential_id())

    def test_credential_missing_expected_keys_yields_sentinels(self):
        provider = TokenLoginTokenProvider(
            self._source({"client_id": "wrong", "client_secret": "shape"})
        )
        self.assertEqual(
            {X_MCD_ID: "no-token-id", X_MCD_TOKEN: "no-token-secret"},
            provider.get_token(),
        )

    def test_credential_id_never_raises_on_unexpected_source_failure(self):
        # get_credential_id is called while authentication is already failing,
        # so a source raising something the source layer does not convert —
        # e.g. boto failing to resolve IRSA credentials — must not take the
        # reachability report down with it.
        provider = TokenLoginTokenProvider(
            self._source(read_error=RuntimeError("boto blew up"))
        )
        self.assertEqual("no-token-id", provider.get_credential_id())

    def test_get_token_never_raises_on_unexpected_source_failure(self):
        provider = TokenLoginTokenProvider(
            self._source(read_error=RuntimeError("boto blew up"))
        )
        self.assertEqual(
            {X_MCD_ID: "no-token-id", X_MCD_TOKEN: "no-token-secret"},
            provider.get_token(),
        )

    def test_empty_string_credentials_yield_no_token_sentinels(self):
        # A templated secret created before its variables are populated is a
        # plausible secret-manager payload — both keys present, both empty.
        # The old guard (`key in credentials`) let this through; get_token()
        # and get_credential_id() must still report the sentinels.
        provider = TokenLoginTokenProvider(
            self._source({"mcd_id": "", "mcd_token": ""})
        )
        self.assertEqual(
            {X_MCD_ID: "no-token-id", X_MCD_TOKEN: "no-token-secret"},
            provider.get_token(),
        )
        self.assertEqual("no-token-id", provider.get_credential_id())

    def test_null_credentials_yield_no_token_sentinels(self):
        # An explicit `null` is worse than an empty string: unguarded, it
        # would reach `requests` as a non-string header value.
        provider = TokenLoginTokenProvider(
            self._source({"mcd_id": None, "mcd_token": None})
        )
        self.assertEqual(
            {X_MCD_ID: "no-token-id", X_MCD_TOKEN: "no-token-secret"},
            provider.get_token(),
        )
        self.assertEqual("no-token-id", provider.get_credential_id())
