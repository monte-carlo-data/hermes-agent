FROM montecarlodata/agent:latest-generic AS base

# Allow statements and log messages to immediately appear in the logs
ENV PYTHONUNBUFFERED=True
# JSON log format for structured logging (parsed by fluentd sidecar)
# Override with MCD_LOG_FORMAT=text for local development
ENV MCD_LOG_FORMAT=json

ENV APP_HOME=/app
ENV VENV_DIR=.venv
WORKDIR $APP_HOME

# delete the source code from the base image, we don't need it
RUN rm -rf ./apollo

COPY requirements.txt ./
RUN . $VENV_DIR/bin/activate && pip install --no-cache-dir --force-reinstall -r requirements.txt

# copy sources in the last step so we don't install python libraries due to a change in source code
COPY gunicorn.conf.py ./
COPY hermes/ ./hermes

ARG code_version="local"
ARG build_number="0"
RUN echo $code_version,$build_number > ./hermes/agent/version

FROM base AS tests

COPY requirements-dev.txt ./
RUN . $VENV_DIR/bin/activate && \
    pip install --no-cache-dir -r requirements-dev.txt

COPY tests ./tests
ARG CACHEBUST=1
RUN . $VENV_DIR/bin/activate && \
    PYTHONPATH=. pytest tests

FROM base AS hermes-generic

WORKDIR $APP_HOME

# Run as non-root for clusters that enforce runAsNonRoot. UID/GID 1000 is
# referenced by helm/values.yaml (podSecurityContext) — keep them in sync.
RUN groupadd --gid 1000 mcdagent \
    && useradd --uid 1000 --gid mcdagent --no-create-home --home-dir $APP_HOME --shell /usr/sbin/nologin mcdagent \
    && chown -R mcdagent:mcdagent $APP_HOME

USER mcdagent

CMD ["/bin/sh", "-c", ". $VENV_DIR/bin/activate && exec gunicorn --config gunicorn.conf.py --bind :$PORT hermes.agent.main:app"]
