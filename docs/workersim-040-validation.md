# WorkerSim 0.4.0 实现与验收记录

日期：2026-11-17。目标为原版 GPUStack 2.3.1，Server/UI 没有修改。

后续补充：GLM-5.3 严格验收发现 0.4.0 未实现 Router `/workers` 成员管理接口。本文的 PD Running 结果不证明成员回读成功；此前这项验收标准不完整。0.4.1 已补齐此协议，重新以 Model Running、九个实例 Running 和实际 5P3D 注册表一致为成功条件，见 `glm53-pd-benchmark-2026-11-17.md`。

## 本版更新

- 移除单节点无角色限制，按原版分配处理 Prefill、Decode、Router，以及多 Worker 主从。
- 覆盖 legacy delegated / initialize_later / run_first，managed rank 与 SGLang Engine/SMG 组件。逐 Worker 上报 preparation、workload 注册和 observations，不代替 Server 做调度或宣布 managed 全组成功。
- 支持 stop_requested、legacy_stop 的 token 确认、restart_count 与组件 attempt；旧代次被拒绝时不报告就绪。运行失败可由回环控制接口注入，并交回 Server 的正常重启流程恢复。
- 同时 watch model-instances 和 model-files，重连带退避，完整分页快照补偿遗漏删除；快照进行期间若有新事件，避免覆盖较新的事件状态。
- 修复文件页一直 downloading：使用对应 Worker 凭证上报分段进度、虚拟路径、合成大小，最后 ready/100%。也覆盖文件记录晚于实例出现的情况，文件未就绪时不提前启动。
- 注册身份和凭证持久化；恢复已运行代次时重建模拟端点，避免无必要地将实例退回初始化。凭证不进入源码包或镜像。
- 节点资源使用与 Server 的实际实例 claim 关联；不写回已弃用的 allocated 字段。Headless 从节点不重复生成 token 指标；SMG/PD Router 只生成路由指标，PD 另有合成传输、队列和成员健康数据。
- 修复原版网关 registry 标识解析；按原版 Worker 规则接受节点 Token 或集群注册 Token，支持 Bearer 和 X-API-Key，拒绝错误凭证。
- 独立 IP 复用 40000–40063；同 IP 为普通实例分配物理端口块，向调度器声明公共物理端口池，避免跨 Worker 无共同范围而被拒绝。分布式共同端口由各 Worker 独立认证代理承接，headless 成员不重复绑定同一宿主机端口。
- 增加逻辑端口预留、旧 legacy 重复端口恢复、有限的启动端口失败重试和实例阶段诊断。Managed 已发布拓扑保持原版的代次和停止屏障约束。

PD 端口目录取自原版镜像 API，包括 vLLM NIXL/Mooncake、SGLang Mooncake/NIXL、Ascend Mooncake 和 custom 的端口声明。声明可解析不代表这些引擎和硬件已全部做过集成验收。纯 vLLM 拓扑 helper 按目标 commit 固定，附 Apache-2.0 来源说明和许可证。

## 验证范围

本地完整回归：**39 passed**；ruff check 与 format 通过。覆盖 50/100 Worker、双 watch、重连、删除补偿、文件创建延迟、错误凭证、代次冲突、端口释放与恢复、主从启动顺序、rank/component 和故障注入。回环夹具不是 GPUStack 调度器。

云端运行原版 `gpustack/gpustack:v2.3.1` 与 WorkerSim，同一台 8 vCPU/16 GiB 的 Vultr Shared CPU。主 fleet 为 100 × 8 B300，共 800 模拟 GPU。另短暂增加一个独立双 Worker fleet 验证同 IP，完成后清理。没有创建第二台收费主机。

