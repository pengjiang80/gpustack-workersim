# WorkerSim 0.1.0：1000 节点验证

日期：2026-11-19。产品版本 **0.1.0**，协议目标 **GPUStack 2.3.1**。
测试不修改 GPUStack Server 或 UI，不下载真实模型权重。

## 端口与接入机制

默认同 IP：Worker API `20100–21099`，监控 `21100–22099`，模型
`40000–59999`。每节点 20 个模型端口，按需打开监听器；控制接口仍为
`127.0.0.1:20099`。配置上限从 100 提高到 1000，并增加覆盖 8000 张 B300
设备的配置样例。显式配置的旧端口池不会被自动覆盖。

每节点 6 路独立 watch、独立连接池，1000 节点为 6000 路常驻事件连接。
不设置跨节点 REST 限流，不共享池来降低 Server 压力。注册速率和默认 8 个
同时启动名额仅控制 Fleet 的创建节奏。原生通信细节见
[通信对齐说明](native-worker-communication.md)。

## 协议回归

- [原生通信版本 Linux CI](https://github.com/pengjiang80/gpustack-workersim/actions/runs/44466111995)：
  **69 passed，61.72 秒**，包含普通副本、PD、分布式、文件下载与监控用例。
- 1000 节点用例实际打开 2000 个节点监听器和 6000 路认证 watch，验证
  8000 张设备、独立 token/UUID、1000 份持久化身份、全部节点的心跳上报。
- 对第一个和最后一个节点分配模型，验证端口确实落在相应 20 端口池、
  引擎健康接口可访问、资源已分配；再验证删除回收与 1000→10→1000 在线切换。
- 同一原生通信用例在本机 Windows 通过：**1 passed，28.25 秒**，其中断言阶段
  24.72 秒。规模验收和容器部署仍推荐 Linux。
- 已补上超过 64 KiB 的 Backend 事件回归，以及部署前的
  `check_configuration=true` 校验和 Legacy 模型报告缓存路径回归。

## 镜像内源码核对

从实际发布镜像提取并核对 Worker、WorkerManager、RuntimeMetricsAggregator、
ServeManager、ModelFileManager、ClientSet、ModelInstanceClient、周期任务工具、
envs 共 9 个关键文件，SHA-256 **全部与参考 commit 一致**。
见[审计作业](https://github.com/pengjiang80/gpustack-workersim/actions/runs/44465342694)
和[机器可读记录](validation/native-source-audit.json)。

上述为回环协议测试，控制面由测试 fixture 提供，**不包含真实 scheduler**。
用例耗时不能作为 GPUStack 调度性能指标。

## 原版 Server 验收

原版 Docker 镜像 `gpustack/gpustack:v2.3.1`，拉取 digest
`sha256:2573799fd23f9973838323e93ca0c23b41ea29b8fd8a28bdb1f12ba93c5d0f6a`。
独立 GitHub Linux Runner，Server 容器与模拟器同机，`nofile=65536`，预留
全部服务端口，并将测试宿主机源端口范围显式扩大为 `10240–65535`，为原生
独立连接留足空间。指定 bootstrap password 和 disable update check。
注册速率上限 10/s，启动并发 8；心跳/状态 30 秒，runtime metrics 3 秒、
初始延迟 30 秒，配置缓存 300 秒，周期性全量查询默认关闭。

Server 使用文档支持的环境变量：`GPUSTACK_DB_POOL_SIZE=80`、
`GPUSTACK_DB_MAX_OVERFLOW=10`、`GPUSTACK_DB_POOL_TIMEOUT=120`。
分别验证 500 和 1000 节点，每节点 8 卡 B300。验收顺序是：全量注册及 watch、
Server 分页 API 确认全部 Ready、额外 35 秒观察、端点检查、2→1→0 副本场景。
前置条件失败就停止，因此不是每次运行都执行到了模型部署。

早期诊断曾出现注册请求积压和默认 30+20 数据库池耗尽；当时的模拟器还只有
双 watch，并包含额外全量轮询，因此这些结果**不用于推断原生 Worker 的节点上限**。
随后撤销了全局 REST 限流与共享连接池，按源码补齐当前通信行为，再独立验收。

### 实测结果

| 环境与源码 | 数据库预算 | 注册及 watch | 后续结果 |
|---|---|---|---|
| GitHub Runner，4 vCPU / 16 GiB，`034358f`，500 节点 | 内置 PostgreSQL，80+10 | 482.353 秒，500 / 3000 | 786.860 秒全部 Ready；完成副本场景，但累计 1880 次通信错误 |
| GitHub Runner，4 vCPU / 16 GiB，`61d9c8c`，500 节点 | 内置 PostgreSQL，80+10 | 472.531 秒，500 / 3000 | 等待全部 Ready 超时，未执行副本场景 |
| GitHub Runner，4 vCPU / 16 GiB，`61d9c8c`，1000 节点 | 内置 PostgreSQL，80+10 | 最后采样 669 / 3972 | 启动阶段 HTTP 读取超时，未完成注册 |
| Vultr Shared CPU，8 vCPU / 16 GB，`6d8fb20`，500 节点 | 外置 PostgreSQL，384+128；PG 最大 768 | 718.189 秒，500 / 3000 | 等待全部 Ready 的 API 验收超时，未执行副本场景 |

前两轮 GitHub 作业分别为
[44464421352](https://github.com/pengjiang80/gpustack-workersim/actions/runs/44464421352)
和 [44466149407](https://github.com/pengjiang80/gpustack-workersim/actions/runs/44466149407)。
Vultr 使用同一 `61d9c8c` 通信实现，后续提交只增加验收脚本和文档。
四组脱敏结果及参数见[机器可读记录](validation/scale-acceptance-0.1.0.json)。

第一轮 500 节点的 2→1→0 分别耗时 **108.596 / 28.409 / 20.474 秒**，
测试模型已删除。这里计时包含 Server 控制器、调度、模拟启动及 API 查询收敛，
不是纯 scheduler 计算耗时。由于存在 1880 次通信错误，脚本的功能成功标志
**不代表稳定性验收通过**，也不能替代当前通信实现的验收。

Vultr 运行累计记录 **130 次读取超时**。数据库检查曾发现全部 500 个节点因
`Heartbeat lost` 处于 Not Ready；06:32:25 UTC 的后续快照又显示全部 Ready。
但分页 API 未在验收窗口内观察到全部 Ready，最终抛出
`TimeoutError: Acceptance condition did not converge`。这说明存在状态恢复，
不能描述为“500 节点从未 Ready”；同样不能宣布稳定通过。

结束附近的瞬时采样：Server CPU 141.60%，内存 7.054 GiB；PG 会话有
314 个 `idle in transaction / ClientRead`、90 个 idle、1 个 active。
CPU 百分比为 Docker 多核口径，以上不是时段平均值，也不是峰值。
这轮同时改变 CPU 环境和数据库部署方式，不能将差异单独归因于连接池。

**交付结论：1000 节点的配置、端口及协议回归通过；原版 Server 的 500/1000
节点稳定性尚未通过。** 当前不承诺该配置的 Server 可稳定承载 1000 个真实
Worker，也不把最后采样到的 669 个节点当作其容量上限。

### 诊断判断与后续建议

- 先前 1000 节点日志有明确的 80+10 QueuePool 耗尽、等待 120 秒记录。
  最新 1000 节点的直接失败是 HTTP 读取超时，其日志没有同样的 QueuePool 错误；
  不能据此断言所有失败都由连接池造成。
- 扩大至 512 个应用连接后，500 节点仍未完成稳定验收。因此继续加连接数
  不能作为已验证的解决方案。文档支持的参数及默认值见
  [GPUStack 数据库环境变量](https://docs.gpustack.ai/2.2/environment-variables/#database-configuration)。
- 下一步应在 Server 侧采集请求分位延迟、连接池等待、事务存活时间，以及
  心跳收到时间到落库时间的差值。大量 `idle in transaction` 是排查会话生命周期
  的线索，尚不能单凭快照确定连接泄漏、数据库锁或根因。
- 重点检查心跳/状态批量写入、认证查询与缓存失效、watch 初始化期间的会话
  释放。根据采样再决定缩短事务、拆分批次或隔离心跳写入等修改；本次未修改
  原版 Server，也未延长心跳周期或删除订阅来绕过负载。
- 每次只改变一个 Server 参数或补丁，使用相同节点配置复测。达到稳定 500
  后再做 1000，随后单独测试大量副本、PD 与分布式调度。

验收脚本现会在失败时保留执行阶段、最后状态分布和错误类型，避免只有
`failed` 而缺少失败位置。此处历史记录未事后改写为通过。

### 环境清理

复用了 2026-11-16 创建、此前未释放的 `gpustack-workersim-231-lab`，本轮没有
另建 Vultr 主机。结果及脱敏日志已下载；2026-11-19 06:38 UTC 再次核对：
删除实例查询返回 **404**，账户实例列表为 **0**。旧测试配置另有本地私有备份。

## 边界

每节点 20 个端口不代表支持任意多副本或所有复杂 PD 角色同时驻留。
增加单节点端口数时，应同步预留脚本参数，并减少同 IP 节点数量或拆分
IP/主机。1000 节点容量与重型模型、PD 组合搜索的 scheduler 性能应分别验证。
本次不是长期稳定性、1000 模型并发部署或推理性能测试。
Benchmark/Cache Service 保留订阅压力，尚未模拟执行引擎。
