# WorkerSim 0.1.0 验收记录

日期：2026-11-16，Asia/Singapore。独立工具，原版 GPUStack Server/UI 无源码改动。

## 环境和交付

- Vultr 新加坡 Shared CPU，`vhp-8c-16gb-intel`，8 vCPU / 16 GiB，Ubuntu 24.04。
- Server：`gpustack/gpustack:v2.3.1`，`/version` 返回 `v2.3.1 (458030e)`。
- Server digest：`sha256:2573799fd23f9973838323e93ca0c23b41ea29b8fd8a28bdb1f12ba93c5d0f6a`。
- 最终 WorkerSim image ID：`sha256:800f7c3306be50f1216f3fa35e4db03eddb1d1ab2e1da0eaefd0c5d4bb32f1a0`。
- 独立镜像 `gpustack-workersim:0.1.0`，Linux amd64，非 root 用户，无 GPU/特权/Docker socket 依赖。
- 导出的镜像已用 `docker load` 实际导入，并验证 CLI 返回版本 `0.1.0`。
- 当前交付包的 SHA-256 和文件大小见 `dist/manifest.json`。

Server 和 WorkerSim 在同一 CPU 主机上运行。本次没有测真实推理吞吐，也没有做独立 Server 瓶颈定位。资源采样只是瞬时观察，不能视为峰值、均值或容量上限。

## 已完成的验证

| 检查 | 结果 |
|---|---|
| 本地回环协议/资源释放测试 | 14 项通过 |
| 原版 2.3.1 注册和 workload Schema | 通过官方镜像内原始模型校验 |
| Prometheus exposition 格式 | 通过原版镜像内 parser 校验 |
| 2 Worker / 16 GPU | Ready，模型 `2→5→1→0` 通过 |
| 10 Worker / 80 GPU | 10 个独立身份和 watch，模型 `10→20→5→0` 通过 |
| 50 Worker / 400 GPU | 50 Ready、50 个唯一 Worker UUID、50 个独立 watch |
| 50 Worker 重启恢复 | 注册后仍为 50 个节点和 50 个唯一身份，无重复节点 |
| 50 Worker 模型扩缩容 | `10→50→100→25→0` 两轮通过 |
| 原版 Prometheus 抓取 | 400 组 GPU 利用率时序，有历史样本 |
| 模型删除和资源释放 | 最终模型/实例为零，已分配 GPU 显存指标总和归零 |
| 原版 Workers UI | 模拟节点显示 Ready，并显示模拟标签和资源状态 |
| 原版 Deployments UI | 验收模型 2/2 就绪，两个实例均为 Running |
| 原版 Grafana | 集群汇总显示 50 Worker / 400 GPU，单节点利用率和显存曲线可见 |

25 个节点模拟 H100、25 个节点模拟 A800，每节点 8 卡。显存、利用率、温度和功耗都是模拟值；分配位置、实例创建/删除以及副本收敛来自未修改的 Server。

10→50 节点阶段包含 10 个已有节点恢复和 40 个新节点注册，就绪收敛耗时 14.511 秒。最终镜像下 50 节点重启收敛耗时 10.618 秒。这些数据受配置中每秒 5 个节点的注册节流影响，不是 Server 最大注册吞吐量。

最终镜像模型收敛结果：

| 目标副本 | 等待收敛时间（秒） |
|---:|---:|
| 10 | 11.956 |
| 50 | 40.163 |
| 100 | 54.608 |
| 25 | 1.871 |
| 0 | 1.122 |

计时从 API 创建/更新后开始等待，包括调度、模拟启动和控制器汇总延迟，不代表纯调度耗时。模型是虚拟本地 Llama 配置，明确设置 VRAM/RAM 需求，TP=1、GPU memory utilization=0.9。

最终模拟器状态：50 个 watch 连接、零运行实例、零真正协议错误；13 次实例在上报前已被删除的正常竞争已单独计数并清理。没有把这些删除竞争当成部署成功或静默忽略真实 API 错误。

首次云端小规模用例因 UFW 阻止 Docker 网桥回连而未收敛。只对网桥来源放行 Worker/监控/模拟引擎端口后重跑成功；未为此关闭防火墙、修改 Server 或禁用健康检查。

## 原始证据

原始 JSON 和快照在 `outputs/cloud-20261116/`，另以 evidence ZIP 随交付物提供：

- `server-version.json`、`fleet-10.json`、`fleet-50-initial.json`、`fleet-50.json`。
- `scenario-small.json`、`scenario-10.json`、`scenario-50.json`、`scenario-50-final.json`。
- `native-contract.json`、`simulator-status.json`、`final-inspection.txt`。
- `prometheus-gpu-count.json`、`prometheus-gpu-memory.json`、`prometheus-gpu-history.json`。
- `container-resource-sample.json` 为单次容器资源采样，不是性能基准。
- `ui-inspection.json` 记录 UI 验收模型 2/2 就绪和两个 Running 实例；`ui-proof-model.json` 是创建请求返回的初始状态。
- `post-ui-cleanup.json` 记录视觉验收模型删除后 50 个健康 watch、零模型、零实例、零协议错误；`prometheus-gpu-memory.json` 确认分配显存归零。
- `screenshots/workers-ready.jpg`、`model-ready.jpg`、`instances-running.jpg`、`monitor-summary.jpg`、`monitor-curves.jpg` 保存原版页面截图。

## UI 验收

用户完成原版 UI 的首次密码修改后，已在浏览器实际检查 Workers、Deployments 和内置 Grafana 页面。`workersim-ui-proof` 只用于保存部署截图，验收完成后已删除。Grafana 的集群汇总为 50 Worker、400 GPU；筛选 `sim-a800-007` 后，可见对应 8 卡及 GPU 利用率、显存和系统内存历史曲线。页面中的所有硬件和运行数据均为合成值。

截图与 JSON 结果保存在 evidence ZIP 中，源码包和 Docker 镜像不包含登录密码、API Key 或 Worker 身份数据。

## 云资源清理

Vultr 临时实例 `68e19e0c-4c5d-44a8-984f-caff5ce89806` 已删除。释放脚本在 DELETE 后再次 GET，确认返回 HTTP 404，台账状态已记为 `released`，详见 `resource-release.json`。本次测试未保留云主机，因此原测试页面已停止服务；后续可用交付镜像重新部署。

## 范围边界

首版验收覆盖单节点普通部署和模型副本扩缩容。managed workload 的基础上报契约有校验，但多节点 TP/PP、PD、External/Hybrid-LB 和 SGLang SMG 的完整执行协议未验收。不把固定聊天响应当作真实模型推理或推理性能测试。
