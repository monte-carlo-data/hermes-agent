"""In-process log shipping for the generic agent.

When the logs-collector daemonset is disabled (e.g. for non-root cluster
policies), this module captures the agent's structured log records into a
bounded in-memory buffer and exposes them via the BaseLogsService
interface so the existing "Logs sender" TimerService can periodically
POST them to the same /api/v1/agent/logs endpoint the daemonset uses.

Records are reshaped to fluentd's wire format ({timestamp, message,
instance_id}) so they're indistinguishable from daemonset-shipped records
on the backend side.
"""

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from apollo.egress.agent.service.logs_service import BaseLogsService

DEFAULT_BUFFER_SIZE = 10000
# Match the fluentd daemonset's default level filter (set in helm/values.yaml's
# logsCollector.logLevel = "INFO|WARN|WARNING|ERROR|CRITICAL").
DEFAULT_LEVEL = logging.INFO


class InProcessLogShippingHandler(logging.Handler):
    """logging.Handler that buffers reshaped records for in-process shipping."""

    def __init__(
        self,
        instance_id: Optional[str],
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        level: int = DEFAULT_LEVEL,
    ):
        super().__init__(level=level)
        # Default formatter renders "<message>\n<traceback>" when exc_info is set,
        # so logger.exception(...) and logger.error(..., exc_info=True) preserve
        # stack traces in the shipped payload.
        self.setFormatter(logging.Formatter("%(message)s"))
        self._instance_id = instance_id or ""
        self._buffer: Deque[Dict[str, Any]] = deque(maxlen=buffer_size)
        self._lock = threading.Lock()
        # Counter for records the deque silently evicted because the buffer was
        # full at emit() time. Surfaced as a synthetic warning at the next
        # drain() so the loss is visible backend-side.
        self._dropped_count = 0
        # If a downstream call inside emit() ever logs, it would re-enter this
        # handler and recurse. Drop re-entrant records silently.
        self._reentry = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._reentry, "in_emit", False):
            return
        self._reentry.in_emit = True
        try:
            shipped = {
                "timestamp": _format_timestamp(record.created),
                "message": self.format(record),
                "instance_id": self._instance_id,
            }
            with self._lock:
                if len(self._buffer) == self._buffer.maxlen:
                    self._dropped_count += 1
                self._buffer.append(shipped)
        except Exception:
            self.handleError(record)
        finally:
            self._reentry.in_emit = False

    def drain(self, _limit: int) -> List[Dict[str, Any]]:
        """Pop and return all buffered records.

        The `limit` parameter from BaseLogsService is intentionally ignored —
        our buffer is already bounded by `buffer_size`, so a full drain has a
        known upper bound (~buffer_size records per push). Honoring a smaller
        limit would systematically lag at sustained log rates and cause the
        deque to silently evict unsent records. If the buffer ever did
        overflow, the count of dropped records is surfaced as a synthetic
        warning at the head of the next drain.
        """
        with self._lock:
            records: List[Dict[str, Any]] = []
            if self._dropped_count > 0:
                records.append(
                    {
                        "timestamp": _format_timestamp(),
                        "message": (
                            f"In-process log buffer overflow: "
                            f"{self._dropped_count} oldest records dropped"
                        ),
                        "instance_id": self._instance_id,
                    }
                )
                self._dropped_count = 0
            records.extend(self._buffer)
            self._buffer.clear()
            return records


def _format_timestamp(epoch_seconds: Optional[float] = None) -> str:
    ts = (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        if epoch_seconds is not None
        else datetime.now(tz=timezone.utc)
    )
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class InProcessLogsService(BaseLogsService):
    """BaseLogsService implementation backed by an InProcessLogShippingHandler."""

    def __init__(self, handler: InProcessLogShippingHandler):
        self._handler = handler

    def get_logs(self, limit: int) -> List[Dict[str, Any]]:
        return self._handler.drain(limit)

    def close(self) -> None:
        """Detach the handler from the root logger and release resources."""
        logging.getLogger().removeHandler(self._handler)
        self._handler.close()
