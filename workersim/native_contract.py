"""Control-plane contract from GPUStack commit 458030e8, not fleet-level load shaping."""

WATCH_RESOURCES = (
    "inference-backends", "models", "model-instances", "benchmarks",
    "cache-service-instances", "model-files",
)
HEARTBEAT_INTERVAL = 30
STATUS_INTERVAL = 30
STATE_SYNC_INTERVAL = 3
WATCH_RECONNECT_SECONDS = 5
REGISTER_RETRY_SECONDS = 3
REGISTER_ATTEMPTS = 10
RUNTIME_METRICS_INTERVAL = 3
RUNTIME_METRICS_INITIAL_DELAY = 30
METRICS_CONFIG_TTL = 300
CACHE_PROVIDER_INTERVAL = 60
