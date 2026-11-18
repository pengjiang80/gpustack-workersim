FROM python:3.12-slim
WORKDIR /app
COPY LICENSE ./LICENSE
COPY pyproject.toml ./
COPY requirements.lock ./
COPY workersim ./workersim
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home workersim \
    && mkdir -p /data /config && chown workersim:workersim /data
USER workersim
STOPSIGNAL SIGTERM
ENTRYPOINT ["workersim"]
CMD ["--config", "/config/fleet.yaml", "run"]

