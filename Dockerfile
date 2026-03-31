FROM montecarlodata/agent:latest-generic AS base

# Web server env var configuration
ENV GUNICORN_WORKERS=1
ENV GUNICORN_THREADS=1
ENV GUNICORN_TIMEOUT=0

# Allow statements and log messages to immediately appear in the logs
ENV PYTHONUNBUFFERED=True

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

CMD ["/bin/sh", "-c", ". $VENV_DIR/bin/activate && exec gunicorn --config gunicorn.conf.py --bind :$PORT --workers $GUNICORN_WORKERS --threads $GUNICORN_THREADS --timeout $GUNICORN_TIMEOUT hermes.agent.main:app"]