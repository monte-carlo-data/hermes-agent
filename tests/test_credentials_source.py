import base64
import json
import os
import tempfile
import threading
from unittest import TestCase
from unittest.mock import Mock, patch

from apollo.integrations.aws.asm_proxy_client import SecretsManagerProxyClient

from hermes.agent.service.credentials_source import (
    ATTR_NAME_BASE64_ENCODED,
    ATTR_NAME_FILE_PATH,
    ATTR_NAME_REGION,
    ATTR_NAME_SECRET_ID,
    ATTR_NAME_SOURCE,
    SOURCE_AWS_SECRETS_MANAGER,
    SOURCE_FILE,
    AwsSecretsManagerCredentialsSource,
    CredentialsSourceError,
    FileCredentialsSource,
)

_CREDS = {"client_id": "test-client-id", "client_secret": "test-client-secret"}


class FileCredentialsSourceTests(TestCase):
    def setUp(self):
        fd, self._path = tempfile.mkstemp(suffix=".json")
        with open(fd, "w") as f:
            json.dump(_CREDS, f)

    def tearDown(self):
        if os.path.exists(self._path):
            os.unlink(self._path)

    def test_reads_json_object(self):
        self.assertEqual(_CREDS, FileCredentialsSource(self._path).read())

    def test_describes_source_without_secret_values(self):
        described = FileCredentialsSource(self._path).describe()
        self.assertEqual(
            {ATTR_NAME_SOURCE: SOURCE_FILE, ATTR_NAME_FILE_PATH: self._path},
            described,
        )

    def test_missing_file_raises_with_path(self):
        os.unlink(self._path)
        with self.assertRaises(CredentialsSourceError) as ctx:
            FileCredentialsSource(self._path).read()
        self.assertIn(self._path, str(ctx.exception))

    def test_invalid_json_raises(self):
        with open(self._path, "w") as f:
            f.write("not json")
        with self.assertRaises(CredentialsSourceError):
            FileCredentialsSource(self._path).read()

    def test_base64_encoded_json_is_named_as_such(self):
        # Operators who can write secrets but not read them back have no
        # other view of this: the only symptom is a parse failure.
        with open(self._path, "w") as f:
            f.write(base64.b64encode(json.dumps(_CREDS).encode()).decode())
        with self.assertRaises(CredentialsSourceError) as ctx:
            FileCredentialsSource(self._path).read()
        self.assertIn("base64", str(ctx.exception))

    def test_base64_of_a_json_scalar_is_not_reported_as_base64(self):
        # _parse rejects anything but an object, so pointing at a decode here
        # would just swap one error for another.
        for value in (b"12345678", b'"a-bare-token-value"'):
            with self.subTest(value=value):
                with open(self._path, "w") as f:
                    f.write(base64.b64encode(value).decode())
                with self.assertRaises(CredentialsSourceError) as ctx:
                    FileCredentialsSource(self._path).read()
                self.assertNotIn("base64", str(ctx.exception))

    def test_non_json_is_not_reported_as_base64(self):
        # The hint has to stay quiet on payloads that merely fail to parse, or
        # it sends every misconfiguration down the wrong path.
        for payload in ("not json", "bm90IGpzb24gYXQgYWxs", "{oops:", "{}{}"):
            with self.subTest(payload=payload):
                with open(self._path, "w") as f:
                    f.write(payload)
                with self.assertRaises(CredentialsSourceError) as ctx:
                    FileCredentialsSource(self._path).read()
                self.assertNotIn("base64", str(ctx.exception))

    def test_json_scalar_is_rejected(self):
        with open(self._path, "w") as f:
            f.write('"a string"')
        with self.assertRaises(CredentialsSourceError):
            FileCredentialsSource(self._path).read()

    def test_directory_at_path_is_reported_as_such(self):
        # A bind mount whose host file is missing leaves a directory behind.
        os.unlink(self._path)
        os.mkdir(self._path)
        try:
            with self.assertRaises(CredentialsSourceError) as ctx:
                FileCredentialsSource(self._path).read()
            self.assertIn("directory", str(ctx.exception))
        finally:
            os.rmdir(self._path)


