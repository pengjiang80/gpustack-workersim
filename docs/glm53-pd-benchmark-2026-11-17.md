# GLM-5.3 5P3D 调度规模测试报告

2026-11-17，Asia/Singapore。六档有效测量均完整部署成功。曾观察到一次 60 节点启动失败：调度已成功，但三个实例绑定端口失败；确认是宿主机随机源端口抢占，配置服务端口保留后重测。该失败独立保留，没有算作成功，也没有归因于 GPUStack 调度算法。

| Worker 数 | 模拟 B300 数 | WorkerSim | 创建到 GPU 组调度提交（秒） | 创建到完整 PD 就绪（秒） | 结果 |
|---:|---:|---|---:|---:|---|
| 10 | 80 | 0.4.2 | 45.911 | 60.556 | 成功，实例 Error 0 |
| 20 | 160 | 0.4.2 | 87.736 | 97.786 | 成功，实例 Error 0 |
| 40 | 320 | 0.4.2 | 204.587 | 216.177 | 成功，实例 Error 0 |
| 60 | 480 | 0.4.3 | 328.529 | 343.344 | 成功，实例 Error 0 |
| 80 | 640 | 0.4.3 | 411.541 | 430.038 | 成功，实例 Error 0 |
| 100 | 800 | 0.4.3 | 551.753 | 574.775 | 成功，实例 Error 0 |

首次 60 节点尝试：调度提交 **324.225 秒**，首个实例 Error 出现在 **336.755 秒**。共 3 个实例发生 `address already in use`，未完整就绪；保留观察至 546.220 秒的记录后中止，修复环境重测。这不是 30 分钟超时结果。原始证据见 `bench-r4-port-conflict-060/`。

测试在同一台 Vultr 新加坡 Shared CPU（8 vCPU / 16 GiB）上运行。Server/UI 始终是原版 GPUStack 2.3.1；每档使用全新 Server 和 Worker 身份数据卷，一次一个模型。Server 指定 bootstrap_password、disable_update_check，禁用真实 Worker。没有修改 Scheduler、Server 或 UI。

Server 固定镜像：`gpustack/gpustack@sha256:2573799fd23f9973838323e93ca0c23b41ea29b8fd8a28bdb1f12ba93c5d0f6a`。

