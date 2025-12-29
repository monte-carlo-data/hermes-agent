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
