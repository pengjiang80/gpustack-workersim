# demo2 升级与镜像仓库迁移

日期：2026-11-20（Asia/Singapore）。访问入口仍为
`http://demo2.gpustack.cn:8080`，Server 保持原有企业版 `v2.3.1-ent2`，未修改或重启。

## 镜像与发布

Docker Hub 现为 [`gpustack/workersim`](https://hub.docker.com/r/gpustack/workersim)。
GitHub 仓库和 Python 包名仍为 `gpustack-workersim`。原 Docker Hub 仓库已删除，
本次将原始 `v0.1.1-gpustack2.3.1` 标签的源码重新发布到新地址，没有移动 Git 标签。

以下三个标签已通过匿名 Registry 查询，指向同一 Linux amd64 构建：

```text
gpustack/workersim:0.1.1
gpustack/workersim:0.1.1-gpustack2.3.1
gpustack/workersim:latest
```

部署固定为 `gpustack/workersim@sha256:e1008a8180440c8108e99cd207a1c7f1beebc92c24d3cf8b2ac8ff219f09a948`。
OCI source revision 为 `44ee790112be53053382ee1108514f5eeb17b358`，产品版本 0.1.1，
协议目标 2.3.1。企业版配置保留显式 `allow_version_mismatch: true`，结论限于本环境。

GitHub Actions 的 `DOCKERHUB_IMAGE` 已改为 `gpustack/workersim`。发布流程支持
`source_tag`，从实际检出的标签生成镜像并记录真实源码提交；已完成
[本次发布与容器检查](https://github.com/pengjiang80/gpustack-workersim/actions/runs/44566579683)。
迁移流程完整回归为 97 项；容器内验证版本和自定义配置。中英文 README、Compose、
发布指南、Release 说明及 demo2 启动辅助脚本已改用新地址。

Docker Hub 概览写入另行尝试，但当前令牌的管理 API 返回 403，浏览器未登录，
因此网页 Overview 尚未同步。镜像的推送、匿名拉取与公开可见性均已验证，GitHub
README 已更新。历史 0.1.0 镜像没有自动迁移，不把不存在的旧镜像标签列为可用。

## 原有环境保留

| 检查项 | 结果 |
|---|---|
| 原有 Worker | 54 个记录保留，包括 53 个模拟节点及原有的另一个节点 |
| 模拟节点 | 50 个 B300 + 3 个 910B4，均 Ready，ID、UUID、名称及地址保持 |
| 模型 | 28 个记录保留，名称、配置、后端参数、选择器、环境变量及副本数核对一致 |
| B300 Ready 副本 | 切换前 114，切换后恢复到 114 |
| 原有实例 ID | 基线捕获的 131 个唯一 ID 全部保留 |
| GPU/NPU 监控 | B300 集群 400 条设备显存监控序列；910B4 集群 24 条 |
| 引擎健康 | LLM、embedding、rerank、ASR、TTS 各抽查一个，健康接口均返回 200 |
| 自动启动 | 两个新容器均为 `unless-stopped`；Docker 服务 enabled |

最初分页基线有 132 行，包含控制器更新期间的重复 ID，因此不能把行数当作唯一
实例数。最终使用 `page=-1` 单次快照、按 ID 去重并补查，确认基线的 131 个唯一
实例 ID 均保留。最终快照为 135 个实例：114 Running、20 Pending、1 Unreachable。
Server 调度器始终在运行，本次没有删除、缩放或重建既有模型；升级前已有等待中任务，
本次不把它们视为已通过调度验收。

现有设备名称与容量保持 B300 288 GiB、910B4 64 GiB。为保留模型端口，显式保存旧的
64 端口池，没有切换为新环境默认的每节点 20 个端口：

| Fleet | Worker API | 监控 | 模型池 | 控制接口 |
|---|---|---|---|---|
| B300 | 20100–20149 | 20200–20249 | 40000–43199 | 127.0.0.1:20099 |
| 910B4 | 20400–20402 | 20500–20502 | 43200–43391 | 127.0.0.1:20399 |

保留原身份数据挂载、集群、节点名和分组顺序，仅将旧的额外全量轮询改为
`reconcile_interval: 0`，沿用当前版本的原生 watch 缓存同步。

## 自定义名称与单卡显存

先在独立临时集群验证，没有修改原有节点的设备定义或向原集群增加测试模型：

| 模板 | 自定义名称 | 单卡显存 | 每节点卡数 | 上报字节 |
|---|---|---:|---:|---:|
| h100-80gb | Custom NVIDIA Test 96.5GiB | 96.5 GiB | 2 | 103616086016 |
| ascend-910b4-64gb | Custom Ascend Test 96GiB | 96 GiB | 2 | 103079215104 |

Server Worker API、节点直接 `/metrics` 和 Server Prometheus 均匹配以上名称与
显存，厂商及 CUDA/CANN 类型继续继承模板。Prometheus 共获得 4 条设备显存序列。
验证后已删除本次临时容器、两个临时节点和空测试集群，并再次核对原有节点、模型集合。

## 通信观察与回滚

每个新 Worker 启动六路 watch。B300 验收采样为 300 路连接、0 次上报错误。
空闲集群的 `model-instances` 流观察到 45 秒读取超时、5 秒后重连；旧 910B4
模拟器在升级前也有类似超时记录。新 910B4 采样恰有部分连接处于重连等待中，
临时验证 Fleet 也记录了重连。因此本报告确认自定义上报和升级保留检查通过，
**不声称通信长期零错误**；没有延长超时或减少订阅来掩盖该现象。

配置、凭据、身份及私有模型基线备份位于主机
`/opt/workersim-upgrade-011-20261120`，另有本地私有备份，不提交凭据到仓库。
两个旧容器以 `*-pre011-*` 名称保留，已停止并设为 `restart=no`，避免启动时争用身份
和端口。新容器名称和数据目录保持不变。回滚应恢复对应旧配置后使用旧容器及当前
身份数据，不应运行历史版本的升级命令覆盖现有端口设置。