class AwsSecretsManagerCredentialsSourceTests(TestCase):
    def _source(self, client, **kwargs):
        source = AwsSecretsManagerCredentialsSource(secret_id="mcd/agent", **kwargs)
        source._client = client
        return source

    @staticmethod
    def _client(*side_effects):
        """Proxy-client mock returning each value from get_secret_value.

        A plain string (or None) becomes a SecretString response; pass a dict
        to control the whole response, or an exception to have it raised.
        """
        client = Mock(spec=SecretsManagerProxyClient)
        client.wrapped_client.get_secret_value.side_effect = [
            (
                value
                if isinstance(value, (dict, BaseException))
                else {"SecretString": value}
            )
            for value in side_effects
        ]
        return client

    def test_reads_and_parses_secret(self):
        client = self._client(json.dumps(_CREDS))
        self.assertEqual(_CREDS, self._source(client).read())
        client.wrapped_client.get_secret_value.assert_called_once_with(
            SecretId="mcd/agent"
        )

    def test_second_read_inside_ttl_is_served_from_cache(self):
        client = self._client(json.dumps(_CREDS))
        source = self._source(client)
        source.read()
        source.read()
        self.assertEqual(1, client.wrapped_client.get_secret_value.call_count)

    def test_read_after_ttl_expiry_refetches(self):
        rotated = {"client_id": "rotated", "client_secret": "rotated-secret"}
        client = self._client(json.dumps(_CREDS), json.dumps(rotated))
        source = self._source(client, cache_ttl_seconds=0)
        self.assertEqual(_CREDS, source.read())
        self.assertEqual(rotated, source.read())
        self.assertEqual(2, client.wrapped_client.get_secret_value.call_count)

    def test_refresh_failure_serves_cached_value(self):
        client = self._client(json.dumps(_CREDS), RuntimeError("throttled"))
        source = self._source(client, cache_ttl_seconds=0)
        self.assertEqual(_CREDS, source.read())
        # Refresh fails, but the cached credential is still valid.
        self.assertEqual(_CREDS, source.read())

    def test_first_read_failure_raises(self):
        source = self._source(self._client(RuntimeError("access denied")))
        with self.assertRaises(CredentialsSourceError) as ctx:
            source.read()
        self.assertIn("mcd/agent", str(ctx.exception))
        self.assertIn("access denied", str(ctx.exception))

    def test_invalid_json_raises(self):
        source = self._source(self._client("not json"))
        with self.assertRaises(CredentialsSourceError):
            source.read()

    def test_describes_secret_id_and_region_without_secret_values(self):
        source = AwsSecretsManagerCredentialsSource(
            secret_id="mcd/agent", region="us-east-1"
        )
        self.assertEqual(
            {
                ATTR_NAME_SOURCE: SOURCE_AWS_SECRETS_MANAGER,
                ATTR_NAME_SECRET_ID: "mcd/agent",
                ATTR_NAME_REGION: "us-east-1",
            },
            source.describe(),
        )

    def test_region_omitted_from_description_when_unset(self):
        source = AwsSecretsManagerCredentialsSource(secret_id="mcd/agent")
        self.assertNotIn(ATTR_NAME_REGION, source.describe())

    @patch(
        "apollo.integrations.aws.asm_proxy_client.SecretsManagerProxyClient",
        autospec=True,
    )
    def test_client_is_built_lazily_and_reused(self, mock_client_cls):
        mock_client_cls.return_value.wrapped_client.get_secret_value.return_value = {
            "SecretString": json.dumps(_CREDS)
        }
        source = AwsSecretsManagerCredentialsSource(
            secret_id="mcd/agent", region="us-west-2", cache_ttl_seconds=0
        )
        # Constructing the source must not resolve IRSA credentials: the pod's
        # projected token may not exist yet at import time.
        mock_client_cls.assert_not_called()
        source.read()
        source.read()
        mock_client_cls.assert_called_once_with(credentials={"aws_region": "us-west-2"})

    def test_concurrent_reads_single_flight_the_fetch(self):
        # The lock in read() is held across _fetch(), so only one thread ever
        # has a Secrets Manager call in flight; every other reader blocks on
        # the lock and then gets the value that call produced. This pins that
        # property: moving _fetch() outside the lock — the "obvious cleanup"
        # of not blocking readers on a network call — would let every reader
        # fire its own GetSecretValue call instead of sharing one, which is
        # what makes a long cache TTL viable in the first place.
        release = threading.Event()
        call_count = 0
        count_lock = threading.Lock()

        def slow_get_secret_value(**kwargs):
            nonlocal call_count
            with count_lock:
                call_count += 1
            release.wait(timeout=5)
            return {"SecretString": json.dumps(_CREDS)}

        client = Mock(spec=SecretsManagerProxyClient)
        client.wrapped_client.get_secret_value.side_effect = slow_get_secret_value
        source = self._source(client)

        results = []

        def reader():
            results.append(source.read())

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        release.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(1, call_count)
        self.assertEqual([_CREDS, _CREDS], results)


