"""Sources the agent's own backend credentials, independent of auth method.

Both authentication methods want a JSON object holding a credential and differ
only in which keys they expect, so separating *where* it comes from lets a new
source be added without touching either token provider.

The file source covers the Kubernetes Secret and Docker bind-mount cases. The
AWS Secrets Manager sources read the credential with the pod's own AWS
identity, so a deployment without the External Secrets Operator need not
materialize it in the cluster at all.

Not built on agent-common's `apollo.credentials`, which is shaped for
self-hosted integration credentials: its credentials dict doubles as both
resolution parameters and cache identity, its cache documents having no
single-flight, and it has no serve-stale-on-failure — none of which suit a
credential read on every backend request.
"""

import base64
import binascii
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
ATTR_NAME_SECRET_IDS = "credentials_secret_ids"
ATTR_NAME_REGION = "credentials_region"
ATTR_NAME_BASE64_ENCODED = "credentials_base64_encoded"

SOURCE_FILE = "file"
SOURCE_AWS_SECRETS_MANAGER = "aws_secrets_manager"

# The key/token provider reads its credential on every backend request, so
# without a cache that is one API call per operation. Fifteen minutes bounds
# rotation lag — the ESO path this replaces refreshes hourly.
DEFAULT_CACHE_TTL_SECONDS = 900

# Wait this long after a failed read before trying again, so a failing API
# isn't hit once per backend request. Well under the TTL.
RETRY_AFTER_FAILURE_SECONDS = 60

# Ceiling on serving a cached credential while refreshes keep failing. Without
# it, detaching the pod's IAM policy to contain a compromise would not stop the
# agent for the life of the pod.
DEFAULT_MAX_STALE_SECONDS = 3600


class CredentialsSourceError(Exception):
    """The credential could not be read, or is not usable as JSON."""


