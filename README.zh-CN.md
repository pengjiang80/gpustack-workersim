# GPUStack WorkerSim

[English](README.md) · [发布流程](docs/publishing.md)

**只用 CPU 主机，验证 GPUStack 的节点管理、模型调度、部署生命周期和监控展示。**

WorkerSim 是独立的 **GPUStack Worker 协议模拟器**，无需修改 Server 或 UI。Linux 下单进程最多管理 **1000 个独立节点**，每个节点拥有自己的身份、心跳、事件订阅、HTTP 接口和监控端口。

WorkerSim 使用独立产品版本。各版本支持的 GPUStack 版本见 [Releases](https://github.com/pengjiang80/gpustack-workersim/releases)，启动时会像原生 Worker 一样检查 Server 版本，版本不一致只记录警告，不阻止启动。

调度由真实 GPUStack Server 执行，WorkerSim 接收任务并模拟节点行为。无需真实 GPU、模型权重或推理引擎，监控指标均为合成数据。

## 功能特性

- 独立 Worker 注册、凭据、心跳、认证接口及身份持久化。
- 配置节点数量、GPU/NPU 模板、自定义设备名称和单卡显存、卡数、CPU、内存、磁盘和标签，并上报 NVLink 域拓扑事实和每张 NPU 的 RoCE 地址。
- 接收真实调度结果，上报初始化、启动、运行、失败和停止状态。
- 支持普通副本、跨 Worker、PD 分离及已验证的 vLLM/SGLang 编排布局。
- 按原生通信方式保持每节点 6 路 watch、独立连接池、心跳/状态周期、缓存、重连重放、权威回收检查、孤儿负载清理和 runtime metrics 读取，使 Server 压力与真实 Worker 一致。
- 模拟文件下载进度、Ready 状态、虚拟路径和合成大小。
- 上报 Worker 节点指标，并以 vLLM、SGLang、MindIE、PD Router 和 SGLang 网关的原生指标名暴露引擎指标；Worker 按 Server 的 `/v2/metrics/config` 归一化，与 GPUStack runtime metrics 聚合器一致。
- 提供模拟聊天、流式聊天、embedding、rerank、语音转写和静音 WAV。
- 模拟节点离线及实例故障，通过 GPUStack 正常机制验证恢复，包括 Worker 对 `restart_on_error` 模型按指数退避（10 秒起翻倍，最长 300 秒）重启实例。

## Docker 快速启动

WorkerSim 可直接使用 GPUStack 在 **集群 → 添加 Worker** 中给出的命令启动。需要当前
WorkerSim Release 支持的 GPUStack Server，以及 Linux **amd64** 主机。

### 1. 复制 GPUStack 的添加 Worker 命令

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

### 2. 修改三处

1. 镜像换成 **`gpustack/workersim:0.1.3`**。
2. 删除 **`--runtime nvidia`**（或 `ascend` 等）及 `--device /dev/infiniband`：CPU 主机没有
   这些运行时和设备，Docker 会拒绝启动。
3. 在末尾追加节点数量和硬件预设。

```bash
sudo docker run -d --name workersim \
  -e "GPUSTACK_TOKEN=<集群Token>" \
  --restart=unless-stopped --privileged --network=host \
  --volume gpustack-data:/var/lib/gpustack \
  gpustack/workersim:0.1.3 \
  --server-url http://<server> --worker-ip <主机IP> \
  --workers 20 --profile h100-80gb
```

在 UI 中可以看到 20 个名为 `<主机名>-sim-001`… 的 Worker 变为 **Ready**，每个有 8 张
H100。之后照常部署模型，由真实 Server 调度。

原命令的其他参数可以保留。Docker socket 和 `GPUSTACK_RUNTIME_DEPLOY_MIRRORED_NAME`
会被忽略，可删除。`--privileged` 让 WorkerSim 每次启动时在主机上预留监听端口，避免被
自动分配的源端口占用（大规模节点需要），随后降为 UID 10001 运行；不加该参数也能启动，
只会在日志中提示未预留端口。身份保存在 `gpustack-data` 的 `workersim/` 目录下，不会修改
真实 Worker 的文件。

```bash
docker logs -f workersim
curl -s http://127.0.0.1:20099/status   # 在主机上查看 Fleet 状态
```

### 节点数量与硬件

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--workers N` | `1` | 本容器模拟的 Worker 数（1–1000）。 |
| `--profile 名称` | `h100-80gb` | 硬件预设：设备及典型主机配置，`--list-profiles` 可列出全部。 |
| `--gpus-per-worker N` | 预设 | 每个 Worker 的卡数。 |
| `--gpu-name 文本`、`--gpu-memory-gib N` | 预设 | 上报的设备名称和单卡显存。 |
| `--cpu-cores N`、`--memory-gib N`、`--disk-gib N` | 预设 | 每个 Worker 的 CPU 线程、内存和磁盘。 |
| `--label KEY=VALUE` | — | Worker 标签，可重复。 |
| `--nvlink-domain-workers N` | — | 每个多节点 NVLink 域的 Worker 数（NVIDIA）。 |
| `--model-rps N` | `2` | 每个运行中模型的合成后台请求速率，`0` 表示空闲。 |
| `--worker-name 前缀` | `<主机名>-sim` | Worker 名称前缀（`前缀-001` …）。 |
| `--group KEY=VALUE,...` | — | 为混合集群添加分组，可重复。键：`workers`、`profile`、`gpus`、`prefix`、`gpu-name`、`gpu-memory-gib`、`cpu-cores`、`memory-gib`、`disk-gib`、`nvlink-domain-workers`。 |
| `--dry-run` | — | 打印解析后的节点、地址和端口后退出。 |

每个参数也可用环境变量设置，例如 `-e WORKERSIM_WORKERS=20 -e WORKERSIM_PROFILE=b300-288gb`；
分组使用 `WORKERSIM_GROUPS="workers=10,profile=b300-288gb;workers=3,profile=ascend-910b4-64gb"`。

| 预设 | 设备 | 单卡显存 | 卡数 | CPU | 内存 | 磁盘 |
|---|---|---:|---:|---:|---:|---:|
| `b300-288gb` | NVIDIA B300 | 288 GiB | 8 | 224 | 2048 GiB | 8192 GiB |
| `h100-80gb` | NVIDIA H100 80GB HBM3 | 80 GiB | 8 | 224 | 2048 GiB | 8192 GiB |
| `a800-80gb` | NVIDIA A800-SXM4-80GB | 80 GiB | 8 | 128 | 1024 GiB | 4096 GiB |
| `rtx4090-24gb` | NVIDIA GeForce RTX 4090 | 24 GiB | 8 | 64 | 512 GiB | 2048 GiB |
| `ascend-910b4-64gb` | Ascend 910B4 | 64 GiB | 8 | 192 | 1536 GiB | 4096 GiB |

混合示例：10 个 4 卡 B300 Worker 加 3 个 Ascend Worker。

```bash
gpustack/workersim:0.1.3 --server-url http://<server> --worker-ip <主机IP> \
  --group workers=10,profile=b300-288gb,gpus=4 \
  --group workers=3,profile=ascend-910b4-64gb
```

原生参数 `--worker-port`、`--worker-metrics-port` 设置第一个 Worker 的 API 和 metrics 端口，
其他未识别的原生参数记录日志后忽略。同一主机运行第二个容器时，应使用不同的
`--worker-port` / `--worker-metrics-port` 和独立 volume。

### Compose 与 YAML

需要用文件描述节点时，Compose 方式保持不变：编辑 [`configs/docker.yaml`](configs/docker.yaml)，
设置 `GPUSTACK_REGISTRATION_TOKEN` 后执行 `docker compose up -d --no-build`。参数也可与挂载的
`--config` 文件组合，参数优先。

Docker 镜像仓库为 [`gpustack/workersim`](https://hub.docker.com/r/gpustack/workersim)。

## 节点与设备配置

```yaml
server:
  url: http://127.0.0.1:8080
  cluster_id: 1
network:
  advertise_address: 172.17.0.1
  worker_ip: 172.17.0.1
simulation:
  namespace: workersim-b300
  data_dir: /data
  registration_rate: 2
  reconcile_interval: 0
  model_requests_per_second: 2
worker_groups:
  - name_prefix: sim-b300
    count: 10
    gpu_profile: b300-288gb
    gpu_count: 8
    cpu_cores: 192
    memory_gib: 2048
    disk_gib: 8192
```

`model_requests_per_second` 默认生成每实例约 2 RPS 的合成后台流量，与实际客户端请求无关；设为 0 可显示空闲模型流量。

| 预设 | 设备 | 每卡模拟显存 | 运行时 |
|---|---|---:|---|
| `b300-288gb` | NVIDIA B300 | 288 GiB | CUDA |
| `h100-80gb` | NVIDIA H100 | 80 GiB | CUDA |
| `a800-80gb` | NVIDIA A800 | 80 GiB | CUDA |
| `rtx4090-24gb` | NVIDIA RTX 4090 | 24 GiB | CUDA |
| `ascend-910b4-64gb` | Ascend 910B4 | 64 GiB | CANN |

容量、版本和功耗属于模拟配置，不代表真实设备的精确参数。Ascend 按 `ascend/cann` 上报，不填写 CUDA compute capability。

### 拓扑与设备网络

| 字段 | 位置 | 含义 |
|---|---|---|
| `nvlink_domain_workers` | worker group | 每个多节点 NVLink 域的 Worker 数，仅 NVIDIA。每张卡上报 `nvidia.com/gpu.clique` 拓扑提示（`<fabric-uuid>.0`），Worker 上报一致的 `topology_facts`，与原生 NVML fabric 探测一致。单机 HGX 不上报域，应省略。 |
| `kv_transfer_ifname` | worker group | 原样上报为 `kv_transfer_ifname`，对应原生 `--kv-transfer-ifname`。 |
| `npu_roce_cidr` | `network` | 合成 NPU RoCE 地址段（默认 `10.200.0.0/16`，每个 Worker 槽位 64 个地址）。Ascend 设备以该地址上报 `network.status: up`，MindIE 分布式放置需要该信息。设为 `null` 则不上报设备网络。 |

### 自定义设备名称和单卡显存

保留内置 `gpu_profile` 作为模板，每个分组可以分别覆盖名称、显存，或同时覆盖：

```yaml
worker_groups:
  - name_prefix: sim-custom
    count: 10
    gpu_profile: b300-288gb
    gpu_name: "我的测试 GPU"
    gpu_memory_gib: 144
    gpu_count: 8
    memory_gib: 2048
```

| 字段 | 含义 |
|---|---|
| `gpu_name` | 上报给 GPUStack、用于监控标签的设备名称；必须是非空字符串，首尾空白会去除。 |
| `gpu_memory_gib` | **每张卡**的总显存，单位 GiB（`1 GiB = 2^30 字节`）；支持有限正数，包括 `96.5` 等小数，上报为整数字节。 |
| `memory_gib` | 主机内存，独立于单卡显存。 |

字段省略或设为 `null` 时继承模板值。覆盖作用于分组内所有卡，不修改内置模板或
其他分组。`reserved_vram_gib` 必须小于覆盖后的单卡容量；默认预留 1 GiB，
模拟更小容量时需相应降低预留值。

厂商、CUDA/CANN 类型、架构、compute capability、驱动/运行时版本、功耗等仍
继承模板。新名称不会自动切换架构或推理后端，应选择适合调度场景的模板。
自定义名称和显存通过正常状态上报传给 GPUStack，监控使用相同数据。
修改配置后重启 WorkerSim，保留身份 volume 和节点名称。

完整示例见 [混合 GPU/NPU 配置](configs/custom-gpu.yaml)。用于 Compose 时，
先调整地址，再将配置挂载改为 `configs/custom-gpu.yaml:/config/fleet.yaml:ro`。
启动前可以校验：

```bash
workersim --config configs/custom-gpu.yaml validate
```

示例：[50 个混合 GPU 节点](configs/fleet-50.yaml)、[100 个八卡 B300 节点](configs/fleet-100-b300.yaml)、[1000 个八卡 B300 节点](configs/fleet-1000-b300.yaml)、[3 个八卡 910B4 节点](configs/demo2-3-910b4.yaml)。

## 网络与持久化

默认同 IP、不同端口。1000 节点的端口布局如下：

| 用途 | 端口范围 |
|---|---|
| Worker API | `20100–21099`，每节点 1 个 |
| Worker 监控 | `21100–22099`，每节点 1 个 |
| 模型端口池 | `40000–59999`，每节点 20 个 |
| 控制接口 | `127.0.0.1:20099` |

第一个节点使用 `40000–40019`，第 1000 个使用 `59980–59999`。这是模拟器自己的默认分配，不改变 GPUStack Server；只有已分配的模型任务才打开模型监听端口。复杂 PD/分布式布局可能需要更多端口，可增大 `network.instance_port_end`，同时减少单 IP 节点数量或拆分 IP/主机；预留脚本提供 `--ports-per-worker`、`--start-port` 与自定义配置对应。

启动 1000 节点时，在 Compose 中将配置挂载改为 `configs/fleet-1000-b300.yaml:/config/fleet.yaml:ro`，执行 `sudo python3 scripts/configure_port_reservations.py --workers 1000 --expand-ephemeral-range --apply`，保留 `nofile=65536`。脚本预留 API、监控、控制及模型端口，并显式扩大测试主机的自动源端口范围，为数千路独立出站连接留足空间。

每节点保持 **6 路常驻 watch** 和独立连接池。心跳与状态启动后立即发送，每次完成后间隔 30 秒；Runtime metrics 保留原生查询 Worker 的周期。不设置跨节点 REST 限流，也不共享连接池来降低 Server 压力。请求、缓存、重连与执行范围见[原生通信说明](docs/native-worker-communication.md)。

默认每秒最多启动 5 个节点（`registration_rate`），同时启动名额为 8（`registration_concurrency`），这两个参数只控制建起 Fleet 的节奏。保留 `reconcile_interval: 0` 使用原生 watch 缓存行为。实际 CPU、内存和 Server/数据库压力取决于模型数与事件量；模拟器支持 1000 节点不代表调度吞吐保证，验收可使用 GPUStack 文档中的数据库池参数调整 Server。

同主机多个进程应使用不同端口池。独立 IP 可通过 `network.worker_ip_cidr` 配置，并先准备宿主机地址和路由；可参考 [`configure_virtual_network.py`](scripts/configure_virtual_network.py)。

保留数据目录或 Docker volume 即可保留 Worker 身份。每个命名空间对应一个 Server URL，进程锁防止并发使用同一套身份。节点配置改变后需重启，保持节点名和分组顺序可稳定端口。

## 扩缩容与故障测试

模型可直接通过 UI/API 部署。自动副本测试另需平台管理员 API Key：

```bash
export GPUSTACK_API_KEY='<GPUStack平台管理员APIKey>'
workersim --config configs/local.yaml scenario --replicas 2 5 1 0 --timeout 300
```

命令创建自己管理的测试模型，调整副本数并等待收敛，最后删除该测试模型。报告保存至 `outputs/scenario.json`，耗时包含调度、模拟启动和控制器处理。

```bash
workersim --config configs/local.yaml fleet-scale 1
workersim --config configs/local.yaml fleet-scale 2
workersim --config configs/local.yaml stop
```

`fleet-scale` 改变在线数量，不代表注册或删除 Worker。离线展示由 Server 心跳宽限期决定。控制 API 的 `POST /fault` 可注入实例运行失败。

## 验证与使用限制

- demo2：[53 个模拟节点、28 个模型保留升级，以及 CUDA/CANN 自定义名称与显存验证](docs/demo2-upgrade-2026-11-20.md)。
- 原生通信：[1000 节点协议回归通过，8000 张设备、6000 路 watch；原版 Server 的 500/1000 节点稳定性尚未通过](docs/scale-1000-validation.md)。
- 原版 GPUStack：[100 Worker / 800 张 B300 验证](docs/workersim-040-validation.md)。
- GLM-5.3 5P3D：[10–100 Worker 规模实验](docs/glm53-pd-benchmark-2026-11-17.md)，具体版本及测量边界见报告。
- 已测企业版：[50 个 B300 Worker、16 个模型、116 个 Running 副本验收快照](docs/demo2-deployment-2026-11-17.md)。
- Ascend：[3 个 Worker、24 张 64 GiB 设备及监控、双 watch、重启恢复](docs/demo2-910b4-2026-11-17.md)。

兼容版本和协议来源 commit 在每个 [Release](https://github.com/pengjiang80/gpustack-workersim/releases) 中说明。其他版本需要单独验证。`server.allow_version_mismatch` 已废弃并被忽略：与原生 Worker 一样，版本不一致只记录警告。

不执行真实推理、权重下载、GPU kernel、NCCL/Ray 或 KV 数据传输。embedding 为测试向量，rerank 使用固定顺序，转写返回模拟提示，TTS 固定返回 **0.25 秒静音 WAV**。指标不能用于真实推理性能、请求审计或计费。模型指标在原生的 30 秒聚合延迟后出现，最后一次成功聚合 30 秒后过期。

Benchmark 与 Cache Service 按原生方式订阅和轮询，但不模拟其执行和日志（`/benchmark_logs`、`/cacheServiceInstanceLogs`）；也不模拟 tunnel 代理模式、`/cluster-proxy` 和 GGUF 解析。Managed（workload-report）实例由 Server 驱动恢复，WorkerSim 不执行其原生 managed workload 同步。

部分企业版实例可能残留历史下载百分比；模型文件 Ready/100% 与实例 Running 的验证结果见报告。资源放置和调度始终由 Server 决定，未验证所有 backend/layout 组合。

## 本地开发与发布

需要 Python 3.11+，大规模模拟推荐 Linux：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
workersim --config configs/local.yaml validate
ruff check workersim scripts tests
ulimit -n 65536
pytest -q
```

编辑本地配置、设置注册 Token 后，执行 `workersim --config configs/local.yaml run`。Windows 使用 `.venv\Scripts\Activate.ps1` 激活环境。回环协议测试与实际 Server/UI 集成验证分别执行。GitHub Actions、Docker Hub 凭据和版本标签规则见[发布说明](docs/publishing.md)。

## 许可证

见 [LICENSE](LICENSE)。派生 GPUStack helper 保留[原许可证](workersim/_vendor/LICENSE-GPUStack.txt)和[来源说明](workersim/_vendor/NOTICE.md)。
