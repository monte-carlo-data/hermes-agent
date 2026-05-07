import logging
from unittest import TestCase

from hermes.agent.service.in_process_logs_service import (
    InProcessLogShippingHandler,
    InProcessLogsService,
)


class InProcessLogShippingHandlerTests(TestCase):
    def _make_record(
        self, level: int = logging.WARNING, msg: str = "test"
    ) -> logging.LogRecord:
        return logging.LogRecord(
            name="test",
            level=level,
            pathname=__file__,
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )

    def test_emit_buffers_in_fluentd_shape(self):
        handler = InProcessLogShippingHandler(instance_id="abc-123")
        handler.emit(self._make_record(msg="hello world"))
        records = handler.drain(_limit=10)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(set(record.keys()), {"timestamp", "message", "instance_id"})
        self.assertEqual(record["message"], "hello world")
        self.assertEqual(record["instance_id"], "abc-123")
        self.assertTrue(record["timestamp"].endswith("Z"))

    def test_drain_returns_all_records_oldest_first_and_clears(self):
        # The `limit` parameter is intentionally ignored — drain returns
        # everything currently buffered.
        handler = InProcessLogShippingHandler(instance_id="i")
        handler.emit(self._make_record(msg="first"))
        handler.emit(self._make_record(msg="second"))
        handler.emit(self._make_record(msg="third"))
        records = handler.drain(_limit=2)  # limit ignored
        self.assertEqual([r["message"] for r in records], ["first", "second", "third"])
        # Buffer is cleared after drain.
        self.assertEqual(handler.drain(_limit=10), [])

    def test_buffer_overflow_drops_oldest_and_surfaces_warning(self):
        handler = InProcessLogShippingHandler(instance_id="i", buffer_size=2)
        handler.emit(self._make_record(msg="a"))
        handler.emit(self._make_record(msg="b"))
        handler.emit(self._make_record(msg="c"))  # evicts "a"
        handler.emit(self._make_record(msg="d"))  # evicts "b"
        records = handler.drain(_limit=10)
        # Synthetic warning record prepended, then the surviving records.
        self.assertEqual(len(records), 3)
        self.assertIn("buffer overflow", records[0]["message"])
        self.assertIn("2 oldest records dropped", records[0]["message"])
        self.assertEqual(records[0]["instance_id"], "i")
        self.assertEqual([r["message"] for r in records[1:]], ["c", "d"])

        # Counter resets after drain — a subsequent drain with no new evictions
        # must not include another synthetic warning.
        handler.emit(self._make_record(msg="e"))
        records = handler.drain(_limit=10)
        self.assertEqual([r["message"] for r in records], ["e"])

    def test_below_handler_level_is_filtered(self):
        # Python's logging framework checks Handler.level inside callHandlers
        # before invoking emit(). We mirror that path via Logger to make sure
        # our level filter actually gates records.
        handler = InProcessLogShippingHandler(instance_id="i", level=logging.WARNING)
        logger = logging.getLogger("hermes.test.in_process_logs_filter")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            logger.info("info message — should be dropped")
            logger.warning("warning message — should be kept")
        finally:
            logger.removeHandler(handler)
        records = handler.drain(_limit=10)
        self.assertEqual(
            [r["message"] for r in records], ["warning message — should be kept"]
        )

    def test_args_substitution_applied_to_message(self):
        handler = InProcessLogShippingHandler(instance_id="i")
        record = logging.LogRecord(
            name="test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=0,
            msg="user=%s status=%d",
            args=("alice", 200),
            exc_info=None,
        )
        handler.emit(record)
        records = handler.drain(_limit=10)
        self.assertEqual(records[0]["message"], "user=alice status=200")

    def test_exception_traceback_is_included(self):
        # logger.exception(...) and logger.error(..., exc_info=True) must
        # ship the formatted traceback alongside the message — Formatter.format
        # appends it when record.exc_info is set.
        handler = InProcessLogShippingHandler(instance_id="i")
        logger = logging.getLogger("hermes.test.in_process_logs_exc")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            try:
                raise ValueError("boom")
            except ValueError:
                logger.exception("caught it")
        finally:
            logger.removeHandler(handler)
        records = handler.drain(_limit=10)
        self.assertEqual(len(records), 1)
        msg = records[0]["message"]
        self.assertIn("caught it", msg)
        self.assertIn("ValueError", msg)
        self.assertIn("boom", msg)
        self.assertIn("Traceback", msg)


class InProcessLogsServiceTests(TestCase):
    def test_get_logs_drains_handler_buffer(self):
        handler = InProcessLogShippingHandler(instance_id="i")
        service = InProcessLogsService(handler)

        record = logging.LogRecord(
            name="test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=0,
            msg="payload",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        result = service.get_logs(limit=5)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["message"], "payload")
        # Buffer is drained, so a follow-up call returns nothing.
        self.assertEqual(service.get_logs(limit=5), [])