def _base64_hint(raw: str, already_decoded: bool = False) -> str:
    """Return a hint when `raw` is base64 that decodes to a JSON object, else "".

    Hinted rather than decoded on sight: base64 text is a valid string value,
    so decoding anything that looks like it would hide real misconfigurations.
    Worth detecting at all because a deployment whose operators can write
    secrets but not read them back has no other view of what was stored.

    `already_decoded` says the caller has decoded once, which changes the
    remedy: the value is doubly encoded, not merely encoded.
    """
    candidate = "".join(raw.split())
    # Shorter than this cannot be base64 of a JSON object, and short strings
    # ("null", "true") are valid base64 alphabet often enough to mislead.
    if len(candidate) < 8:
        return ""
    try:
        decoded = json.loads(base64.b64decode(candidate, validate=True))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return ""
    # Only an object is worth pointing at: _parse rejects anything else, so
    # advising a decode for base64 of a scalar just swaps one error for
    # another.
    if not isinstance(decoded, dict):
        return ""
    if already_decoded:
        return (
            " — the value is still base64 after decoding once, so it looks "
            "doubly encoded; store it encoded at most once"
        )
    return (
        " — the value looks like base64-encoded JSON. Store the decoded JSON "
        "instead, or enable base64 decoding for this credential source"
    )


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
    def _parse(raw: str, origin: str, base64_decoded: bool = False) -> Dict[str, Any]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise CredentialsSourceError(
                f"Credentials are not valid JSON: {origin}"
                f"{_base64_hint(raw, base64_decoded)}"
            )
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
        than a source; reading it back off the resolved source keeps source
        precedence in one place.
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
            # A bind mount whose host file is missing leaves a directory at
            # the container path — a broken mount, not a missing credential.
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

    `secret_id` names one secret holding the whole credential as JSON, which
    keeps rotation atomic. `secret_ids` instead maps each credential field to
    its own secret holding a bare value, for conventions that allow only one
    value per secret; rotation is not atomic there, since the fields are
    separate API calls and one landing mid-rotation can pair a new value with
    an old one until the next refresh.

    Either way a refresh produces the whole credential under one lock, so the
    cache never holds a partially refreshed one.

    The boto client is built lazily and then reused: constructing it resolves
    the pod's AWS credentials, which is not possible before the cluster's AWS
    identity provider (EKS Pod Identity or IRSA) is reachable, so it must not
    happen at import time. Both work here — boto resolves either through the
    standard credential chain, so nothing here depends on which is in use.
    """

    source_name = SOURCE_AWS_SECRETS_MANAGER

    def __init__(
        self,
        secret_id: Optional[str] = None,
        secret_ids: Optional[Dict[str, str]] = None,
        region: Optional[str] = None,
        base64_encoded: bool = False,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        max_stale_seconds: float = DEFAULT_MAX_STALE_SECONDS,
    ):
        if bool(secret_id) == bool(secret_ids):
            raise ValueError(
                "pass either secret_id, for one secret holding the whole "
                "credential, or secret_ids, mapping each credential field to "
                "its own secret"
            )
        self._secret_id = secret_id
        self._secret_ids = secret_ids or {}
        self._region = region
        self._base64_encoded = base64_encoded
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
                    f"Failed to refresh {self._target} from AWS Secrets "
                    f"Manager, using cached value: {self._last_failure}"
                )
            return self._cached

        if log:
            logger.warning(
                f"Failed to read {self._target} from AWS Secrets "
                f"Manager: {self._last_failure}"
            )

        if self._cached is not None:
            raise CredentialsSourceError(
                f"Cached credential for {self._target} from AWS "
                f"Secrets Manager is older than the allowed staleness window "
                f"of {self._max_stale_seconds:.0f}s; last failure: "
                f"{self._last_failure}"
            )
        raise CredentialsSourceError(
            f"Failed to read {self._target} from AWS Secrets "
            f"Manager: {self._last_failure} (last attempt failed and a retry "
            f"is pending)"
        )

    def describe(self) -> Dict[str, str]:
        described = {**super().describe()}
        if self._secret_ids:
            described[ATTR_NAME_SECRET_IDS] = ", ".join(
                f"{field}={secret_id}"
                for field, secret_id in sorted(self._secret_ids.items())
            )
        else:
            described[ATTR_NAME_SECRET_ID] = str(self._secret_id)
        if self._region:
            described[ATTR_NAME_REGION] = self._region
        if self._base64_encoded:
            described[ATTR_NAME_BASE64_ENCODED] = "true"
        return described

    @property
    def _target(self) -> str:
        """What is being read, for log and error messages. Never a secret."""
        if self._secret_ids:
            return f"secrets {', '.join(sorted(self._secret_ids.values()))}"
        return f"secret {self._secret_id}"

    def _is_stale(self) -> bool:
        return time.monotonic() - self._fetched_at >= self._cache_ttl_seconds

    def _is_over_max_stale(self) -> bool:
        return time.monotonic() - self._fetched_at > self._max_stale_seconds

    def _in_backoff_window(self) -> bool:
        return time.monotonic() < self._retry_after

    def _fetch(self) -> Dict[str, Any]:
        if self._secret_ids:
            return self._fetch_fields()
        raw = self._read_secret(str(self._secret_id))
        return self._parse(
            raw,
            f"AWS Secrets Manager secret {self._secret_id}",
            base64_decoded=self._base64_encoded,
        )

    def _fetch_fields(self) -> Dict[str, Any]:
        values: Dict[str, Any] = {}
        for field, secret_id in self._secret_ids.items():
            try:
                # Stripped: a single-value secret created from a file or
                # pasted into a console easily picks up a trailing newline.
                values[field] = self._read_secret(secret_id).strip()
            except CredentialsSourceError as ex:
                # _read_secret names the secret; only this loop knows which
                # field it was supplying.
                raise CredentialsSourceError(f"{ex}, for credential field '{field}'")
        return values

    def _read_secret(self, secret_id: str) -> str:
        """Return one secret's value as text, whatever it is stored as.

        Runs under `self._lock`, which single-flights the refresh — pinned by
        test_concurrent_reads_single_flight_the_fetch. Reaches past
        get_secret_string(), which discards SecretBinary, so one call carries
        both fields. Timeouts are botocore's 60s defaults, so a slow call holds
        the lock for minutes; bounding that needs an agent-common change.
        """
        response = self._get_client().wrapped_client.get_secret_value(
            SecretId=secret_id
        )
        raw = response.get("SecretString")
        if raw is None:
            raw = self._decode_binary(response, secret_id)
        # Kept distinct from the cases above: a secret created by one tool and
        # populated by another is empty until the second runs, and calling
        # that a missing or binary value misdirects.
        if not raw.strip():
            raise CredentialsSourceError(
                f"Secret {secret_id} exists but has no value yet"
            )
        if self._base64_encoded:
            raw = self._decode_base64(raw, secret_id)
        return raw

    @staticmethod
    def _decode_binary(response: Dict[str, Any], secret_id: str) -> str:
        """Return the response's SecretBinary payload, decoded as UTF-8."""
        binary = response.get("SecretBinary")
        if binary is None:
            raise CredentialsSourceError(
                f"Secret {secret_id} has neither a string nor a binary value"
            )
        try:
            return binary.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            raise CredentialsSourceError(
                f"Secret {secret_id} holds binary data that is not UTF-8 text, "
                f"so it cannot hold agent credentials"
            )

    @staticmethod
    def _decode_base64(raw: str, secret_id: str) -> str:
        try:
            return base64.b64decode("".join(raw.split()), validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            raise CredentialsSourceError(
                f"Secret {secret_id} is configured as base64-encoded but its "
                f"value is not valid base64 text"
            )

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
