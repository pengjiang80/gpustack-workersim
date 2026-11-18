# GPUStack 2.3.1 PD evaluating 性能诊断与修改建议

记录时间：2026-11-17（Asia/Singapore）。本报告分析原版 Server 的调度耗时；WorkerSim 0.4.0 的兼容更新另见 `workersim-040-validation.md`。没有替换或修改 Server/UI 源码。

## 结论

现场的长时间 evaluating 有明确的 Server 路径原因：PD 容量计算按角色、Worker 和假设槽位重复建立 selector；selector 反复读取 Hugging Face 配置、解析 revision 和估算资源。调度消费循环又串行等待整个 PD 组完成，导致普通模型扩容排在其后。

这次不是主机 CPU 或内存耗尽，也不是永久死锁。首次 5P3D 的八个 GPU 成员从创建到调度提交约 **18 分 37 秒**；提交后普通模型恢复至 20/20。随后 PD 成员报错是旧 WorkerSim 0.3.0 主动拒绝角色部署造成的另一个问题。0.4.0 已移除这个限制并补齐角色、从节点和 workload 协议，不能因此宣称原版 Server 的调度性能问题已修复。

## 环境与时间线

- 原版镜像：`gpustack/gpustack:v2.3.1`。
- 镜像 digest：`sha256:2573799fd23f9973838323e93ca0c23b41ea29b8fd8a28bdb1f12ba93c5d0f6a`。
- 源码 commit：`458030e88718ce47bd31f7cdee9cbc8994a6760b`。
- 单台 Vultr Shared CPU，8 vCPU / 16 GiB；Server 和模拟器同机。
- 100 Worker × 8 B300，GPU 数据为合成值；HF 元数据查询、Server 求解、数据库提交和 watch 都是真实执行。
- 用户模型：DeepSeek-V4.1-Flash-1017，5 Prefill / 3 Decode / 1 Router。普通模型从 10 扩到 20。

| 时间（11 月 17 日） | 观察 |
|---|---|
| 00:14:25 | 八个 PD GPU 成员创建，进入 analyzing |
| 00:16:11–12 | 普通模型新增十个副本，等待 evaluating |
| 00:23:34 | 日志出现第一条 group placed；此时没有对应 scheduled 提交记录 |
| 00:32:58 | 再次出现 group placed |
| 00:33:02.840 | 八个 PD GPU 成员实际提交到 Worker 1–8 |
| 00:33:03 起 | 旧模拟器返回不支持角色部署；八个成员进入 Error |
| 00:39:49 快照 | 普通模型 20/20，PD Error，100 Worker Ready |

placed 日志表示求解器产生了方案，不能等同于数据库已经完成调度。第一次方案没有提交的具体 fence/重试原因在现有 INFO 日志中没有保留下来，不作唯一归因。

## 证据与调用路径

原版 `scheduler/scheduler.py::_schedule_cycle`（约 385 行）从队列取一个实例后，直接 `await _schedule_one(item)`，完成后才消费下一项。`_try_schedule_group`（约 397 行）让首个成员求解整个未放置 PD 组。普通扩容因此会受到前面长组任务的队首阻塞。

容量链路为：

```text
_schedule_cycle -> _schedule_one -> _try_schedule_group -> schedule_group
  -> GroupCapacity._count（逐 Worker / 角色）
  -> count_offer_slots（逐个假设放置槽位）
  -> 新建 VLLMResourceFitSelector -> select_candidates
  -> _init_model_parameters / estimate_model_vram
  -> get_pretrained_config_with_workers -> get_pretrained_config
  -> transformers / HF resolve_revision -> HfApi.model_info -> HTTPS
```

定位入口：`scheduler/group_capacity.py::_count`（约 206 行，循环约 240 行）、`scheduler/offer_slot.py::count_offer_slots`（约 100 行）、`policies/candidate_selectors/vllm_resource_fit_selector.py::select_candidates`（约 304 行）、`scheduler/calculator.py::get_pretrained_config_with_workers`（约 711 行）。

已核对运行镜像内文件与导出源码 SHA-256 一致：

| 文件 | SHA-256 |
|---|---|
| scheduler/group_capacity.py | `01566c20a2d5d7ca37a04c7e67127f83ef0506e273def4935bc28cf38228f64a` |
| scheduler/scheduler.py | `bdf5bfc8ccd887a9aa306cedf487598e8c1d63c2b595b9c85616434af705d70b` |

首次日志的约十分钟窗口中，读取 HF 配置的开始日志 1574 条，DeepSeek 资源计算日志 1644 条；开始间隔中位数约 0.263 秒。这是日志事件的间隔，不能直接当成函数独占耗时或网络 RTT。

资源快照中 Server 约 15.9% CPU、5.18 GiB 内存，主机仍有约 9.6 GiB 可用内存，Swap 为零。初始资源分配校验对 100 Worker 的 Server allocation 与实例 claim 逐项比对，差异为零；只有十个 GPU 当时已绑定普通实例，其余大多空闲，不能据此说 WorkerSim 没有上报 allocation。2.3.1 由 Server 根据实例绑定计算 allocation，Worker 不应写回已弃用的 allocated 字段。

