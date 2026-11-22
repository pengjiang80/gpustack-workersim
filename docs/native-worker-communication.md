# 原生 Worker 通信对齐

参考协议源码 commit：`458030e88718ce47bd31f7cdee9cbc8994a6760b`。
验收还会从实际 Docker 镜像提取 Worker 源码并记录 SHA-256，避免只凭版本号
认定镜像内实现与本地参考源码一致。

| 行为 | 对应源码 | WorkerSim 默认行为 |
|---|---|---|
| 版本检查 | `worker/worker.py`、`worker_manager.py` | 每节点 GET `/version`，5 秒超时；不一致或失败只记录日志，不阻止启动 |
| 注册和身份确认 | `Worker._register` | POST `/workers`，初始系统信息不含 GPU；使用返回的节点凭据 GET `/workers?me=true` |
| 注册重试 | `Worker._register` | 最多 10 次，固定间隔 3 秒 |
| 心跳 | `Worker._heartbeat`、`envs` | POST `/worker-heartbeat`，启动后立即执行，完成后等待 30 秒 |
| 节点状态 | `sync_worker_status`、`utils/task.py` | POST `/worker-status`，启动后立即执行，完成后等待 30 秒；首次状态包含模拟 GPU |
| 持续订阅 | Worker、ServeManager、ModelFileManager 等 | 每节点 6 路独立 watch：inference-backends、models、model-instances、benchmarks、cache-service-instances、model-files |
| watch 参数/重连 | generated clients、各 manager | `watch=true`；连接 10 秒、行读取 45 秒；异常后固定等 5 秒再连；重连清空缓存并接受服务端重放 |
| 模型状态同步 | ServeManager、generated client cache | 默认每 3 秒读本地 watch 缓存；断连回退到 HTTP，需回收时 GET `page=-1` 确认；默认不开周期性全量查询 |
| Backend 初始缓存 | InferenceBackendManager | 每节点启动时 GET `/inference-backends/all` |
| Provider catalog | CacheProviderManager | 每节点 GET `/cache-providers?page=0`，立即执行，此后每 60 秒 |
| Runtime metrics | RuntimeMetricsAggregator | 启动延迟 30 秒，此后每 3 秒 GET `/workers/{id}`（有可采集端点时一轮两次）；实例/模型列表读 watch 缓存；首次探测到引擎版本时对 legacy 实例GET + PUT `api_detected_backend_version` |
| 孤儿负载清理 | WorkloadCleaner | 启动延迟 15 秒，此后每 120 秒；节点存在模型负载时 GET `/model-instances?page=-1`（不走缓存） |
| 错误实例重启 | ServeManager.watch_model_instances | 每 10 秒处理 UPDATED 为 ERROR 的本地实例；指数退避 10→300 秒；GET 实例确认、按需 GET 模型，`restart_on_error` 时 PUT `restart_count+1`、`state=scheduled` |
| 状态拓扑 | collector、detectors/runtime | 设备 `topology_hints`、Worker `topology_facts`、`kv_transfer_ifname`，Ascend 设备 RoCE `network` |
| Metrics 配置 | RuntimeMetricsAggregator | GET `/metrics/config`，2 秒超时，缓存 300 秒，失败回退内置配置；返回的配置决定引擎原生指标到 `gpustack:*` 的映射、缩放和类型 |

每个节点保留独立认证、连接池和任务，不设置跨节点 REST 并发上限，不共享
连接池来降低 Server 压力。Fleet 的 `registration_rate` 和
`registration_concurrency` 仅控制创建节点的启动节奏，已经启动的节点独立
发送请求。测试验收不拉长心跳、状态或 runtime metrics 周期。

`simulation.reconcile_interval` 对应原生可选的权威状态对账周期，默认 0；
单元测试可以显式缩短周期以加速故障用例。`model_state_interval` 默认 3。
对齐的是请求路径、参数、数据语义、周期、缓存与重连行为；GPU 数据、文件
下载、模型启动和推理结果仍为合成数据。实现使用异步任务代替真实 Worker
的线程/进程，不把模拟器 CPU 占用作为真实 Worker 的 CPU 性能指标。

引擎以各自原生指标名暴露（vLLM、SGLang、MindIE、PD Router、SGLang 网关），Worker 端聚合器复用原生的来源判定、标签、按版本选择映射、单位缩放、30 秒快照过期和采集状态逻辑；`scripts/native_contract.py` 会把同一份指标同时交给原生聚合器和 WorkerSim，逐样本比较结果。

目前部署执行覆盖模型副本、PD 与分布式模型场景。Benchmark 和 Cache Service
订阅用于保留原生通信压力与缓存语义，尚未模拟它们的执行引擎。

大规模验收遇到 Server 压力时，使用 GPUStack 已提供的
`GPUSTACK_DB_POOL_SIZE`、`GPUSTACK_DB_MAX_OVERFLOW`、
`GPUSTACK_DB_POOL_TIMEOUT` 参数调整数据库池，并确保总连接预算不超过
PostgreSQL 的可用连接数；不在 WorkerSim 内减少请求来规避错误。参数来源见
[GPUStack 环境变量文档](https://docs.gpustack.ai/2.2/environment-variables/)。
