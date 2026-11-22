# GPUStack WorkerSim

[![CI](https://github.com/pengjiang80/gpustack-workersim/actions/workflows/ci.yml/badge.svg)](https://github.com/pengjiang80/gpustack-workersim/actions/workflows/ci.yml)
[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-gpustack%2Fworkersim-2496ED?logo=docker)](https://hub.docker.com/r/gpustack/workersim)

**Add hundreds of simulated GPU workers to a real GPUStack cluster, on a CPU-only machine.**

WorkerSim registers virtual Workers with an unmodified GPUStack Server. They appear in the
UI with GPUs or NPUs, receive real scheduling decisions, "run" models, and report metrics,
just like real Workers, so you can test scheduling, deployments and Server performance
without GPU hardware.

[中文说明](README.zh-CN.md) · [Releases](https://github.com/pengjiang80/gpustack-workersim/releases)

## Quick start

You need a GPUStack **2.3.1** Server and a Linux amd64 host with Docker.

**1.** In GPUStack, open **Clusters → Add Worker** and copy the command. It looks like:

```bash
sudo docker run -d --name gpustack-worker \
  -e "GPUSTACK_RUNTIME_DEPLOY_MIRRORED_NAME=gpustack-worker" \
  -e "GPUSTACK_TOKEN=<cluster-token>" \
  --restart=unless-stopped --privileged --network=host \
  --volume /var/run/docker.sock:/var/run/docker.sock \
  --volume gpustack-data:/var/lib/gpustack \
  --runtime nvidia \
  gpustack/gpustack:v2.3.1 \
  --server-url http://<server> --worker-ip <host-ip>
```

**2.** Make three changes:

- replace the image with `gpustack/workersim:0.1.3`;
- delete `--runtime nvidia` (and `--device /dev/infiniband` if present);
- add how many Workers you want and their hardware at the end.

```bash
sudo docker run -d --name workersim \
  -e "GPUSTACK_TOKEN=<cluster-token>" \
  --restart=unless-stopped --privileged --network=host \
  --volume gpustack-data:/var/lib/gpustack \
  gpustack/workersim:0.1.3 \
  --server-url http://<server> --worker-ip <host-ip> \
  --workers 20 --profile h100-80gb
```

**3.** Open the cluster in the UI. Twenty Workers named `<hostname>-sim-001` … `-020`
become **Ready**, each with eight H100 cards. Deploy models as usual.

```bash
docker logs -f workersim                    # startup and errors
curl -s http://127.0.0.1:20099/status       # fleet status
```

## Choose Workers and hardware

| Option | Default | Meaning |
|---|---|---|
| `--workers N` | `1` | Number of Workers (up to 1000 per container). |
| `--profile NAME` | `h100-80gb` | Hardware preset, see below. |
| `--gpus-per-worker N` | from preset | Cards per Worker. |
| `--gpu-name TEXT` | from preset | Device name shown in GPUStack. |
| `--gpu-memory-gib N` | from preset | Memory per card. |
| `--cpu-cores N` · `--memory-gib N` · `--disk-gib N` | from preset | Host CPU threads, RAM, disk. |
| `--label KEY=VALUE` | — | Worker label; repeat for more. |
| `--worker-name PREFIX` | `<hostname>-sim` | Name prefix: `PREFIX-001`, `PREFIX-002`, … |
| `--model-rps N` | `2` | Fake background requests/s per running model (`0` = idle). |
| `--dry-run` | — | Show what would start, then exit. |

| Preset | Device | Memory/card | Cards | CPU | RAM | Disk |
|---|---|---:|---:|---:|---:|---:|
| `b300-288gb` | NVIDIA B300 | 288 GiB | 8 | 224 | 2048 GiB | 8192 GiB |
| `h100-80gb` | NVIDIA H100 80GB HBM3 | 80 GiB | 8 | 224 | 2048 GiB | 8192 GiB |
| `a800-80gb` | NVIDIA A800-SXM4-80GB | 80 GiB | 8 | 128 | 1024 GiB | 4096 GiB |
| `rtx4090-24gb` | NVIDIA GeForce RTX 4090 | 24 GiB | 8 | 64 | 512 GiB | 2048 GiB |
| `ascend-910b4-64gb` | Ascend 910B4 | 64 GiB | 8 | 192 | 1536 GiB | 4096 GiB |

**Mixed fleet:** repeat `--group` instead of `--workers`/`--profile`:

```bash
  --group workers=10,profile=b300-288gb,gpus=4 \
  --group workers=3,profile=ascend-910b4-64gb
```

Group keys: `workers`, `profile`, `gpus`, `prefix`, `gpu-name`, `gpu-memory-gib`,
`cpu-cores`, `memory-gib`, `disk-gib`, `nvlink-domain-workers`.

**Environment variables:** every option also works as `-e WORKERSIM_<OPTION>`, for example
`-e WORKERSIM_WORKERS=20 -e WORKERSIM_PROFILE=b300-288gb`. Groups:
`-e WORKERSIM_GROUPS="workers=10,profile=b300-288gb;workers=3,profile=ascend-910b4-64gb"`.

## What gets simulated

- **Workers:** registration, heartbeats, status, labels and persistent identities.
- **Hardware:** GPUs/NPUs with usage, memory, temperature and power that follow the
  deployed models.
- **Deployments:** real scheduling results go through initializing → starting → running;
  replicas, multi-Worker, and PD (prefill/decode/router) layouts are supported.
- **Model files:** download progress and Ready state, without downloading anything.
- **APIs:** chat (incl. streaming), embeddings, rerank, transcription and speech return
  fixed synthetic responses through the normal GPUStack gateway.
- **Metrics:** Worker and model metrics in GPUStack's native format for Prometheus/Grafana.
- **Server load:** the same requests, watches and timings as real 2.3.1 Workers.

Nothing is computed: no inference, no GPU kernels, no weight downloads. Throughput,
latency and utilization numbers are synthetic and are not benchmarks.

---

## Advanced usage

### Notes on the docker command

- `--privileged` is optional. With it, WorkerSim reserves its ports on the host at each
  start (recommended above ~50 Workers), then runs as an unprivileged user.
- `--network=host` is required: GPUStack and Prometheus connect to the Workers' ports.
- `GPUSTACK_TOKEN`, `--server-url`, `--worker-ip`, `--advertise-address`, `--worker-port`
  and `--worker-metrics-port` work as on a real Worker. Other GPUStack flags are ignored,
  and the Docker socket is not used.
- Worker identities are kept in the `gpustack-data` volume (in `workersim/`). Keep the
  volume to keep the same Workers after a restart.
- To run a second WorkerSim container on the same host, give it its own volume and
  different `--worker-port` / `--worker-metrics-port` values.

### Fault and scale tests

The fleet control API listens on `127.0.0.1:20099` on the host.

```bash
# Take Workers offline: keep only the first 5 online
curl -X POST http://127.0.0.1:20099/scale -H 'Content-Type: application/json' -d '{"count": 5}'

# Make a model instance fail (it is restarted with backoff, as on a real Worker)
curl -X POST http://127.0.0.1:20099/fault -H 'Content-Type: application/json' \
  -d '{"worker": "<worker-name>", "instance_id": 10, "action": "fail-instance"}'
```

Offline Workers show as unavailable after GPUStack's heartbeat grace period.

### YAML configuration and Compose

For repeatable setups, describe the fleet in a file. Flags can be combined with
`--config`; flags win.

```yaml
server:
  url: http://10.0.0.5
network:
  worker_ip: 10.0.0.9
  advertise_address: 10.0.0.9
worker_groups:
  - name_prefix: sim-b300
    count: 10
    gpu_profile: b300-288gb
    gpu_count: 8
    cpu_cores: 192
    memory_gib: 2048
    disk_gib: 8192
    labels: {rack: a}
```

Start it with Compose: edit [`configs/docker.yaml`](configs/docker.yaml), then

```bash
export GPUSTACK_REGISTRATION_TOKEN='<cluster-token>'
docker compose up -d --no-build
```

More examples: [mixed GPU/NPU](configs/custom-gpu.yaml), [50 Workers](configs/fleet-50.yaml),
[100 B300 Workers](configs/fleet-100-b300.yaml), [1000 B300 Workers](configs/fleet-1000-b300.yaml).
Check a file with `workersim --config <file> validate`.

Extra per-group fields beyond the flags above:

| Field | Meaning |
|---|---|
| `nvlink_domain_workers` | Workers per multi-node NVLink domain (NVIDIA, e.g. `18` for NVL72 with 4 cards). Reported as the `nvidia.com/gpu.clique` topology. |
| `kv_transfer_ifname` | Reported as the Worker's KV-transfer network interface. |
| `reserved_ram_gib`, `reserved_vram_gib` | System reservation per Worker / per card (defaults 2 and 1 GiB). |

Network-level fields: `network.npu_roce_cidr` (default `10.200.0.0/16`) gives each Ascend
NPU a RoCE address, which MindIE multi-node placement needs; `null` disables it.
`network.worker_ip_cidr` gives each Worker its own IP (host addresses must exist; see
[`configure_virtual_network.py`](scripts/configure_virtual_network.py)).

A renamed or resized card keeps its preset's vendor, architecture and driver versions.

### Ports and large fleets

| Service | Default |
|---|---|
| Worker API | `20100` + Worker index |
| Worker metrics | `21100` + Worker index |
| Model endpoints | 20 ports per Worker from `40000` (1000 Workers: 40000–59999) |
| Fleet control | `127.0.0.1:20099` |

Model ports are opened only when a model runs. Complex PD/distributed layouts may need
more than 20 ports per Worker: raise `network.instance_port_end` in YAML.

For several hundred Workers, run with `--privileged` (or reserve ports yourself with
[`configure_port_reservations.py`](scripts/configure_port_reservations.py)), keep the
container's open-file limit at 65536 or more, and size the GPUStack Server and database
for the load: every simulated Worker talks to the Server as a real one does. Workers
start at 5 per second by default (`simulation.registration_rate`).

### How WorkerSim matches a real Worker

WorkerSim reproduces GPUStack 2.3.1's Worker traffic: six event watches per Worker,
heartbeat and status every 30 s, watch caches and reconnects, orphan-workload sweeps,
restart of failed instances with backoff (10 s doubling to 300 s), and runtime-metrics
collection normalized with the Server's metrics config. Details:
[communication contract](docs/native-worker-communication.md). A Server version mismatch
only logs a warning, as on a real Worker.

### Validation reports

| Scenario | Report |
|---|---|
| 1000-Worker protocol fixture; real-Server 500/1000 limits | [Scale validation](docs/scale-1000-validation.md) |
| 100 Workers / 800 B300 devices on unmodified GPUStack | [Integration](docs/workersim-040-validation.md) |
| GLM-5.3 PD 5P3D at 10–100 Workers | [PD experiment](docs/glm53-pd-benchmark-2026-11-17.md) |
| Enterprise build: 50 B300 Workers, 116 running replicas | [Deployment](docs/demo2-deployment-2026-11-17.md) |
| Ascend: 3 Workers, 24 × 910B4 | [910B4 acceptance](docs/demo2-910b4-2026-11-17.md) |
| Upgrade with custom CUDA/CANN devices | [Upgrade](docs/demo2-upgrade-2026-11-20.md) |

### Limitations

- Only NVIDIA and Ascend devices are simulated.
- Benchmarks and cache services are not executed; tunnel proxy mode, `/cluster-proxy` and
  GGUF parsing are not simulated.
- Embeddings are fixed vectors, rerank keeps input order, speech is 0.25 s of silence.
- Model metrics appear about 30 s after a model starts running, as on a real Worker.
- Not every backend/layout combination has been validated against a real Server.

### Development

Requires Python 3.11+.

```bash
pip install -e '.[dev]'
ruff check workersim scripts tests
pytest -q
workersim --server-url http://<server> --workers 2 --dry-run
```

Releases: [publishing guide](docs/publishing.md).

## License

See [LICENSE](LICENSE). Derived GPUStack code keeps its
[original license](workersim/_vendor/LICENSE-GPUStack.txt) and
[attribution](workersim/_vendor/NOTICE.md).
