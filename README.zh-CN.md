# GPUStack WorkerSim

[![CI](https://github.com/pengjiang80/gpustack-workersim/actions/workflows/ci.yml/badge.svg)](https://github.com/pengjiang80/gpustack-workersim/actions/workflows/ci.yml)
[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-gpustack%2Fworkersim-2496ED?logo=docker)](https://hub.docker.com/r/gpustack/workersim)

**在一台 CPU 机器上，为真实 GPUStack 集群添加成百上千个模拟 GPU Worker。**

WorkerSim 向未修改的 GPUStack Server 注册虚拟 Worker。它们在 UI 中显示 GPU/NPU，接收真实
调度、"运行"模型并上报指标，与真实 Worker 一致，无需 GPU 即可测试调度、部署和 Server 性能。

[English](README.md) · [Releases](https://github.com/pengjiang80/gpustack-workersim/releases)

## 快速开始

需要 GPUStack **2.3.1** Server，以及一台装有 Docker 的 Linux amd64 主机。

**1.** 在 GPUStack 中打开 **集群 → 添加 Worker**，复制命令，形如：

```bash
sudo docker run -d --name gpustack-worker \
  -e "GPUSTACK_RUNTIME_DEPLOY_MIRRORED_NAME=gpustack-worker" \
  -e "GPUSTACK_TOKEN=<集群Token>" \
  --restart=unless-stopped --privileged --network=host \
  --volume /var/run/docker.sock:/var/run/docker.sock \
  --volume gpustack-data:/var/lib/gpustack \
  --runtime nvidia \
  gpustack/gpustack:v2.3.1 \
  --server-url http://<server> --worker-ip <主机IP>
```

**2.** 修改三处：

- 镜像换成 `gpustack/workersim:0.1.3`；
- 删除 `--runtime nvidia`（如有 `--device /dev/infiniband` 也删除）；
- 在末尾加上 Worker 数量和硬件。

```bash
sudo docker run -d --name workersim \
  -e "GPUSTACK_TOKEN=<集群Token>" \
  --restart=unless-stopped --privileged --network=host \
  --volume gpustack-data:/var/lib/gpustack \
  gpustack/workersim:0.1.3 \
  --server-url http://<server> --worker-ip <主机IP> \
  --workers 20 --profile h100-80gb
```

**3.** 在 UI 中查看集群：20 个名为 `<主机名>-sim-001` … `-020` 的 Worker 变为 **Ready**，
每个有 8 张 H100。之后照常部署模型。

```bash
docker logs -f workersim                    # 启动日志和错误
curl -s http://127.0.0.1:20099/status       # Fleet 状态
```

## 选择 Worker 数量和硬件

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--workers N` | `1` | Worker 数量（每个容器最多 1000）。 |
| `--profile 名称` | `h100-80gb` | 硬件预设，见下表。 |
| `--gpus-per-worker N` | 预设值 | 每个 Worker 的卡数。 |
| `--gpu-name 文本` | 预设值 | GPUStack 中显示的设备名。 |
| `--gpu-memory-gib N` | 预设值 | 单卡显存。 |
| `--cpu-cores N` · `--memory-gib N` · `--disk-gib N` | 预设值 | 主机 CPU 线程、内存、磁盘。 |
| `--label KEY=VALUE` | — | Worker 标签，可重复。 |
| `--worker-name 前缀` | `<主机名>-sim` | 名称前缀：`前缀-001`、`前缀-002` … |
| `--model-rps N` | `2` | 每个运行中模型的模拟后台请求速率（`0` 为空闲）。 |
| `--dry-run` | — | 只显示将要启动的内容，然后退出。 |

| 预设 | 设备 | 单卡显存 | 卡数 | CPU | 内存 | 磁盘 |
|---|---|---:|---:|---:|---:|---:|
| `b300-288gb` | NVIDIA B300 | 288 GiB | 8 | 224 | 2048 GiB | 8192 GiB |
| `h100-80gb` | NVIDIA H100 80GB HBM3 | 80 GiB | 8 | 224 | 2048 GiB | 8192 GiB |
| `a800-80gb` | NVIDIA A800-SXM4-80GB | 80 GiB | 8 | 128 | 1024 GiB | 4096 GiB |
| `rtx4090-24gb` | NVIDIA GeForce RTX 4090 | 24 GiB | 8 | 64 | 512 GiB | 2048 GiB |
| `ascend-910b4-64gb` | Ascend 910B4 | 64 GiB | 8 | 192 | 1536 GiB | 4096 GiB |

**混合集群：** 用多个 `--group` 代替 `--workers`/`--profile`：

```bash
  --group workers=10,profile=b300-288gb,gpus=4 \
  --group workers=3,profile=ascend-910b4-64gb
```

分组键：`workers`、`profile`、`gpus`、`prefix`、`gpu-name`、`gpu-memory-gib`、
`cpu-cores`、`memory-gib`、`disk-gib`、`nvlink-domain-workers`。

**环境变量：** 每个参数也可写成 `-e WORKERSIM_<参数>`，例如
`-e WORKERSIM_WORKERS=20 -e WORKERSIM_PROFILE=b300-288gb`。分组：
`-e WORKERSIM_GROUPS="workers=10,profile=b300-288gb;workers=3,profile=ascend-910b4-64gb"`。

## 模拟内容

- **Worker：** 注册、心跳、状态、标签和持久化身份。
- **硬件：** GPU/NPU 的使用率、显存、温度和功耗随部署的模型变化。
- **部署：** 真实调度结果经过 initializing → starting → running；支持副本、跨 Worker 和
  PD（prefill/decode/router）布局。
- **模型文件：** 下载进度和 Ready 状态，不实际下载。
- **接口：** 聊天（含流式）、embedding、rerank、转写和语音通过 GPUStack 网关返回固定的模拟结果。
- **指标：** GPUStack 原生格式的 Worker 与模型指标，可接入 Prometheus/Grafana。
- **Server 压力：** 请求、watch 和周期与真实 2.3.1 Worker 一致。

不进行任何计算：没有推理、GPU kernel 或权重下载。吞吐、延迟和使用率均为模拟值，不是性能测试结果。

---

## 进阶用法

### docker 命令说明

- `--privileged` 可选。加上后，WorkerSim 每次启动会在主机上预留端口（约 50 个 Worker 以上建议使用），
  随后以非特权用户运行。
- 必须使用 `--network=host`：GPUStack 和 Prometheus 需要连接 Worker 端口。
- `GPUSTACK_TOKEN`、`--server-url`、`--worker-ip`、`--advertise-address`、`--worker-port`、
  `--worker-metrics-port` 与真实 Worker 含义相同。其他 GPUStack 参数会被忽略，不使用 Docker socket。
- Worker 身份保存在 `gpustack-data` volume 的 `workersim/` 目录。保留 volume，重启后仍是同一批 Worker。
- 同一主机运行第二个 WorkerSim 容器时，使用独立 volume 和不同的 `--worker-port` / `--worker-metrics-port`。

### 故障与扩缩容测试

Fleet 控制接口监听在主机的 `127.0.0.1:20099`。

```bash
# 让部分 Worker 离线：只保留前 5 个在线
curl -X POST http://127.0.0.1:20099/scale -H 'Content-Type: application/json' -d '{"count": 5}'

# 让某个模型实例失败（与真实 Worker 一样按退避策略重启）
curl -X POST http://127.0.0.1:20099/fault -H 'Content-Type: application/json' \
  -d '{"worker": "<worker名>", "instance_id": 10, "action": "fail-instance"}'
```

离线状态在 GPUStack 心跳宽限期之后显示。

### YAML 配置与 Compose

需要可重复的配置时，用文件描述 Fleet。参数可与 `--config` 组合，参数优先。

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

使用 Compose 启动：编辑 [`configs/docker.yaml`](configs/docker.yaml)，然后

```bash
export GPUSTACK_REGISTRATION_TOKEN='<集群Token>'
docker compose up -d --no-build
```

更多示例：[GPU/NPU 混合](configs/custom-gpu.yaml)、[50 个 Worker](configs/fleet-50.yaml)、
[100 个 B300 Worker](configs/fleet-100-b300.yaml)、[1000 个 B300 Worker](configs/fleet-1000-b300.yaml)。
可用 `workersim --config <文件> validate` 检查。

参数之外的分组字段：

| 字段 | 含义 |
|---|---|
| `nvlink_domain_workers` | 每个多节点 NVLink 域的 Worker 数（NVIDIA，例如 4 卡 NVL72 为 `18`），以 `nvidia.com/gpu.clique` 拓扑上报。 |
| `kv_transfer_ifname` | 上报为 Worker 的 KV 传输网卡。 |
| `reserved_ram_gib`、`reserved_vram_gib` | 每个 Worker / 每张卡的系统预留（默认 2 和 1 GiB）。 |

网络字段：`network.npu_roce_cidr`（默认 `10.200.0.0/16`）为每张 Ascend NPU 分配 RoCE 地址，
MindIE 多节点放置需要；设为 `null` 关闭。`network.worker_ip_cidr` 为每个 Worker 分配独立 IP
（需先在主机上配置地址，参见 [`configure_virtual_network.py`](scripts/configure_virtual_network.py)）。

改名或修改显存的卡仍保留预设的厂商、架构和驱动版本。

### 端口与大规模部署

| 服务 | 默认值 |
|---|---|
| Worker API | `20100` + Worker 序号 |
| Worker metrics | `21100` + Worker 序号 |
| 模型端口 | 每个 Worker 20 个，从 `40000` 起（1000 个 Worker：40000–59999） |
| Fleet 控制 | `127.0.0.1:20099` |

模型端口只在模型运行时打开。复杂的 PD/分布式布局可能需要每个 Worker 多于 20 个端口，
可在 YAML 中调大 `network.instance_port_end`。

数百个 Worker 时，请使用 `--privileged`（或用
[`configure_port_reservations.py`](scripts/configure_port_reservations.py) 自行预留端口），
保持容器打开文件数上限不低于 65536，并按负载配置 GPUStack Server 和数据库：每个模拟 Worker
都像真实 Worker 一样访问 Server。默认每秒启动 5 个 Worker（`simulation.registration_rate`）。

### 与真实 Worker 的一致性

WorkerSim 复现 GPUStack 2.3.1 Worker 的通信：每个 Worker 6 路事件 watch、每 30 秒心跳和状态、
watch 缓存与重连、孤儿负载清理、失败实例按退避（10 秒起翻倍，最长 300 秒）重启，以及按 Server
指标配置归一化的 runtime metrics。详见[通信约定](docs/native-worker-communication.md)。
Server 版本不一致时与真实 Worker 一样只记录警告。

### 验证报告

| 场景 | 报告 |
|---|---|
| 1000 Worker 协议测试；真实 Server 500/1000 限制 | [规模验证](docs/scale-1000-validation.md) |
| 原版 GPUStack 上 100 Worker / 800 张 B300 | [集成验证](docs/workersim-040-validation.md) |
| GLM-5.3 PD 5P3D，10–100 Worker | [PD 实验](docs/glm53-pd-benchmark-2026-11-17.md) |
| 企业版：50 个 B300 Worker，116 个运行副本 | [部署验收](docs/demo2-deployment-2026-11-17.md) |
| Ascend：3 个 Worker，24 张 910B4 | [910B4 验收](docs/demo2-910b4-2026-11-17.md) |
| 自定义 CUDA/CANN 设备的升级 | [升级验证](docs/demo2-upgrade-2026-11-20.md) |

### 限制

- 仅模拟 NVIDIA 和 Ascend 设备。
- 不执行 Benchmark 和 Cache Service；不模拟 tunnel 代理模式、`/cluster-proxy` 和 GGUF 解析。
- embedding 为固定向量，rerank 保持输入顺序，语音为 0.25 秒静音。
- 模型指标在模型运行约 30 秒后出现，与真实 Worker 一致。
- 并非所有 backend/布局组合都已在真实 Server 上验证。

### 本地开发

需要 Python 3.11+。

```bash
pip install -e '.[dev]'
ruff check workersim scripts tests
pytest -q
workersim --server-url http://<server> --workers 2 --dry-run
```

发布流程：[发布指南](docs/publishing.md)。

## 许可证

见 [LICENSE](LICENSE)。派生自 GPUStack 的代码保留其[原始许可证](workersim/_vendor/LICENSE-GPUStack.txt)和[署名说明](workersim/_vendor/NOTICE.md)。
