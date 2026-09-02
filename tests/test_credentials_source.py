import json
import os
import tempfile
import threading
from unittest import TestCase
from unittest.mock import Mock, patch

from apollo.integrations.aws.asm_proxy_client import SecretsManagerProxyClient

from hermes.agent.service.credentials_source import (
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
        client = Mock(spec=SecretsManagerProxyClient)
        client.get_secret_string.side_effect = side_effects
        return client

    def test_reads_and_parses_secret(self):
        client = self._client(json.dumps(_CREDS))
        self.assertEqual(_CREDS, self._source(client).read())
        client.get_secret_string.assert_called_once_with("mcd/agent")

    def test_second_read_inside_ttl_is_served_from_cache(self):
        client = self._client(json.dumps(_CREDS))
        source = self._source(client)
        source.read()
        source.read()
        self.assertEqual(1, client.get_secret_string.call_count)

    def test_read_after_ttl_expiry_refetches(self):
        rotated = {"client_id": "rotated", "client_secret": "rotated-secret"}
        client = self._client(json.dumps(_CREDS), json.dumps(rotated))
        source = self._source(client, cache_ttl_seconds=0)
        self.assertEqual(_CREDS, source.read())
        self.assertEqual(rotated, source.read())
        self.assertEqual(2, client.get_secret_string.call_count)

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

    def test_binary_secret_raises(self):
        source = self._source(self._client(None))
        with self.assertRaises(CredentialsSourceError):
            source.read()

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
        mock_client_cls.return_value.get_secret_string.return_value = json.dumps(_CREDS)
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

        def slow_get_secret_string(secret_id):
            nonlocal call_count
            with count_lock:
                call_count += 1
            release.wait(timeout=5)
            return json.dumps(_CREDS)

        client = Mock(spec=SecretsManagerProxyClient)
        client.get_secret_string.side_effect = slow_get_secret_string
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
        client = Mock(spec=SecretsManagerProxyClient)
        client.get_secret_string.side_effect = side_effects
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
        self.assertLessEqual(client.get_secret_string.call_count, 2)

    def test_sustained_failure_from_cold_cache_backs_off(self):
        client = self._client(*[RuntimeError("access denied")] * 20)
        source = self._source(client)

        for _ in range(20):
            with self.assertRaises(CredentialsSourceError):
                source.read()

        # Nothing is cached, so read() must keep raising — but it must not
        # hammer the API once per call.
        self.assertLessEqual(client.get_secret_string.call_count, 2)
