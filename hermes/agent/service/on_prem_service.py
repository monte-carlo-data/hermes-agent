import logging
import os
from typing import Dict, Any, Callable, Optional, Tuple

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
from apollo.egress.agent.service.in_process_logs_service import (
    setup_in_process_log_shipping,
)
from apollo.egress.agent.service.logs_service import BaseLogsService
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
_CUSTOM_CONNECTORS_MANIFESTS_PATH = "/api/v1/agent/custom-connectors/manifests"
_CONNECTORS_TYPES_PATH = "/api/v1/agent/connectors/types"
_VALIDATE_SELF_HOSTED_CREDENTIALS_PATH_PREFIX = (
    "/api/v1/self-hosted-credentials/validate/"
)

logger = logging.getLogger(__name__)


class OnPremService(BaseEgressAgentService):
    def __init__(
        self,
        config_manager: ConfigurationManager,
        logging_utils: LoggingUtils,
        instance_id: Optional[str] = None,
    ):
        logger.info(f"Using backend service URL: {_BACKEND_SERVICE_URL}")
        if _MCD_TOKEN_FILE_PATH:
            logger.info(f"Getting MCD token from file: {_MCD_TOKEN_FILE_PATH}")
            login_token_provider = FileLoginTokenProvider(
                file_path=_MCD_TOKEN_FILE_PATH,
            )
        else:
            logger.info("Getting MCD token from env vars")
            login_token_provider = LocalLoginTokenProvider()

        logs_service = self._build_logs_service()
        super().__init__(
            backend_service_url=_BACKEND_SERVICE_URL,
            platform="Generic",
            service_name="Generic Agent",
            config_manager=config_manager,
            skip_logs=logs_service is None,
            logs_service=logs_service,
            storage_service=EmptyStorageService(),
            metrics_service=MetricsService(),
            login_token_provider=login_token_provider,
            instance_id=instance_id,
        )
        self._agent = Agent(logging_utils)

        self._operations_mapping.append(
            OperationMapping(
                path=_NETWORK_PATH_PREFIX,
                matching_type=OperationMatchingType.STARTS_WITH,
                method=self._execute_network_operation,
            )
        )
        self._operations_mapping.append(
            OperationMapping(
                path=_CUSTOM_CONNECTORS_MANIFESTS_PATH,
                matching_type=OperationMatchingType.EQUALS,
                method=self._execute_connection_manifests,
            )
        )
        self._operations_mapping.append(
            OperationMapping(
                path=_CONNECTORS_TYPES_PATH,
                matching_type=OperationMatchingType.EQUALS,
                method=self._execute_supported_connector_types,
            )
        )
        self._operations_mapping.append(
            OperationMapping(
                path=_VALIDATE_SELF_HOSTED_CREDENTIALS_PATH_PREFIX,
                matching_type=OperationMatchingType.STARTS_WITH,
                method=self._execute_validate_self_hosted_credentials,
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

    def _execute_connection_manifests(
        self,
        operation_id: str,
        event: Dict[str, Any],
    ):
        try:
            operation = event.get("operation")
            trace_id = (
                operation.get("trace_id") if isinstance(operation, dict) else None
            )
            response = self._agent.get_connection_manifests(trace_id)
            self._schedule_push_results(operation_id, response.result)
        except Exception as ex:
            self._schedule_push_results(operation_id, self._result_for_exception(ex))

    def _execute_supported_connector_types(
        self,
        operation_id: str,
        event: Dict[str, Any],
    ):
        try:
            operation = event.get("operation")
            trace_id = (
                operation.get("trace_id") if isinstance(operation, dict) else None
            )
            response = self._agent.get_supported_connector_types(trace_id)
            self._schedule_push_results(operation_id, response.result)
        except Exception as ex:
            self._schedule_push_results(operation_id, self._result_for_exception(ex))

    def _execute_validate_self_hosted_credentials(
        self,
        operation_id: str,
        event: Dict[str, Any],
    ):
        """Egress counterpart to the Flask /api/v1/self-hosted-credentials/validate/
        route in apollo-agent.

        Thin wrapper: parse the connection_type out of the path and hand off
        the raw credentials envelope to ``Agent.validate_self_hosted_credentials``,
        which owns the self-hosted guard, the secret-store fetch, and the
        schema validation. Keeping that pipeline in the agent means the
        Flask and egress entry points share a single implementation.
        """
        try:
            path = event.get("path")
            if not path or not path.startswith(
                _VALIDATE_SELF_HOSTED_CREDENTIALS_PATH_PREFIX
            ):
                raise ValueError(f"Invalid path: {path}")
            connection_type = path.removeprefix(
                _VALIDATE_SELF_HOSTED_CREDENTIALS_PATH_PREFIX
            )
            if not connection_type:
                raise ValueError(f"Missing connection_type in path: {path}")

            operation = event.get("operation")
            trace_id = (
                operation.get("trace_id") if isinstance(operation, dict) else None
            )
            response = self._agent.validate_self_hosted_credentials(
                connection_type=connection_type,
                credentials=event.get("credentials"),
                trace_id=trace_id,
            )
            self._schedule_push_results(operation_id, response.result)
        except Exception as ex:
            self._schedule_push_results(operation_id, self._result_for_exception(ex))

    @staticmethod
    def _build_logs_service() -> Optional[BaseLogsService]:
        # Default ON: Docker users get log shipping out of the box, matching
        # the helm chart's `logShipping: in-process` default. Helm sets this
        # env var explicitly per the chart value, so this default only affects
        # non-helm deployments. The level is sourced from
        # MCD_IN_PROCESS_LOGS_LEVEL (default INFO, allowlist gated against
        # DEBUG in the helm validator). Reads are inlined here rather than at
        # module import so tests can patch the environment.
        if os.getenv("MCD_IN_PROCESS_LOGS_ENABLED", "true").lower() != "true":
            return None
        level_name = os.getenv("MCD_IN_PROCESS_LOGS_LEVEL", "INFO").upper()
        level = logging.getLevelName(level_name)
        if not isinstance(level, int):
            level = logging.INFO
        return setup_in_process_log_shipping(level=level)

    def _get_version(self) -> str:
        return VERSION

    def _get_build_number(self) -> str:
        return BUILD_NUMBER
