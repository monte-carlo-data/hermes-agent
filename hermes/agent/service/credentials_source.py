"""Sources the agent's own backend credentials, independent of auth method.

Both authentication methods need the same thing — a JSON object holding a
credential — and differ only in which keys they expect. Separating *where* the
credential comes from lets a new source be added without touching either token
provider.

The file source covers the Kubernetes Secret and Docker bind-mount cases. The
AWS Secrets Manager source removes the Kubernetes Secret from the picture
entirely: the agent reads the credential itself using the pod's own AWS
identity, so a deployment that cannot use the External Secrets Operator does
not have to materialize the credential in the cluster at all. That is the same
mechanism the agent already uses to read self-hosted *integration* credentials
from AWS Secrets Manager (`AwsSecretsManagerCredentialsService`) — this
applies it to the agent's own credential.

A reviewer proposed replacing this module with agent-common's
`apollo.credentials` layer instead. That was declined: `apollo.credentials` is
shaped for self-hosted integration credentials, where the credentials dict
doubles as both the resolution parameters and the cache identity (its cache
key is a sha256 of that dict minus `connect_args`), so a change made there for
that use case could silently alter behaviour here. Its cache also documents
having no single-flight, justified by a "one first call, then warm cache"
traffic pattern that does not hold for a credential read on every backend
request, and it has no serve-stale-on-failure.
"""

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Attribute names used to report the credential source in health information
# and in failed reachability tests. Never carry a secret value.
ATTR_NAME_SOURCE = "credentials_source"
ATTR_NAME_FILE_PATH = "credentials_file_path"
ATTR_NAME_SECRET_ID = "credentials_secret_id"
ATTR_NAME_REGION = "credentials_region"

SOURCE_FILE = "file"
SOURCE_AWS_SECRETS_MANAGER = "aws_secrets_manager"

# A secret manager read costs a network round trip, and the key/token provider
# reads its credential on every request to the backend. Without a cache that is
# one API call per operation. Fifteen minutes bounds how long a rotated
# credential can go unnoticed while staying well inside the read quotas — for
# comparison, the External Secrets Operator path this replaces defaults to
# refreshing hourly.
DEFAULT_CACHE_TTL_SECONDS = 900

# After a failed read, wait before trying again. Without this the entry stays
# stale and every subsequent read re-attempts — one API call per backend
# request, against an API that is already failing. Well under the TTL so a
# rotated credential is still picked up promptly.
RETRY_AFTER_FAILURE_SECONDS = 60

# Ceiling on how long a cached credential is served after its last successful
# fetch, even while every refresh keeps failing. Without this, an operator who
# detaches the pod's IAM policy to contain a suspected compromise finds the
# agent keeps authenticating for the life of the pod — IAM revocation stops
# being the lever they'd assume it is. A small multiple of the TTL bounds that
# window while still riding out a sustained transient outage.
DEFAULT_MAX_STALE_SECONDS = 3600


class CredentialsSourceError(Exception):
    """The credential could not be read, or is not usable as JSON."""


class CredentialsSource(ABC):
    """Reads a JSON credential object from somewhere."""

    #: Non-secret label naming this kind of source.
    source_name: str

    @abstractmethod
    def read(self) -> Dict[str, Any]:
        """Return the credential as a dict; raises CredentialsSourceError if
        the credential cannot be read."""

    def describe(self) -> Dict[str, str]:
        """Return non-secret attributes identifying where the credential lives.

        Surfaced in reachability output, so an operator can tell a misconfigured
        source from a rejected credential without the agent echoing either.
        """
        return {ATTR_NAME_SOURCE: self.source_name}

    @staticmethod
    def _parse(raw: str, origin: str) -> Dict[str, Any]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise CredentialsSourceError(f"Credentials are not valid JSON: {origin}")
        if not isinstance(data, dict):
            raise CredentialsSourceError(f"Credentials must be a JSON object: {origin}")
        return data


class FileCredentialsSource(CredentialsSource):
    """Reads the credential from a JSON file on disk."""

    source_name = SOURCE_FILE

    def __init__(self, file_path: str):
        self._file_path = file_path

    @property
    def file_path(self) -> str:
        """The path this source reads.

        Exposed because the factory hands the key/token file case to
        agent-common's ``FileLoginTokenProvider``, which takes a path rather
        than a source — reading it back off the resolved source keeps the
        source-precedence rule in one place instead of re-reading the
        environment at the construction site.
        """
        return self._file_path

    def read(self) -> Dict[str, Any]:
        try:
            with open(self._file_path) as f:
                raw = f.read()
        except FileNotFoundError:
            raise CredentialsSourceError(
                f"Credentials file not found: {self._file_path}"
            )
        except PermissionError:
            raise CredentialsSourceError(
                f"Cannot read credentials file (permission denied): {self._file_path}"
            )
        except IsADirectoryError:
            # A bind mount whose host file is missing leaves a directory at the
            # container path, which is a misconfigured mount rather than a
            # missing credential — worth distinguishing in the message.
            raise CredentialsSourceError(
                f"Credentials path is a directory, not a file: {self._file_path}"
            )
        except UnicodeDecodeError:
            raise CredentialsSourceError(
                f"Credentials file is not valid UTF-8: {self._file_path}"
            )
        return self._parse(raw, self._file_path)

    def describe(self) -> Dict[str, str]:
        return {**super().describe(), ATTR_NAME_FILE_PATH: self._file_path}


