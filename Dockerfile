# When bumping this tag, also bump the matching apollo Python pin in requirements.in
# so the runtime apt deps and the venv-installed apollo source agree on the version.
FROM montecarlodata/agent:1.14.3-system-base AS base

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
# Bundled test suites ship public test keys and fake credentials that secret
# scanners flag. Nothing runs them; removed in the same RUN so no layer keeps them.
RUN . $VENV_DIR/bin/activate && pip install --no-cache-dir -r requirements.txt \
    && rm -rf $VENV_DIR/lib/python*/site-packages/Crypto/SelfTest \
              $VENV_DIR/lib/python*/site-packages/pandas/tests \
              $VENV_DIR/lib/python*/site-packages/impala/tests \
    # teradatasql bundles its Go driver for every OS/CPU (Windows, macOS, AIX, POWER,
    # 32-bit, ARM) but loads only the one for the running platform: teradatasql.so,
    # or teradatasql.fips.so on FIPS hosts (see teradatasql/__init__.py). Scanners
    # flag the vendored Go modules in every copy, so keep only the native ones.
    && TD=$(echo $VENV_DIR/lib/python*/site-packages/teradatasql) \
    && rm -f $TD/*.dll $TD/*.dylib $TD/teradatasql.aix.so $TD/teradatasql.power.so $TD/teradatasql.x86.so \
    && if [ "$(uname -m)" = x86_64 ]; then \
         rm -f $TD/teradatasql.arm.so $TD/teradatasql.arm.fips.so && test -f $TD/teradatasql.so; fi
# VULN-423: pip and setuptools must be upgraded post-install. Exact-pinned for
# reproducible builds and to bound supply-chain exposure on the upgrade step
# itself; bump explicitly when picking up new patches. pip's vendored SBOM lists
# pip-internal deps (an old setuptools) that scanners report as image CVEs; it's
# removed in the same RUN so no layer keeps it.
RUN . $VENV_DIR/bin/activate && pip install pip==26.3 setuptools==84.2.1 \
    && rm -f $VENV_DIR/lib/python*/site-packages/pip/_vendor/bom.cdx.json

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

# No --bind here: gunicorn.conf.py binds 127.0.0.1:$PORT by default, or
# 0.0.0.0:$PORT when MCD_HTTP_LISTEN_ALL_INTERFACES=true (SEC-4041).
CMD ["/bin/sh", "-c", ". $VENV_DIR/bin/activate && exec gunicorn --config gunicorn.conf.py hermes.agent.main:app"]
