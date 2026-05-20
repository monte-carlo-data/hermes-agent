import os
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
        # Disable in-process log shipping for the bulk of these tests so
        # construction doesn't attach a real log handler to the root logger.
        # The dedicated logs-wiring tests opt back in explicitly.
        with patch.dict("os.environ", {"MCD_IN_PROCESS_LOGS_ENABLED": "false"}):
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

    @patch("hermes.agent.service.on_prem_service.setup_in_process_log_shipping")
    def test_in_process_logs_default_is_enabled(self, setup_mock):
        # When MCD_IN_PROCESS_LOGS_ENABLED is unset, log shipping must still
        # activate — Docker users without an explicit override should get logs
        # out of the box. The previous "false" default left them silent.
        sentinel_logs_service = Mock()
        setup_mock.return_value = sentinel_logs_service
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("MCD_IN_PROCESS_LOGS_ENABLED", None)
            service = OnPremService(
                config_manager=self._config_manager,
                logging_utils=self._logging_utils,
            )
        setup_mock.assert_called_once()
        self.assertIs(service._logs_service, sentinel_logs_service)

    @patch("hermes.agent.service.on_prem_service.setup_in_process_log_shipping")
    def test_in_process_logs_explicitly_disabled(self, setup_mock):
        # The opt-out path: anyone who genuinely doesn't want log shipping can
        # still suppress it with MCD_IN_PROCESS_LOGS_ENABLED=false.
        with patch.dict("os.environ", {"MCD_IN_PROCESS_LOGS_ENABLED": "false"}):
            service = OnPremService(
                config_manager=self._config_manager,
                logging_utils=self._logging_utils,
            )
        setup_mock.assert_not_called()
        self.assertIsNone(service._logs_service)

    # ------------------------------------------------------------------
    # OAuth provider selection
    # ------------------------------------------------------------------

    @patch("hermes.agent.service.on_prem_service.OAuthLoginTokenProvider")
    @patch("hermes.agent.service.on_prem_service._MCD_OAUTH_TOKEN_ENDPOINT", None)
    @patch(
        "hermes.agent.service.on_prem_service.OnPremService._read_oauth_credentials",
        return_value=("test-id", "test-secret"),
    )
    @patch(
        "hermes.agent.service.on_prem_service._MCD_OAUTH_FILE_PATH",
        "/etc/secrets/mcd-oauth/credentials.json",
    )
    def test_oauth_provider_selected_when_file_path_set(
        self, mock_read, mock_oauth_cls
    ):
        mock_oauth_cls.return_value = Mock()
        with patch.dict(
            "os.environ",
            {"MCD_IN_PROCESS_LOGS_ENABLED": "false"},
        ):
            service = OnPremService(
                config_manager=self._config_manager,
                logging_utils=self._logging_utils,
            )
        mock_read.assert_called_once_with("/etc/secrets/mcd-oauth/credentials.json")
        mock_oauth_cls.assert_called_once_with(
            client_id="test-id",
            client_secret="test-secret",
            backend_service_url="https://artemis.getmontecarlo.com:443",
            token_endpoint=None,
        )

    @patch("hermes.agent.service.on_prem_service.OAuthLoginTokenProvider")
    @patch("hermes.agent.service.on_prem_service._MCD_TOKEN_FILE_PATH", "/some/file")
    @patch("hermes.agent.service.on_prem_service._MCD_OAUTH_TOKEN_ENDPOINT", None)
    @patch(
        "hermes.agent.service.on_prem_service.OnPremService._read_oauth_credentials",
        return_value=("test-id", "test-secret"),
    )
    @patch(
        "hermes.agent.service.on_prem_service._MCD_OAUTH_FILE_PATH",
        "/etc/secrets/mcd-oauth/credentials.json",
    )
    def test_oauth_takes_precedence_over_file_token(self, mock_read, mock_oauth_cls):
        mock_oauth_cls.return_value = Mock()
        with patch.dict(
            "os.environ",
            {"MCD_IN_PROCESS_LOGS_ENABLED": "false"},
        ):
            service = OnPremService(
                config_manager=self._config_manager,
                logging_utils=self._logging_utils,
            )
        mock_oauth_cls.assert_called_once()
        self.assertIs(service._login_token_provider, mock_oauth_cls.return_value)

    @patch("hermes.agent.service.on_prem_service.OAuthLoginTokenProvider")
    @patch(
        "hermes.agent.service.on_prem_service._MCD_OAUTH_TOKEN_ENDPOINT",
        "https://custom.example.com/oauth2/token",
    )
    @patch(
        "hermes.agent.service.on_prem_service.OnPremService._read_oauth_credentials",
        return_value=("test-id", "test-secret"),
    )
    @patch(
        "hermes.agent.service.on_prem_service._MCD_OAUTH_FILE_PATH",
        "/etc/secrets/mcd-oauth/credentials.json",
    )
    def test_oauth_token_endpoint_override_passed_through(
        self, mock_read, mock_oauth_cls
    ):
        mock_oauth_cls.return_value = Mock()
        with patch.dict(
            "os.environ",
            {"MCD_IN_PROCESS_LOGS_ENABLED": "false"},
        ):
            service = OnPremService(
                config_manager=self._config_manager,
                logging_utils=self._logging_utils,
            )
        mock_oauth_cls.assert_called_once_with(
            client_id="test-id",
            client_secret="test-secret",
            backend_service_url="https://artemis.getmontecarlo.com:443",
            token_endpoint="https://custom.example.com/oauth2/token",
        )

    # ------------------------------------------------------------------
    # OAuth credential file reading
    # ------------------------------------------------------------------

    def test_oauth_file_missing_client_id_raises(self):
        import json
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"client_secret": "test-secret"}, f)
            path = f.name
        try:
            with self.assertRaises(ValueError) as ctx:
                OnPremService._read_oauth_credentials(path)
            self.assertIn("client_id", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_oauth_file_missing_client_secret_raises(self):
        import json
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"client_id": "test-id"}, f)
            path = f.name
        try:
            with self.assertRaises(ValueError) as ctx:
                OnPremService._read_oauth_credentials(path)
            self.assertIn("client_secret", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_oauth_file_not_found_raises(self):
        with self.assertRaises(ValueError) as ctx:
            OnPremService._read_oauth_credentials("/nonexistent/path/creds.json")
        self.assertIn("not found", str(ctx.exception))

    def test_oauth_file_invalid_json_raises(self):
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not json")
            path = f.name
        try:
            with self.assertRaises(ValueError) as ctx:
                OnPremService._read_oauth_credentials(path)
            self.assertIn("not valid JSON", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_oauth_file_reads_credentials(self):
        import json
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"client_id": "my-id", "client_secret": "my-secret"}, f)
            path = f.name
        try:
            client_id, client_secret = OnPremService._read_oauth_credentials(path)
            self.assertEqual(client_id, "my-id")
            self.assertEqual(client_secret, "my-secret")
        finally:
            os.unlink(path)

    # ------------------------------------------------------------------
    # Fallback provider selection
    # ------------------------------------------------------------------

    @patch("hermes.agent.service.on_prem_service._MCD_OAUTH_FILE_PATH", None)
    @patch(
        "hermes.agent.service.on_prem_service._MCD_TOKEN_FILE_PATH", "/some/token/file"
    )
    def test_file_provider_when_no_oauth(self):
        with patch.dict(
            "os.environ",
            {"MCD_IN_PROCESS_LOGS_ENABLED": "false"},
        ):
            service = OnPremService(
                config_manager=self._config_manager,
                logging_utils=self._logging_utils,
            )
        from apollo.egress.agent.service.file_login_token_provider import (
            FileLoginTokenProvider,
        )

        self.assertIsInstance(service._login_token_provider, FileLoginTokenProvider)

    @patch("hermes.agent.service.on_prem_service._MCD_TOKEN_FILE_PATH", None)
    @patch("hermes.agent.service.on_prem_service._MCD_OAUTH_FILE_PATH", None)
    def test_local_provider_when_no_oauth_no_file(self):
        with patch.dict(
            "os.environ",
            {"MCD_IN_PROCESS_LOGS_ENABLED": "false"},
        ):
            service = OnPremService(
                config_manager=self._config_manager,
                logging_utils=self._logging_utils,
            )
        from apollo.egress.agent.service.login_token_provider import (
            LocalLoginTokenProvider,
        )

        self.assertIsInstance(service._login_token_provider, LocalLoginTokenProvider)
