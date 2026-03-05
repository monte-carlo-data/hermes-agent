import logging
import os
from typing import Dict, Any, Callable, Tuple

from apollo.agent.agent import Agent
from apollo.agent.logging_utils import LoggingUtils
from apollo.credentials.factory import CredentialsFactory
from apollo.egress.agent.config.config_manager import ConfigurationManager
from apollo.egress.agent.service.base_egress_service import (
    BaseEgressAgentService,
    OperationMapping,
    OperationMatchingType,
)
from apollo.egress.agent.service.file_login_token_provider import FileLoginTokenProvider
from apollo.egress.agent.service.login_token_provider import LocalLoginTokenProvider
from apollo.egress.agent.service.storage_service import EmptyStorageService

from hermes.agent.service.metrics_service import MetricsService
from hermes.agent.settings import BUILD_NUMBER, VERSION

_BACKEND_SERVICE_URL = os.getenv(
    "BACKEND_SERVICE_URL",
    "https://artemis.getmontecarlo.com:443",
)
_MCD_TOKEN_FILE_PATH = os.getenv("MCD_TOKEN_FILE_PATH")
_NETWORK_PATH_PREFIX = "/api/v1/test/network/"

logger = logging.getLogger(__name__)


class OnPremService(BaseEgressAgentService):
    def __init__(
        self,
        config_manager: ConfigurationManager,
        logging_utils: LoggingUtils,
    ):
        if _MCD_TOKEN_FILE_PATH:
            logger.info(f"Getting MCD token from file: {_MCD_TOKEN_FILE_PATH}")
            login_token_provider = FileLoginTokenProvider(
                file_path=_MCD_TOKEN_FILE_PATH,
            )
        else:
            logger.info("Getting MCD token from env vars")
            login_token_provider = LocalLoginTokenProvider()
        super().__init__(
            backend_service_url=_BACKEND_SERVICE_URL,
            platform="Generic",
            service_name="Generic Agent",
            config_manager=config_manager,
            skip_logs=True,
            logs_service=None,
            storage_service=EmptyStorageService(),
            metrics_service=MetricsService(),
            login_token_provider=login_token_provider,
        )
        self._agent = Agent(logging_utils)

        self._operations_mapping.append(
            OperationMapping(
                path=_NETWORK_PATH_PREFIX,
                matching_type=OperationMatchingType.STARTS_WITH,
                method=self._execute_network_operation,
                schedule=True,
            )
        )

    def _internal_execute_agent_operation(
        self, event: Dict[str, Any]
    ) -> Dict[str, Any]:
        credentials = self._extract_credentials_in_request(event.get("credentials", {}))
        operation = event.get("operation")
        path = event.get("path")
        if not path or not path.startswith("/api/v1/agent/execute/"):
            raise ValueError(f"Invalid path: {path}")
        connection_type, operation_name = path.split("/")[5:7]

        return self._agent.execute_operation(
            connection_type, operation_name, operation, credentials
        ).result

    @staticmethod
    def _extract_credentials_in_request(credentials: Dict) -> Dict:
        credential_service = CredentialsFactory.get_credentials_service(credentials)
        return credential_service.get_credentials(credentials)

    def _execute_network_operation(
        self,
        operation_id: str,
        event: Dict[str, Any],
    ):
        operation = event.get("operation")
        path = event.get("path")

        try:
            if not path or not path.startswith(_NETWORK_PATH_PREFIX):
                raise ValueError(f"Invalid path: {path}")
            if not isinstance(operation, dict):
                raise ValueError(f"Invalid operation: {operation}")

            network_command = path.removeprefix(_NETWORK_PATH_PREFIX)
            if network_command == "outbound_ip_address":
                response = self._agent.get_outbound_ip_address()
                self._schedule_push_results(operation_id, response.result)
                return

            if network_command == "http":
                response, _ = self._execute_http_connection_test(operation)
            else:
                include_timeout = True
                if network_command == "open":
                    method = self._agent.validate_tcp_open_connection
                elif network_command == "telnet":
                    method = self._agent.validate_telnet_connection
                elif network_command == "dns":
                    method = self._agent.perform_dns_lookup
                    include_timeout = False
                else:
                    raise ValueError(f"Invalid path: {path}")
                response, _ = self._execute_network_validation(
                    method, operation, include_timeout
                )
            self._schedule_push_results(operation_id, response)
        except Exception as ex:
            self._schedule_push_results(operation_id, self._result_for_exception(ex))

    @staticmethod
    def _execute_network_validation(
        method: Callable, operation: Dict[str, Any], include_timeout: bool
    ) -> Tuple[Dict, int]:
        args = dict(
            host=operation.get("host"),
            port_str=operation.get("port"),
            trace_id=operation.get("trace_id"),
        )
        if include_timeout:
            args["timeout_str"] = operation.get("timeout")
        response = method(**args)
        return response.result, response.status_code

    def _execute_http_connection_test(
        self,
        operation: Dict[str, Any],
    ) -> Tuple[Dict, int]:
        response = self._agent.validate_http_connection(
            url=operation.get("url"),
            include_response_str=operation.get("include_response"),
            timeout_str=operation.get("timeout"),
            trace_id=operation.get("trace_id"),
        )
        return response.result, response.status_code

    def _get_version(self) -> str:
        return VERSION

    def _get_build_number(self) -> str:
        return BUILD_NUMBER
