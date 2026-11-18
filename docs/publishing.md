# GitHub 与 Docker Hub 发布

推荐使用同名仓库 `gpustack-workersim`，在 GitHub 管理源码，在 Docker Hub 分发 Linux amd64 镜像。仓库归属与公开范围由仓库所有者决定。

## 一次性配置

1. 创建 GitHub 仓库，设置远端后提交并推送本地代码。
2. 在 Docker Hub 创建目标镜像仓库，并选择公开或私有。
3. 在 GitHub 仓库 Settings → Secrets and variables → Actions 设置：

| 类型 | 名称 | 内容 |
|---|---|---|
| Variable | `DOCKERHUB_USERNAME` | 用于 Docker Hub 登录的账号 |
| Variable | `DOCKERHUB_IMAGE` | 完整镜像名，例如 `your-org/gpustack-workersim` |
| Secret | `DOCKERHUB_TOKEN` | 对目标镜像有推送权限的访问令牌 |

Docker Hub 组织命名空间可以与登录账号不同。令牌放在 GitHub Secret 中，按 [Docker 官方 Actions 配置](https://docs.docker.com/build/ci/github-actions/) 使用，不写入代码或镜像。

`.gitignore` 排除了运行身份、凭据文件、缓存、虚拟环境、测试输出和镜像归档。GPUStack 派生代码的原有许可证和说明保留在 `workersim/_vendor`。公开发布前应检查拟提交文件中的环境地址等部署信息。

## 同步与发布

向 `main` 推送代码会运行 CI。推送与 `pyproject.toml` 中版本一致的标签会运行测试，再构建并推送镜像：

```bash
git push origin main
git tag v0.4.5
git push origin v0.4.5
```

发布标签为 `${DOCKERHUB_IMAGE}:0.4.5`。工作流会在 Docker Hub 按 digest 拉取已发布的镜像，并运行 `--version` 验证。标签版本与源码版本不一致时终止发布。

也可以在 GitHub Actions 中手动运行 `Publish Docker image`。默认仅发布版本号标签；勾选 `Also update the latest image tag` 后才更新 `latest`。

参考：[GitHub 创建仓库](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository)、[Docker 访问令牌](https://docs.docker.com/security/access-tokens/personal-access-tokens/)。