class AwsSecretsManagerCredentialsSource(CredentialsSource):
    """Reads the credential from AWS Secrets Manager, caching it briefly.

    The boto client is built lazily and then reused: constructing it resolves
    the pod's AWS credentials, which is not possible before the cluster's AWS
    identity provider (EKS Pod Identity or IRSA) is reachable, so it must not
    happen at import time. Both work here — boto resolves either through the
    standard credential chain, so nothing here depends on which is in use.
    """

    source_name = SOURCE_AWS_SECRETS_MANAGER

    def __init__(
        self,
        secret_id: str,
        region: Optional[str] = None,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        max_stale_seconds: float = DEFAULT_MAX_STALE_SECONDS,
    ):
        self._secret_id = secret_id
        self._region = region
        self._cache_ttl_seconds = cache_ttl_seconds
        self._max_stale_seconds = max_stale_seconds
        self._lock = threading.Lock()
        self._client: Optional[Any] = None
        self._cached: Optional[Dict[str, Any]] = None
        self._fetched_at: float = 0.0
        # Opens after a failed fetch; while it's open, read() serves the
        # cache (or raises) without calling _fetch() again. See
        # RETRY_AFTER_FAILURE_SECONDS.
        self._retry_after: float = 0.0
        self._last_failure: str = ""

    def read(self) -> Dict[str, Any]:
        with self._lock:
            if self._cached is not None and not self._is_stale():
                return self._cached

            if self._in_backoff_window():
                # A previous failure already opened the backoff window: don't
                # hit an API that's already failing again, and don't log
                # again either — once per window, not once per read.
                return self._serve_stale_or_raise(log=False)

            try:
                fetched = self._fetch()
            except Exception as ex:
                # Payload errors (binary secret, malformed JSON) back off too:
                # the API call succeeds and only parsing fails, so retrying
                # can't fix it but still costs a call per backend request.
                self._last_failure = str(ex)
                self._retry_after = time.monotonic() + RETRY_AFTER_FAILURE_SECONDS
                return self._serve_stale_or_raise(log=True)

            self._cached = fetched
            self._fetched_at = time.monotonic()
            self._retry_after = 0.0
            return self._cached

    def _serve_stale_or_raise(self, log: bool) -> Dict[str, Any]:
        """Handle a failed (or backed-off) fetch attempt under `self._lock`.

        Serves the cached credential if one exists and it isn't older than
        `_max_stale_seconds`; otherwise raises, since there is nothing safe to
        serve.
        """
        if self._cached is not None and not self._is_over_max_stale():
            if log:
                # Serving a slightly stale credential beats failing every
                # operation over a transient API error; the credential is
                # still valid until it is rotated.
                logger.warning(
                    f"Failed to refresh secret {self._secret_id} from AWS "
                    f"Secrets Manager, using cached value: {self._last_failure}"
                )
            return self._cached

        if log:
            logger.warning(
                f"Failed to read secret {self._secret_id} from AWS Secrets "
                f"Manager: {self._last_failure}"
            )

        if self._cached is not None:
            raise CredentialsSourceError(
                f"Cached credential for secret {self._secret_id} from AWS "
                f"Secrets Manager is older than the allowed staleness window "
                f"of {self._max_stale_seconds:.0f}s; last failure: "
                f"{self._last_failure}"
            )
        raise CredentialsSourceError(
            f"Failed to read secret {self._secret_id} from AWS Secrets "
            f"Manager: {self._last_failure} (last attempt failed and a retry "
            f"is pending)"
        )

    def describe(self) -> Dict[str, str]:
        described = {**super().describe(), ATTR_NAME_SECRET_ID: self._secret_id}
        if self._region:
            described[ATTR_NAME_REGION] = self._region
        return described

    def _is_stale(self) -> bool:
        return time.monotonic() - self._fetched_at >= self._cache_ttl_seconds

    def _is_over_max_stale(self) -> bool:
        return time.monotonic() - self._fetched_at > self._max_stale_seconds

    def _in_backoff_window(self) -> bool:
        return time.monotonic() < self._retry_after

    def _fetch(self) -> Dict[str, Any]:
        # Runs under self._lock (see read()), which is what makes single-
        # flight refresh possible: only one thread ever has a Secrets Manager
        # call in flight, and every other reader blocks on the lock and then
        # gets the value that call produced, instead of each firing its own
        # call. Moving this call outside the lock would remove that
        # serialization and defeat the point of caching with a long TTL.
        #
        # get_secret_string() -> SecretsManagerProxyClient ->
        # BaseAwsProxyClient.create_boto_client() calls session.client(...)
        # with no botocore.config.Config and no way to pass kwargs through to
        # it, so connect/read timeouts are botocore's 60s defaults — a single
        # slow call can hold this lock for minutes. Bounding that needs a
        # change in agent-common (apollo), not here.
        raw = self._get_client().get_secret_string(self._secret_id)
        if not raw:
            raise CredentialsSourceError(
                f"Secret {self._secret_id} has no string value — a binary secret "
                f"cannot hold agent credentials"
            )
        return self._parse(raw, f"AWS Secrets Manager secret {self._secret_id}")

    def _get_client(self):
        if self._client is None:
            # Imported here rather than at module scope so environments without
            # the AWS extras can still use the file source.
            from apollo.integrations.aws.asm_proxy_client import (
                SecretsManagerProxyClient,
            )

            self._client = SecretsManagerProxyClient(
                credentials={"aws_region": self._region} if self._region else None
            )
        return self._client
