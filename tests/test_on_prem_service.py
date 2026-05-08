from unittest import TestCase
from unittest.mock import Mock, patch

from apollo.egress.agent.config.config_manager import ConfigurationManager
from apollo.egress.agent.config.local_config import LocalConfig
from apollo.egress.agent.service.operations_runner import Operation

from hermes.agent.service.on_prem_service import OnPremService


class OnPremServiceTests(TestCase):
    def setUp(self):
        self._config_manager = ConfigurationManager(
            persistence=LocalConfig(prefix="MCD")
        )
        self._logging_utils = Mock()
        self._service = OnPremService(
            config_manager=self._config_manager, logging_utils=self._logging_utils
        )

    @patch("hermes.agent.service.on_prem_service.Agent.validate_tcp_open_connection")
    def test_network_open(self, open_connection_mock):
        self._service._execute_scheduled_operation(
            Operation(
                operation_id="1234",
                event={
                    "path": "/api/v1/test/network/open",
                    "operation": {
                        "host": "localhost",
                        "port": "8081",
                        "timeout": "10",
                        "trace_id": "1234",
                    },
                    "credentials": {},
                },
            )
        )
        open_connection_mock.assert_called_once_with(
            host="localhost", port_str="8081", trace_id="1234", timeout_str="10"
        )

    @patch("hermes.agent.service.on_prem_service.Agent.validate_http_connection")
    def test_network_http(self, http_connection_mock):
        self._service._execute_scheduled_operation(
            Operation(
                operation_id="5678",
                event={
                    "path": "/api/v1/test/network/http",
                    "operation": {
                        "url": "https://example.com",
                        "include_response": "true",
                        "timeout": "30",
                        "trace_id": "5678",
                    },
                    "credentials": {},
                },
            )
        )
        http_connection_mock.assert_called_once_with(
            url="https://example.com",
            include_response_str="true",
            timeout_str="30",
            trace_id="5678",
        )

    @patch("hermes.agent.service.on_prem_service.Agent.get_connection_manifests")
    def test_connection_manifests(self, get_manifests_mock):
        self._service._execute_scheduled_operation(
            Operation(
                operation_id="9012",
                event={
                    "path": "/api/v1/agent/custom-connectors/manifests",
                    "operation": {
                        "trace_id": "9012",
                    },
                },
            )
        )
        get_manifests_mock.assert_called_once_with("9012")

    @patch("hermes.agent.service.on_prem_service.Agent.get_supported_connector_types")
    def test_supported_connector_types(self, get_types_mock):
        self._service._execute_scheduled_operation(
            Operation(
                operation_id="3456",
                event={
                    "path": "/api/v1/agent/connectors/types",
                    "operation": {
                        "trace_id": "3456",
                    },
                },
            )
        )
        get_types_mock.assert_called_once_with("3456")

    @patch("hermes.agent.service.on_prem_service.setup_in_process_log_shipping")
    def test_in_process_logs_enabled_wires_logs_service(self, setup_mock):
        # Drives the activation branch of _build_logs_service: when
        # MCD_IN_PROCESS_LOGS_ENABLED=true, setup_in_process_log_shipping must
        # be called and its return value stored on the parent _logs_service.
        import logging

        sentinel_logs_service = Mock()
        setup_mock.return_value = sentinel_logs_service
        with patch.dict(
            "os.environ",
            {
                "MCD_IN_PROCESS_LOGS_ENABLED": "true",
                "MCD_IN_PROCESS_LOGS_LEVEL": "WARNING",
            },
        ):
            service = OnPremService(
                config_manager=self._config_manager,
                logging_utils=self._logging_utils,
            )
        setup_mock.assert_called_once_with(level=logging.WARNING)
        self.assertIs(service._logs_service, sentinel_logs_service)
