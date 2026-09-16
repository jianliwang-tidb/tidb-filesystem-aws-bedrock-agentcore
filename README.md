# Drive9 + AWS Bedrock AgentCore Runtime Instances（ARM64）

本 Demo 在 AWS `us-east-2` 部署三个 AgentCore Runtime，验证 Drive9 CLI/API 在 Runtime 中的文件操作、持久化、跨 Agent 交接、权限和检索能力。Docker 镜像与 Capacity Provider 固定使用 ARM64；本项目不执行 CPU benchmark 或 CPU 配额测试。

## 已验证结论

- EC2、AWS CLI 和 AgentCore Runtime Instances API 位于 `us-east-2`。
- ARM64 Capacity Provider 使用 `LINUX_ARM64`，默认实例类型为 `c6g.large`。
- Drive9 CLI 使用 `DRIVE9_API_KEY` bearer token 已成功完成文件读写；Runtime 不应把该 token 传给 `drive9 ctx import`。
- 本次测试使用 Anonymous workspace `https://api.drive9.ai`。不要把 `api.drive9.ai` 的 token 与区域 TiDBCloud server 混用。
- 旧 Runtime 曾因镜像中的 `SyntaxError: '(' was never closed` 启动失败；必须重新构建并推送 ARM64 镜像后再验收。
- agent-a 和 agent-b 必须使用同一个可读写 scoped token，并只访问共享目录；agent-c 必须使用独立的只读 scoped token 和独立目录，同时验证其无法列出 A/B 共享目录。

## 文件

- `app.py`：Runtime handler；从 Secrets Manager 读取 Drive9 token，执行 CLI action。
- `deploy.py`：创建 ARM64 Capacity Provider 和三个 Runtime，支持 SDK/CLI control plane。
- `tests/invoke_action.py`：单 action 调用，支持 `--method auto|sdk|cli`。
- `tests/run_tests.py`：SDK 回归测试和 Markdown/JSONL 证据输出。
- `scripts/create_capacity_provider_operator_role.sh`：使用 AWS CLI 创建 Capacity Provider Operator Role。
- `docs/EXECUTE_TESTS.md`：完整执行、验收、清理和故障排查步骤。

## 快速开始

```bash
export AWS_REGION=us-east-2
export DRIVE9_SERVER=https://api.drive9.ai
python3 -m py_compile app.py deploy.py tests/invoke_action.py tests/run_tests.py
```

随后按 [`docs/EXECUTE_TESTS.md`](docs/EXECUTE_TESTS.md) 构建 `linux/arm64` 镜像、推送 ECR、创建 Secret、部署 Runtime，并执行冒烟和回归测试。