| 场景 | 配置/结果 |
|---|---|
| 原用户 DeepSeek | 保留 5 Prefill、3 Decode、1 Router，9 个成员全部 Running |
| vLLM PD fixture | 2P1D + Router，全部 Running |
| SGLang PD fixture | 1P1D + Router，全部 Running |
| 跨 Worker MP | 两节点 TP2，主从 Running |
| 跨 Worker PP | 两节点 TP1/PP2，主从 Running |
| Ray 控制面 | 两节点 TP2，主从 Running；不启动真实 Ray |
| External-LB | 两节点 DP2，多 rank 上报与 API 正常 |
| Hybrid-LB | 两节点共享 API 组件，Running |
| Multi-port External-LB | 两节点各两卡，DP4、本地 DP2，Running |
| SGLang DP/DPA | 两节点 Engine + 主节点 SMG，Running |
| 默认同 IP | 两个独立身份、API 21100/21101，同为 172.17.0.1，跨节点 MP Running |
| 网关 | 12 个被选测试部署通过原版 /v1/chat/completions，返回 200 与 simulated 标识 |
| 文件与事件 | 59 条文件记录 Ready；主 fleet 100 条实例 watch、100 条文件 watch 均连接 |
| 故障与重启 | Hybrid 注入失败，Server 接收到 Error，原端点关闭，原版模型重启创建替代实例 |

验收中遇到并修复的边界均保留说明：Worker 身份不能读取管理端 PD 目录，改为固定目标版本的无凭证端口契约；多端口 DP 配置必须满足原版每节点本地 DP>=2；创建模型未启用服务路由会得到 Model not found，脚本已显式创建路由；Higress 需要集群凭证；同 IP 需要共同端口池；旧 legacy 端口可能重复，需要重新分配。

一次进程升级会同时重启其中的模拟节点。100 节点按每秒 2 个重新注册约需 50 秒，高编号节点期间会暂时不可用；原版控制器可能让相关实例重新评估。升级脚本现会等待全部节点双 watch 恢复再启动后续用例。这段时间不能当成纯调度性能测试。

## 初始化参数

按[原版 2.3 CLI 文档](https://docs.gpustack.ai/2.3/cli-reference/start/)配置 bootstrap_password 与 --disable-update-check。独立原版容器实测：指定密码登录成功，require_password_change=false。密码保存在 mode-0600 私有配置中，不出现在命令行或日志。临时验证容器和数据卷已清理。

## 限制与复现

这些测试验证调度、控制器、Worker 协议、API 与监控展示。合成吞吐/延迟/KV 数据不证明真实推理性能、KV 正确性、NCCL/Ray 网络、驱动、镜像、模型权重、缓存服务进程或 GPU 故障行为。虚拟文件大小和通用本地 config fixture 为合成数据。

HF 模型元数据由原版 Server 真实读取；压测纯控制面时可使用 /models/workersim-llama fixture 隔离网络因素。本次 HF evaluating 的长耗时是原版容量计算与串行队列的问题，见 pd-evaluating-2026-11-17.md；没有通过修改 Server 或降低用户 PD 配置规避。

原始证据、截图、原版指标查询与 SHA-256 清单位于 outputs/cloud-040 和 outputs/pd-evaluating-20261117，交付包位于 dist。最终清理、缩容结果和镜像校验以随包的验收 JSON 与 manifest 为准。


## 最终回收和指标复验

4→1→0→1 实测收敛，本地分配记录在零副本阶段归零。4→1 为 18.397 秒，1→0 为 30.116 秒，0→1 为 5.168 秒；包含原版控制器与停止/回收等待，不能作为纯调度时延。Hybrid 故障后的替代实例已 Running。

清理同 IP 专项的两个额外 Worker 后，基线恢复为 100 Ready / 800 GPU，44 个 ModelInstance 记录全部 Running。主 fleet 双 watch 各 100 条，协议错误和过期报告计数均为 0。此前包括同 IP 专项在内的 12 个部署网关调用全部返回 200；专项清理后，对剩余 11 个被选部署再次通过网关验证。原版普通模型面板 18 条核心查询、PD/分布式 7 条查询均有非 NaN 结果，Server allocation 与实例 claim 对账差异为 0。

最终镜像 ID：`sha256:ed4f117849fbe371b0aa8eea62d7e89fa36a957de8b9ad7c342d816bfc0a9b29`。


临时 Vultr 实例已删除并复核 HTTP 404，见 resource-release.json。源码与镜像已校验一致，镜像中已扫描 5414 个文件，未发现已知测试凭证。完整私有复现配置不进入交付包。