模型为[官方 zai-org/GLM-5.3](https://huggingface.co/zai-org/GLM-5.3)，HF 来源；5 Prefill、3 Decode、1 Router，vllm-nixl，TP=8，gpu-memory-utilization=0.9，spread，readiness=all。未指定 worker_selector/gpu_selector，全部节点参与候选评估；没有使用通用 fixture、跳过模型评估或手工 VRAM claim。每个模拟节点 8 张 B300、192 CPU、2048 GiB RAM、8192 GiB 磁盘。节点共用宿主机 IP，拥有独立身份、API 端口和双 Watch。

每档先等待全部节点 Ready、双 Watch 连通，并验证 Server 容器可访问首尾 Worker 健康接口，然后静置 10 秒。注册及 Server 冷启动不计入模型部署耗时。起点是提交 ModelCreate 请求之前；使用同主机 UTC 和 monotonic 时钟。调度列取原版全组事务提交后的八条 `Scheduled group member` 日志中的最后时间，不能用仅表示方案已求出的 `Group placed` 代替。

完整 PD 就绪要求九个实例 Running、原版 Model Running，并直接读取 Router `/workers`，八个 URL/角色与实际 5P3D 分配完全一致。`pairing_remote` 表示 P/D 没有同 Worker 配对，是本次 TP8 分离部署的正常拓扑提示；不会因该提示判失败。

| Worker 数 | API 首次观察到 GPU 绑定（秒） | 九实例 Running（秒） | HF 配置读取入口记录数 | 注册收敛（秒，不计入部署） | 实例/文件 Watch |
|---:|---:|---:|---:|---:|---|
| 10 | 46.412 | 60.555 | 185 | 10.467 | 10/10 |
| 20 | 87.921 | 97.785 | 345 | 16.627 | 20/20 |
| 40 | 204.900 | 216.175 | 665 | 21.182 | 40/40 |
| 60 | 328.912 | 343.342 | 995 | 31.475 | 60/60 |
| 80 | 412.051 | 430.037 | 1315 | 43.664 | 80/80 |
| 100 | 552.475 | 574.232 | 1643 | 55.448 | 100/100 |

API 约每 0.5 秒轮询；HTTP 查询和资源采样会延长个别间隔，原始文件保留最后未绑定/首次已绑定的时间上下界。绑定前耗时包括队列、真实 HF 元数据读取、资源计算和提交；后续耗时包括 Watch、模拟下载/启动及原版控制器收敛。模拟下载 1 秒、启动 2 秒，并非真实大模型权重加载或推理耗时。

有一个需要保留的实验边界：10/20/40 有效样本使用 0.4.2，60/80/100 使用补齐周期健康同步的 0.4.3；前者未发生状态回退。前三档端口保留尚未配置，但没有端口冲突；后三档已保留 40000–46399。原版 Server、物理主机、模型和部署参数相同，但这不是完全同一模拟器版本与主机配置的严格统计对照。各档仅一个有效样本，不能推导可靠失败率、P95 或生产 SLA。

主机修正的证据：`ss` 显示 40128 被 docker-proxy 的已建立 HTTP 连接占用，42240 被 WorkerSim 到 Server 的已建立 HTTP 连接占用；主机随机端口范围为 32768–60999，原 reserved_ports 为空。使用 `scripts/configure_port_reservations.py --workers 100 --apply` 合并保留 40000–46399，保留已有设置并持久化。该参数只排除自动源端口分配，不阻止显式服务绑定，见[Linux 内核文档](https://www.kernel.org/doc/html/latest/networking/ip-sysctl.html)。现有连接/TIME_WAIT 不会被该设置清除，重测使用新容器和新数据卷。

测试过程中也补齐了模拟器协议：0.4.1 新增 Router 成员增删/回读和 ChatCompletion.created；0.4.2 将端口、PID、Initializing 合并上报，并确认/重试状态转换；0.4.3 按原版 ServeManager.sync_model_instances_state 的行为，周期同步健康运行状态，仍检查代次、所属 Worker、停止屏障、端口和文件就绪。未手工补写模型成功状态。升级前的挂起和方法预检记录单独保留，不纳入有效耗时表。47 项回归通过（26.86 秒），ruff 检查通过。

有效原始数据在 `outputs/glm53-pd-20261117/results/workers-NNN/`，包括请求配置、模型元数据/哈希、时间线、提交日志、最终实例/文件/Worker 状态、实际注册表、网关响应及资源样本。`scripts/summarize_glm53_pd.py` 可重算 summary.json。冷数据卷不等于宿主机/CDN 物理冷缓存；HF 网络延迟会影响结果。

从当前结果看，主要等待发生于 GPU 组提交前，配置读取入口数量随候选 Worker 增长。数量是入口日志数，不是实际 HTTP 请求数或函数独占耗时；本轮未做全程函数 profiling。其路径与此前容量计算反复创建 selector、读取元数据的分析一致，修改建议见 `pd-evaluating-2026-11-17.md`。应优先按单次求解复用不可变模型元数据，并保持资源/端口视图实时；本轮没有实施 Server 优化。

最终交付使用 WorkerSim 0.4.3，镜像 ID `sha256:b9214c75a7e02b67e77b20dd8e0877b23a67ee6a85cbd1a14f4ff3100f77db47`。测试验证控制面、状态与监控协议，不证明真实 GLM 引擎兼容、NCCL/Ray/KV 通信、推理性能或用量计费正确性。

2026-11-17 09:54:54 SGT 的最终验收：100 Ready / 800 GPU，64 GPU 已分配；5P3D + Router 九实例和 Model 全部 Running；注册表为 5 Prefill / 3 Decode；8 条文件记录 Ready / 100%；实例和文件 Watch 各 100 条，协议错误/过期报告均为 0。原版 Prometheus 查询到 800 条 GPU 利用率序列和非零分配显存。通过原版网关返回带 simulated 标记的响应。

镜像应用目录与安装包目录共 48 个文件哈希均与本地源码一致；最终镜像扫描 5381 个文件未发现已知测试凭证，离线包重新 docker load 后镜像 ID 一致。原始验收证据在 final-verification。环境保留为 100 Worker，公网入口 http://139.180.143.2:18080，Vultr 实例 60fc7c70-2614-43e5-a277-c457f487426f 仍运行，主机报价约 $0.132/小时。登录密码仅存于本机私有 access.json，不进入源码、镜像或证据包。