class AwsSecretsManagerFailureBackoffTests(TestCase):
    """A sustained Secrets Manager failure must not retry on every read.

    The key/token provider reads its credential on every outbound backend
    request, so without a retry floor a throttled or unreachable Secrets
    Manager becomes one GetSecretValue attempt per operation — against the
    API that is already failing — while the lock is held across each call.
    """

    @staticmethod
    def _client(*side_effects):
        """Proxy-client mock returning each value from get_secret_value.

        A plain string (or None) becomes a SecretString response; pass a dict
        to control the whole response, or an exception to have it raised.
        """
        client = Mock(spec=SecretsManagerProxyClient)
        client.wrapped_client.get_secret_value.side_effect = [
            (
                value
                if isinstance(value, (dict, BaseException))
                else {"SecretString": value}
            )
            for value in side_effects
        ]
        return client

    def _source(self, client, **kwargs):
        source = AwsSecretsManagerCredentialsSource(secret_id="mcd/agent", **kwargs)
        source._client = client
        return source

    def test_sustained_failure_with_warm_cache_backs_off(self):
        client = self._client(json.dumps(_CREDS), *[RuntimeError("throttled")] * 20)
        source = self._source(client, cache_ttl_seconds=0)
        source.read()

        for _ in range(20):
            self.assertEqual(_CREDS, source.read())

        # One successful fetch plus at most one retry: the failure must open a
        # backoff window rather than being re-attempted per read.
        self.assertLessEqual(client.wrapped_client.get_secret_value.call_count, 2)

    def test_sustained_failure_from_cold_cache_backs_off(self):
        client = self._client(*[RuntimeError("access denied")] * 20)
        source = self._source(client)

        for _ in range(20):
            with self.assertRaises(CredentialsSourceError):
                source.read()

        # Nothing is cached, so read() must keep raising — but it must not
        # hammer the API once per call.
        self.assertLessEqual(client.wrapped_client.get_secret_value.call_count, 2)

    def test_unusable_payload_backs_off(self):
        # A malformed payload is the case where the GetSecretValue call itself
        # succeeds, so "retrying can't fix it" is not a reason to skip the
        # backoff window — without one, every backend request pays for a real
        # API call to re-discover the same bad value.
        client = self._client(*["not json"] * 20)
        source = self._source(client)

        for _ in range(20):
            with self.assertRaises(CredentialsSourceError):
                source.read()

        self.assertLessEqual(client.wrapped_client.get_secret_value.call_count, 2)

    def test_unusable_payload_serves_cached_value(self):
        # Rotating a valid secret to a malformed one must not take the agent
        # down: the cached credential is still accepted by the backend until
        # it is actually revoked.
        client = self._client(json.dumps(_CREDS), "not json")
        source = self._source(client, cache_ttl_seconds=0)
        self.assertEqual(_CREDS, source.read())
        self.assertEqual(_CREDS, source.read())


class AwsSecretsManagerEncodingTests(TestCase):
    """How the payload is stored: SecretString, SecretBinary, or base64 text."""

    @staticmethod
    def _source(response, **kwargs):
        source = AwsSecretsManagerCredentialsSource(secret_id="mcd/agent", **kwargs)
        client = Mock(spec=SecretsManagerProxyClient)
        client.wrapped_client.get_secret_value.return_value = response
        source._client = client
        return source

    def test_binary_secret_is_read_without_a_flag(self):
        # --secret-binary stores valid UTF-8 JSON, and string and binary are
        # separate fields of one response, so no flag is needed.
        source = self._source({"SecretBinary": json.dumps(_CREDS).encode()})
        self.assertEqual(_CREDS, source.read())

    def test_binary_and_string_are_read_in_one_api_call(self):
        source = self._source({"SecretBinary": json.dumps(_CREDS).encode()})
        source.read()
        self.assertEqual(1, source._client.wrapped_client.get_secret_value.call_count)

    def test_binary_secret_that_is_not_utf8_raises(self):
        source = self._source({"SecretBinary": bytes([0xFF, 0xFE, 0x00])})
        with self.assertRaises(CredentialsSourceError) as ctx:
            source.read()
        self.assertIn("not UTF-8", str(ctx.exception))

    def test_base64_encoded_secret_is_decoded_when_enabled(self):
        encoded = base64.b64encode(json.dumps(_CREDS).encode()).decode()
        source = self._source({"SecretString": encoded}, base64_encoded=True)
        self.assertEqual(_CREDS, source.read())

    def test_base64_decoding_is_opt_in(self):
        # The same payload must fail without the flag: decoding on sight
        # would accept a wrong value that happens to be valid base64.
        encoded = base64.b64encode(json.dumps(_CREDS).encode()).decode()
        source = self._source({"SecretString": encoded})
        with self.assertRaises(CredentialsSourceError) as ctx:
            source.read()
        self.assertIn("base64", str(ctx.exception))

    def test_non_base64_value_with_decoding_enabled_raises(self):
        source = self._source({"SecretString": "{not base64}"}, base64_encoded=True)
        with self.assertRaises(CredentialsSourceError) as ctx:
            source.read()
        self.assertIn("not valid base64", str(ctx.exception))

    def test_doubly_encoded_value_is_named_as_such(self):
        # Decoding is already on, so "enable base64 decoding" would tell the
        # operator to do what they have done; the remedy is the second layer.
        inner = base64.b64encode(json.dumps(_CREDS).encode()).decode()
        source = self._source(
            {"SecretString": base64.b64encode(inner.encode()).decode()},
            base64_encoded=True,
        )
        with self.assertRaises(CredentialsSourceError) as ctx:
            source.read()
        self.assertIn("doubly encoded", str(ctx.exception))
        self.assertNotIn("enable base64 decoding", str(ctx.exception))

    def test_base64_decoding_is_reported(self):
        source = AwsSecretsManagerCredentialsSource(
            secret_id="mcd/agent", base64_encoded=True
        )
        self.assertEqual("true", source.describe()[ATTR_NAME_BASE64_ENCODED])

    def test_base64_decoding_is_absent_from_the_description_when_off(self):
        source = AwsSecretsManagerCredentialsSource(secret_id="mcd/agent")
        self.assertNotIn(ATTR_NAME_BASE64_ENCODED, source.describe())
