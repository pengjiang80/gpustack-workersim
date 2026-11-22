FROM python:3.12-slim
WORKDIR /app
COPY LICENSE ./LICENSE
COPY pyproject.toml ./
COPY requirements.lock ./
COPY workersim ./workersim
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home workersim \
    && mkdir -p /data /config /var/lib/gpustack/workersim     && chown workersim:workersim /data /var/lib/gpustack/workersim
# Starts as root only to reserve host ports (with --privileged --network=host), then
# drops to UID 10001 before contacting the Server. Without privileges nothing changes.
STOPSIGNAL SIGTERM
ENTRYPOINT ["workersim"]
CMD ["--config", "/config/fleet.yaml", "run"]

