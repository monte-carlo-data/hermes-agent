import json
import logging
import os
import uuid

from flask import Flask
from flask import make_response
from flask import request

from apollo.agent.logging_utils import LoggingUtils
from apollo.egress.agent.config.config_manager import ConfigurationManager
from apollo.egress.agent.config.local_config import LocalConfig
from apollo.egress.agent.utils.utils import enable_tcp_keep_alive, init_logging

instance_id = str(uuid.uuid4())
init_logging(instance_id=instance_id)
logger = logging.getLogger(__name__)
pod_name = os.getenv("K8S_POD_NAME")
if pod_name:
    logger.info(f"Agent running in Kubernetes, pod={pod_name}")

from hermes.agent.service.on_prem_service import OnPremService

SERVICE_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVICE_PORT = os.getenv("SERVER_PORT") or "8081"

"""
This is the main entry point for the Agent service, it starts a Flask application
and the `OnPremService` that will handle the communication with the MC backend.
It defines a few HTTP endpoints that provides information about the service and its health.
"""

app = Flask(__name__)
logging_utils = LoggingUtils()
service = OnPremService(
    config_manager=ConfigurationManager(persistence=LocalConfig(prefix="MCD")),
    logging_utils=logging_utils,
    instance_id=instance_id,
)


@app.get("/api/v1/test/healthcheck")
def health_check():
    """
    Used for container liveness and readiness probes.

    Example:
        kubectl exec deploy/mcd-agent-deployment -n mcd-agent -- curl -s http://localhost:8080/api/v1/test/healthcheck
    """
    return "OK"


@app.get("/api/v1/test/health")
def health():
    """
    Intended to be used for troubleshooting.

    Example:
        kubectl exec deploy/mcd-agent-deployment -n mcd-agent -- curl -s http://localhost:8080/api/v1/test/health
    """
    health_response = service.health_information(trace_id=request.args.get("trace_id"))
    response = make_response(health_response)
    response.headers["Content-type"] = "application/json"
    return response


@app.post("/api/v1/test/reachability")
def run_reachability_test():
    """
    Checks connectivity to the Monte Carlo backend.

    Example:
        kubectl exec deploy/mcd-agent-deployment -n mcd-agent -- curl -s -X POST http://localhost:8080/api/v1/test/reachability
    """
    reachability_response = service.run_reachability_test()
    output_rows = [[0, json.dumps(reachability_response)]]
    response = make_response({"data": output_rows})
    response.headers["Content-type"] = "application/json"
    return response


enable_tcp_keep_alive()
service.start()

if __name__ == "__main__":
    # only used for local development, when gunicorn is not used
    service.register_signal_handlers()
    app.run(host=SERVICE_HOST, port=int(SERVICE_PORT))
