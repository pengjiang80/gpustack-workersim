# 100 节点 B300 访问环境

2026-11-16 按用户要求创建，2026-11-17 完成验证后已删除。Vultr API 已再次确认实例不存在（HTTP 404）。以下保留历史访问与运行记录；原地址已失效。

- 公网地址：<http://139.180.143.2:18080/>。
- 本机 SSH 隧道地址：<http://127.0.0.1:18880/>。
- 用户名：`admin`。初始密码保存在本机忽略目录 `.workersim/b300-initial-password.txt`，只授予当前 Windows 用户读取和写入权限。首次登录按原版 UI 提示修改密码；该文件不跟踪后续密码修改。
- Vultr 实例：`69d36f46-81d6-4b41-a81c-d576c8db6b91`，新加坡 Shared CPU，8 vCPU / 16 GiB。
- 创建时 API 标价：0.132 美元/小时，持续 24 小时约 3.17 美元。主机现已删除，不再保留运行。

同一台 CPU 主机运行两个主要容器：原版 `gpustack/gpustack:v2.3.1` 和 `gpustack-workersim:0.3.0`。Nginx 将公网 IP 的 18080 端口代理到原版 Server 的回环端口，支持流式响应和 WebSocket。公网入口是 HTTP，供临时测试使用。模拟 Worker 和 exporter 端口只对 Docker 网桥来源开放。

100 个 Worker，每节点 8 张 B300 模拟卡，共 800 卡；100 个独立 UUID 和独立 watch 连接。示例模型 `workersim-ui-proof` 曾完成 100/100 就绪验证，日常浏览环境保留 10 个副本。硬件容量和监控数据为合成值，不执行真实推理。

2026-11-17 更新到 0.3.0：100 个独立可达 IP 为 `10.250.0.1` 至 `10.250.0.100`，绑定在宿主机 `workersim0` dummy 网卡，systemd 保证重启后先于 Docker 恢复。普通实例恢复时同步自身地址快照。当前已有的示例模型和用户创建的 `deepseek-v4.1-flash-1017` 均予以保留。

模型实例 exporter 已补齐模拟请求数、Token 吞吐、TTFT/TPOT、端到端延迟、并发和缓存占用等指标。已通过原版 GPUStack 模型仪表盘中的 18 条核心查询及单实例 Token 吞吐筛选。数据为自动生成的合成流量，带 `workersim="true"`，无需真实推理请求，也不代表 DeepSeek 的实测性能。

页面入口：Workers 查看节点和资源；Deployments 查看模拟模型、实例及调度位置；监控按钮打开原版内置 Grafana。

本机隧道断开后，可在 PowerShell 中运行下面的命令并保持终端开启；公网地址不依赖该隧道：

```powershell
ssh -N -L 127.0.0.1:18880:127.0.0.1:18080 -i "$env:USERPROFILE/.ssh/id_ed25519" -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -o UserKnownHostsFile="C:/Users/Peng/Codex/GPUStack_WorkerSim/.workersim/known_hosts-b300" root@139.180.143.2
```

此前运行状态和监控证据在 `outputs/cloud-b300-100/`，0.3.0 更新证据在 `outputs/cloud-030/`。本地协议测试现为 24 项通过，涵盖 100 节点独立 IP 上报、路由冲突保护、指标计数器/直方图语义和模型删除清理。官方 2.3.1 镜像内 Prometheus parser 和注册/workload Schema 校验通过。

首次 100 副本启动期间触及模拟器容器的默认文件句柄上限，出现 `Too many open files` 和累计 9 次报告错误，重试后副本仍达到 100/100。随后为容器设置 `nofile=65536:65536`；恢复已部署 100 副本时又触发默认 20 秒请求超时，导致容器重启。最终采用每秒 2 个节点注册、120 秒请求超时和 30 秒快照核对间隔，并将日常示例缩至 10 副本，保留原有 Worker 身份复验。此调整属于部署配置，没有修改 Server/UI；最终结果与早期快照分开保存，不能将早期失败描述为无错误通过。

最终复验：100 Ready、100 个唯一身份、100 条 watch、10 个运行实例、累计报告错误为 0、当前容器重启次数为 0。注册恢复收敛 51.523 秒，受每秒 2 节点的节流影响，不代表纯 Server 吞吐。

释放时先保存需要的数据，再运行 `python scripts/vultr_lab.py release`；脚本只会操作当前台账记录且标签匹配的实例。Vultr 停机仍计费，释放需要删除实例。删除会同时移除 Server 数据和示例模型。


## 0.4.0 最终验收与释放

本轮完成 PD、TP/PP、Ray、External/Hybrid/Multi-port External-LB、SGLang、同 IP 跨节点、文件 Ready、双 watch、故障重启和 4→1→0→1 扩缩容验证。39 项本地回归通过，基线恢复到 100 Ready / 800 GPU；57 个 GPU 有 Server 分配记录，对账差异为零。原用户模型保留 5P3D+Router 并全部 Running。详见 workersim-040-validation.md 与 pd-evaluating-2026-11-17.md。

源码、最终 Docker 镜像、报告与无凭证验收证据已保存本地。完整复现配置另存于忽略目录 .workersim/private-040，仅当前 Windows 用户可读，不随交付包分发。Vultr 实例 69d36f46-81d6-4b41-a81c-d576c8db6b91 已删除，复核 HTTP 404。历史访问地址和旧登录凭证不再对应可用环境。
