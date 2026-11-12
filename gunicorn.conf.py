"""Gunicorn configuration for hermes-agent.

The post_worker_init hook registers signal handlers after gunicorn has
finished initializing the worker (including setting up its own signal
handlers). Without this, gunicorn's init_signals() overrides the
application's handlers, and graceful shutdown logic (notify orchestrator,
stop threads) won't run on workers that receive SIGQUIT (gunicorn's
default graceful-shutdown signal).

The agent is egress-only; its HTTP listener serves only health and
troubleshooting routes (/api/v1/test/*). By default it binds to loopback, so
nothing outside the pod or container can reach it: in-pod probes and
`kubectl exec ... curl localhost:$PORT` / `docker compose exec` still work.
Set MCD_HTTP_LISTEN_ALL_INTERFACES=true to bind 0.0.0.0 instead, e.g. for
network (httpGet) health probes (SEC-4041).
"""

import os

LISTEN_ALL_INTERFACES_ENV = "MCD_HTTP_LISTEN_ALL_INTERFACES"


def listen_all_interfaces() -> bool:
    return os.getenv(LISTEN_ALL_INTERFACES_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def bind_address() -> str:
    host = "0.0.0.0" if listen_all_interfaces() else "127.0.0.1"
    return f"{host}:{os.getenv('PORT') or '8080'}"


bind = [bind_address()]


def post_worker_init(worker: object) -> None:
    from hermes.agent.main import service

    service.register_signal_handlers()