## 活跃期采样补充

首次 30 秒采样落在长评估结束后，主要是空闲线程，不用于热点归因。升级模拟器后，通过原版重启 API 保留 5P3D 配置重试，另采到正在评估的 20 秒窗口。使用独立主机 venv 中的 py-spy 0.4.3；没有修改 Server 库。

活跃栈直接显示 `get_pretrained_config -> PreTrainedConfig.get_config_dict -> resolve_revision -> HfApi.model_info -> SSLSocket.read`。样本包含 idle 线程，50 Hz，35 次采样错误。跨线程的包含式样本时间：get_pretrained_config 18.80 秒、resolve_revision 18.62 秒、SSL read 18.38 秒。主线程有效样本 19.28 秒，其中 EpollSelector.select 15.26 秒。

这证实该窗口大量等待发生在重复的 HF revision/config 元数据读取上。上述分类相互包含，跨线程时间不能相加当作实际耗时，也不是 CPU 百分比，不能外推为整段 18 分钟的精确占比。剩余的纯容量组合计算、序列化、锁等待和重试成本仍应单独量化。

原始证据在 `outputs/pd-evaluating-20261117/`：`group-timeline.txt`、`log-cadence.json`、`instances-final.json`、`final-state.json`、`source-hashes.json`、`resource-accounting-check.json`、`pd-active-stack.txt`、`pd-active-profile.json`、`pd-active-profile-summary.json`。Speedscope 文件可离线导入查看。

## 建议修改顺序

### P0：在一次求解内复用不可变模型元数据

新增 PreparedModelContext，首次解析 HF revision/config、权重静态尺寸和 role-effective 参数后，在整个组求解及其 selector 间共享。键包含 source、明确的 revision/解析后的 commit、角色有效参数、量化设置、影响解析的 env 和配置指纹；并发请求合并为一次加载。跨求解缓存设 TTL/失效条件，不能让浮动 main revision 永久陈旧。

继续为每个假设槽位重新计算 ResourceView、already_placed、显存/RAM、端口和缓存服务预留。不要直接缓存整个 selector，也不要只按 Worker ID 缓存容量，否则会把前一个假设放置的空闲量错误用于下一次放置。

先实现这一项，通常比增加 CPU/RAM 更切中本次问题。缓存收益需实测，当前没有修改 Server 或测得加速倍数。

### P1：让不同模型的规划有界并行

长 PD 组规划不应占住唯一消费循环。采用每模型/组互斥、相同组请求合并、有限个跨模型规划任务；普通短任务可以继续推进。提交阶段仍需在事务内重新检查模型配置、绑定身份、最新资源和端口预算，保持 PD 全部提交或全部不提交；必须防止多个并发规划使用同一份空闲资源而超配。

规划不要长时间持有数据库行锁。对 fence 拒绝增加明确的原因分类、计数和重试间隔；本次两条 placed 日志之间的重复求解应有可观测解释。

### P1：限制单轮工作量并上报进度

记录排队时间、元数据时间、每角色 Worker i/N、容量尝试数、候选数、提交/fence 时间及重试原因。设置求解 deadline、I/O timeout、取消标志和配置 epoch。取消 asyncio.to_thread 的等待不等于底层 HTTPS 线程已停止，必须配合底层超时与过期结果丢弃，避免后台请求继续堆积。

超时应给出可恢复诊断并退避重试，避免所有成员只显示相同的 evaluating。求解中再次扩缩容时合并旧请求，禁止旧方案覆盖新配置。

### P2：精确约束下的容量复用

对硬件、可用资源、端口、cache reservation、role-effective 配置和 already_placed 均相同的节点/状态复用容量结果，或先进行可证明的上界剪枝。异构卡、混合显存、不同端口占用和故障状态必须分开；不能因为节点名称相似就批量假设容量相同。

另建议审查 queue.task_done 是否应放入 finally，以免异常路径破坏 queue.join；本次没有证据表明这就是 evaluating 的原因。

## Server 修改后的回归方案

在 2 / 10 / 50 / 100 Worker 上分别运行 PD 1P1D、5P3D，覆盖本地配置与 HF 冷/热元数据、同构与异构、跨节点 TP/PP、端口紧张、共享缓存预留。每组并发执行普通模型 10→20 扩容。

统计 metadata 请求数、求解次数、排队/规划/提交 P50/P95、CPU/RAM 和 fence 原因；对比同配置原版基线。P0 的单轮元数据加载次数应随不同 source/revision/role 有效配置数量增长，不应再随 Worker × slot 重复增长。

同时校验无显存/RAM/端口超配、PD 原子提交、停止确认、旧 generation 拒绝、删除和扩缩容恢复。WorkerSim 可验证这些控制面机制；其合成吞吐、延迟和 KV 传输指标不能证明真实推理或跨机通信性能。
