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
分别验证 500 和 1000 节点，每节点 8 卡 B300，并执行小规模 2→1→0 副本场景。

早期诊断曾出现注册请求积压和默认 30+20 数据库池耗尽；当时的模拟器还只有
双 watch，并包含额外全量轮询，因此这些结果**不用于推断原生 Worker 的节点上限**。
随后撤销了全局 REST 限流与共享连接池，按源码补齐当前通信行为，再独立验收。

最终记录待验收完成后填写。

## 边界

每节点 20 个端口不代表支持任意多副本或所有复杂 PD 角色同时驻留。
增加单节点端口数时，应同步预留脚本参数，并减少同 IP 节点数量或拆分
IP/主机。1000 节点容量与重型模型、PD 组合搜索的 scheduler 性能应分别验证。
本次不是长期稳定性、1000 模型并发部署或推理性能测试。
Benchmark/Cache Service 保留订阅压力，尚未模拟执行引擎。
