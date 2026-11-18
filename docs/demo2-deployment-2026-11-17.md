# demo2 WorkerSim 环境交付

访问地址：http://demo2.gpustack.cn:8080 。登录使用原有 admin 账号，选择 `workersim-demo2` 集群（ID 3）。所有新增模型以 `workersim-` 开头。

## 最终配置

- 原有 Server：`gpustack/gpustack-ee:v2.3.1-ent2`，版本提交 `ece47c3`。未修改 Server 或 UI。
- WorkerSim：`gpustack-workersim:0.4.4`，与 Server 同主机，50 个逻辑 Worker，每个 8 张 288 GiB B300，共 400 张卡。
- 400/400 张模拟 GPU 已分配。显存分配约 88.4%，是调度资源声明，不是实际 GPU 压测结果。
- 16 个模型、116 个副本全部 Running；102 个去重后的 Worker ModelFile 全部 Ready、进度 100%。同节点同来源的副本可复用模型文件。
- 两路事件订阅均已连接：50 条实例 watch、50 条模型文件 watch。
- 现有两个集群、原有 Worker 和六个零副本模型保留。

| 模型 | 副本数 | 每副本 GPU |
|---|---:|---:|
| DeepSeek-V3.2 | 10 | 8 |
| GLM-5.3 | 10 | 8 |
| Kimi-K2.5 | 10 | 8 |
| Qwen3.5-397B-A17B | 10 | 8 |
| Qwen3-32B | 16 | 1 |
| Qwen3-8B | 16 | 1 |
| DeepSeek-R1-Distill-Qwen-32B | 12 | 1 |
| Qwen3-VL-32B-Instruct | 4 | 2 |
| BGE-M3 | 2 | 1 |
| BGE-reranker-v2-m3 | 2 | 1 |
| Qwen3-Embedding-8B | 8 | 1 |
| Qwen3-Reranker-8B | 8 | 1 |
| Qwen3-ASR-0.6B / 1.7B | 各 2 | 1 |
| Qwen3-TTS CustomVoice 1.7B / 0.6B | 各 2 | 1 |

## 模拟返回与验证

0.4.4 新增 `/v1/embeddings`、`/v1/rerank`、`/v1/audio/transcriptions`、`/v1/audio/speech` 的测试返回。通过现有 Server 的原生网关逐一调用了全部 16 个模型，均返回 HTTP 200。聊天、向量、排序和转写均为合成内容；TTS 返回 0.25 秒静音 WAV。未下载真实权重，未运行推理引擎。

完整 Linux 回归：54 项通过，耗时 24.59 秒。测试容器使用 `--ulimit nofile=65536:65536` 并挂载完整源码目录，包含配置夹和脚本。

满载后实际重启了 WorkerSim 容器，50 个 Worker ID 仍为 2–51，116 个实例与两路 watch 自动恢复，再次通过全部 16 个模型的网关调用。此验证没有重启共享主机或现有 Server。

从 32 扩至 50 时曾有一次端口 `42048` 占用重试，模拟器自动恢复。扩容前已经扩大了系统端口预留，但预留操作不会清理此前存在的连接。该记录保存在扩容基线中；满载重启后的验收未发现上报或过期代际错误。

企业版此构建的实例 `download_progress` 会保留部分历史中间值；对模拟 Worker 自有实例 PUT 100 后回读仍为原值。模型文件本身均为 Ready/100%，所有副本均为 Running，故验收以这两个状态为准。此处使用公开 API 核对，未修改企业版内部实现。

## 运行与维护

容器名：`gpustack-workersim-demo2`。Docker 服务已启用，容器重启策略为 `unless-stopped`，运行中的容器会随主机启动恢复。

配置、令牌和状态保存在 `/opt/gpustack-workersim-demo2`，父目录仅 root 可访问。`data` 目录持久保存各 Worker 的 Server 身份；令牌不在镜像或源代码包中。监听地址为 Docker 桥接地址 `172.17.0.1`，控制接口仅在本机 `127.0.0.1:20099` 开放。节点共享 IP，Worker API 使用 20100–20149，监控使用 20200–20249，模型实例端口池为 40000–43199。系统端口预留配置与原有预留范围合并。

```bash
sudo docker logs --tail 100 gpustack-workersim-demo2
sudo docker restart gpustack-workersim-demo2
sudo python3 /opt/gpustack-workersim-demo2/source-0.4.4/scripts/validate_demo2.py
```

配置修改后，通过 `run_demo2_container.py --replace` 重建模拟器可重新挂载配置，身份目录继续保留。该脚本只操作带 `gpustack.workersim.owner=demo2` 标记的容器，并保留一个关闭自动启动的旧容器用于回滚。

通用源码、镜像导入方法见仓库 README；此环境使用的无密钥配置与模型清单在 `configs/demo2-50-b300.yaml`、`configs/demo2-models.json`。企业版版本号带 `ent2`，此配置显式开启版本差异兼容；验证结论限于上述构建。
