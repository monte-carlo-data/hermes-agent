# TEMPORARY: pinned to a pre-release dev build of the new `system-base` tag.
# Swap to montecarlodata/agent:<version>-system-base once apollo-agent#282
# merges and a prod release cuts the corresponding tag.
FROM montecarlodata/pre-release-agent:1.5.4rc2643-system-base AS base

# Allow statements and log messages to immediately appear in the logs
ENV PYTHONUNBUFFERED=True
# JSON log format for structured logging (parsed by fluentd sidecar)
# Override with MCD_LOG_FORMAT=text for local development
ENV MCD_LOG_FORMAT=json

ENV APP_HOME=/app
ENV VENV_DIR=.venv
WORKDIR $APP_HOME

# Create the non-root user up front and own the app dir, so every file created
# below (venv, pip-installed packages, copied source) is owned by mcdagent
# from the start. This avoids a final `chown -R` that would otherwise duplicate
# the entire venv into a new layer (~1 GB) just to flip ownership metadata.
# UID/GID 1000 is referenced by helm/values.yaml (podSecurityContext).
RUN groupadd --gid 1000 mcdagent \
    && useradd --uid 1000 --gid mcdagent --no-create-home --home-dir $APP_HOME --shell /usr/sbin/nologin mcdagent \
    && chown mcdagent:mcdagent $APP_HOME

USER mcdagent

RUN python -m venv $VENV_DIR

COPY --chown=mcdagent:mcdagent requirements.txt ./
RUN . $VENV_DIR/bin/activate && pip install --no-cache-dir -r requirements.txt
# VULN-423: pip and setuptools must be upgraded post-install
RUN . $VENV_DIR/bin/activate && pip install -U pip setuptools

# copy sources in the last step so we don't install python libraries due to a change in source code
COPY --chown=mcdagent:mcdagent gunicorn.conf.py ./
COPY --chown=mcdagent:mcdagent hermes/ ./hermes

ARG code_version="local"
ARG build_number="0"
RUN echo $code_version,$build_number > ./hermes/agent/version

FROM base AS tests

COPY --chown=mcdagent:mcdagent requirements-dev.txt ./
RUN . $VENV_DIR/bin/activate && \
    pip install --no-cache-dir -r requirements-dev.txt

COPY --chown=mcdagent:mcdagent tests ./tests
ARG CACHEBUST=1
RUN . $VENV_DIR/bin/activate && \
    PYTHONPATH=. pytest tests

FROM base AS hermes-generic

CMD ["/bin/sh", "-c", ". $VENV_DIR/bin/activate && exec gunicorn --config gunicorn.conf.py --bind :$PORT hermes.agent.main:app"]
