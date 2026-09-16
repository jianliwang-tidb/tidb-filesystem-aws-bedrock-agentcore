# Drive9 + AgentCore 执行手册（ARM64）

以下命令在项目根目录执行，AWS Region 固定为 `us-east-2`。EC2 连接示例：

```bash
ssh -i awsagentcore.pem ec2-user@ec2-3-16-165-159.us-east-2.compute.amazonaws.com
```

## 1. 前置检查

准备 Python 3.10+、AWS CLI v2、Docker Buildx、`jq`、两个同 VPC 子网、安全组、AgentCore Operator Role、Runtime Execution Role，以及三个 Drive9 token。Runtime Execution Role 的 trust policy 必须允许 `bedrock-agentcore.amazonaws.com` 承担；仅允许 `ec2.amazonaws.com` 会在 `create-agent-runtime` 时失败。安全组必须允许出站 TCP 443；如果使用私有子网，需要可经 NAT 访问 ECR、Secrets Manager、CloudWatch 和 Drive9。当前 AWS CLI 2.36.34 的 `invoke-agent-runtime` 需要 positional 输出文件，仓库中的调用脚本已兼容该行为。

```bash
export AWS_REGION=us-east-2
export AWS_DEFAULT_REGION=$AWS_REGION
aws sts get-caller-identity
python3 --version
aws --version
docker version
docker buildx version
```

本次测试使用 Anonymous workspace `https://api.drive9.ai`。如果使用 TiDBCloud server（例如 `https://aws-us-east-1.drive9.ai`），必须使用该 server 对应的 token，不能混用。

已完成的环境验证记录：EC2 可通过用户提供的 SSH 地址连接；AWS CLI `2.36.34` 已提供 `create-capacity-provider`、`create-agent-runtime` 和 `invoke-agent-runtime`；Drive9 CLI 使用 bearer token 已成功完成文件读写。若账号没有私有子网，可使用同 VPC 的可出站子网；本次账号实际使用一个 public subnet 完成验证。旧 Runtime 的失败日志为 `SyntaxError: '(' was never closed`，属于旧镜像语法错误，不能作为新 ARM64 镜像的验收结果。

### 创建 Capacity Provider Operator Role

本项目提供 [`scripts/create_capacity_provider_operator_role.sh`](../scripts/create_capacity_provider_operator_role.sh)，使用 AWS CLI 创建或更新 `CAPACITY_PROVIDER_OPERATOR_ROLE`。脚本绑定 AWS 官方托管策略 `BedrockAgentCoreRuntimeInstancesOperatorRolePolicy`，并将 trust policy 限制为当前账号在本 Region 创建的 Capacity Provider：

```bash
export AWS_REGION=us-east-2
export CAPACITY_PROVIDER_OPERATOR_ROLE_NAME=AgentCoreCapacityProviderOperatorRole
bash scripts/create_capacity_provider_operator_role.sh
```

脚本最后会打印 `CAPACITY_PROVIDER_OPERATOR_ROLE_ARN`，将该值用于部署：

```bash
export CAPACITY_PROVIDER_OPERATOR_ROLE_ARN=arn:aws:iam::<ACCOUNT_ID>:role/AgentCoreCapacityProviderOperatorRole
```

该 Role 只负责 Capacity Provider 管理的 EC2、Auto Scaling、EBS、网络接口、EventBridge 和相关 service-linked role 操作；不负责 Runtime 容器读取 Secrets Manager，也不负责调用 Runtime。Runtime Execution Role 需要单独配置，并允许 `bedrock-agentcore.amazonaws.com` 承担。

## 2. 创建 token 和 Secret

在已登录 Drive9 CLI 的环境生成 scoped token。A/B 共用共享目录权限，C 仅能访问自己的独立目录；C 不得拥有 A/B 共享目录的 `read` 或 `list` 权限：

```bash
export TEST_RUN_ID=agentcore-$(date -u +%Y%m%dT%H%M%SZ)
export DRIVE9_SERVER=https://api.drive9.ai
# A and B intentionally share one token and one directory.
drive9 token issue --subject "${TEST_RUN_ID}-agents-ab" --ttl 24h --allow /:pseudoroot --allow "/agentcore-tests/$TEST_RUN_ID/shared:read,list,search,write,delete" --print > agents-ab.token
# C has a separate read-only directory. Do not grant it the A/B shared prefix.
drive9 token issue --subject "${TEST_RUN_ID}-agent-c" --ttl 24h --allow /:pseudoroot --allow "/agentcore-tests/$TEST_RUN_ID/agent-c:read,list,search" --print > agent-c.token
cp agents-ab.token agent-a.token
cp agents-ab.token agent-b.token
for token_file in agents-ab.token agent-a.token agent-b.token agent-c.token; do
  test -s "$token_file" || { echo "empty token file: $token_file" >&2; exit 1; }
done
```

`--print` 输出的是 bearer token。Runtime 通过 `DRIVE9_API_KEY` 使用它；不要执行 `drive9 ctx import`。本测试中 agent-a 和 agent-b 必须写入同一个 token，二者使用 `/agentcore-tests/<TEST_RUN_ID>/shared`；agent-c 使用另一个只读 token，仅授权 `/agentcore-tests/<TEST_RUN_ID>/agent-c`。SecretString 为 `{"token":"<TOKEN>","server":"https://api.drive9.ai"}`：

```bash
aws secretsmanager create-secret --name "drive9/agentcore/${TEST_RUN_ID}-agent-a" --secret-string "$(jq -n --arg t "$(cat agent-a.token)" --arg s "$DRIVE9_SERVER" '{token:$t,server:$s}')"
aws secretsmanager create-secret --name "drive9/agentcore/${TEST_RUN_ID}-agent-b" --secret-string "$(jq -n --arg t "$(cat agent-b.token)" --arg s "$DRIVE9_SERVER" '{token:$t,server:$s}')"
aws secretsmanager create-secret --name "drive9/agentcore/${TEST_RUN_ID}-agent-c" --secret-string "$(jq -n --arg t "$(cat agent-c.token)" --arg s "$DRIVE9_SERVER" '{token:$t,server:$s}')"
```

创建后应检查 Secret 中 token 非空；不要只检查 `create-secret` 的返回码：

```bash
for agent in a b c; do
  secret_id="drive9/agentcore/${TEST_RUN_ID}-agent-${agent}"
  token_length=$(aws secretsmanager get-secret-value --secret-id "$secret_id" --region "$AWS_REGION" --query SecretString --output text | jq -r '.token // .api_key // "" | length')
  test "$token_length" -gt 0 || { echo "empty token in $secret_id" >&2; exit 1; }
done
```

## 3. 构建和推送 ARM64 镜像

```bash
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ECR_REPO=drive9-agentcore-test
export IMAGE_TAG=$(date -u +%Y%m%d%H%M%S)
export IMAGE_URI=$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:$IMAGE_TAG
aws ecr describe-repositories --repository-name $ECR_REPO --region $AWS_REGION >/dev/null 2>&1 || aws ecr create-repository --repository-name $ECR_REPO --region $AWS_REGION
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
docker buildx build --platform linux/arm64 --tag $IMAGE_URI --push .
docker buildx imagetools inspect $IMAGE_URI | rg 'linux/arm64'
```

输出必须包含 `linux/arm64`。Dockerfile 使用 ARM64 Drive9 CLI 下载地址；不需要执行 CPU 测试。

## 4. 部署 Capacity Provider 和 Runtime

```bash
export CAPACITY_PROVIDER_OPERATOR_ROLE_ARN=arn:aws:iam::$AWS_ACCOUNT_ID:role/<operator-role>
export AGENT_RUNTIME_ROLE_ARN=arn:aws:iam::$AWS_ACCOUNT_ID:role/<runtime-role>
export SUBNET_IDS=subnet-private-a,subnet-private-b
export SECURITY_GROUP_IDS=sg-agentcore-runtime
export INSTANCE_TYPE=c6g.large
export CAPACITY_PROVIDER_NAME=drive9_arm64_$RANDOM
export PROVIDER_MAX_LIFETIME=3600
export RUNTIME_MAX_LIFETIME=1800
export RUNTIME_IDLE_TIMEOUT=300
export AGENTCORE_CONTROL_METHOD=auto
python3 deploy.py
```

`deploy.py` 默认创建 `LINUX_ARM64` Capacity Provider，生成 `deployment.json`，并将 `DRIVE9_SERVER`, `DRIVE9_SECRET_ID` 和 `EXPECTED_ARCHITECTURE=arm64` 注入 Runtime。默认 Secret 名为 `drive9/agentcore/<TEST_RUN_ID>-agent-a|b|c`，与上一步创建的短期 token 一一对应；如需覆盖，可设置 `DRIVE9_SECRET_ID_AGENT_A/B/C`。`auto` 优先 boto3 control-plane SDK；若 boto3 缺少 Capacity Provider 方法则自动改用 `aws bedrock-agentcore-control`。也可显式设置 `AGENTCORE_CONTROL_METHOD=sdk` 或 `cli`。

## 5. SDK/CLI 单步调用

生成长度至少 33 个字符的 Session ID：

```bash
export SESSION_A=agent-a-primary-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
```

SDK 调用：

```bash
python3 tests/invoke_action.py --method sdk --deployment deployment.json --agent agent-a --session-id "$SESSION_A" --payload '{"action":"preflight"}'
```

CLI 调用：

```bash
python3 tests/invoke_action.py --method cli --deployment deployment.json --agent agent-a --session-id "$SESSION_A" --payload '{"action":"preflight"}'
```

`--method auto` 默认先尝试 boto3，失败后回退 AWS CLI。响应应为 `status: PASS`；HTTP/DNS、Drive9 workspace list、token scope 和架构信息会在响应中保留。

## 6. 回归测试

先跳过可能受平台限制的 FUSE/Git：

```bash
python3 tests/run_tests.py --deployment deployment.json --skip-mount
```

测试包括 preflight、CRUD、append、copy/move/list/stat、A/B 同路径目录快照比较、C 对 A/B 共享目录的不可见性、跨 Runtime 持久化、handoff、C Agent 自己目录的只读权限、find/search 以及新 Session 恢复。A/B 快照测试会分别通过两个 Runtime 列出 `functional/`，规范化后比较结果；C 隔离测试会让 agent-c 对 A/B 共享目录执行 `list`，预期得到不可访问结果，Drive9 可能返回 `fs access denied` 或 `not found` 来隐藏无权路径。双方目录内容、C 的拒绝结果和比较结论会打印到回归日志，并写入 `results.jsonl` 和 `report.md`。脚本不包含 CPU probe、CPU benchmark 或并发 CPU workload。结果写入 `artifacts/<TEST_RUN_ID>/results.jsonl`、`report.md` 和 `sessions.json`。出现 `FAIL` 时退出码为 1。

使用 `--skip-mount` 时，报告中的结论只覆盖 CLI/API 核心回归，并会明确标注 FUSE/Git 为 `SKIPPED`；要得到完整平台判定请省略该参数。

如需验证 FUSE/Git：

```bash
python3 tests/run_tests.py --deployment deployment.json
```

AgentCore 未提供 `/dev/fuse` 或 mount capability 时，FUSE/Git 标记为 `PLATFORM_BLOCKED`，整体为 `CONDITIONAL GO`，不影响 CLI/API 验收。

## 7. 清理

保留报告后，使用 `deployment.json` 中的 ID 删除 Runtime、Capacity Provider session、Capacity Provider，并撤销临时 Drive9 token。只清理 `:/agentcore-tests/<TEST_RUN_ID>/shared` 和 `:/agentcore-tests/<TEST_RUN_ID>/agent-c`，不要删除其他 workspace 数据。删除顺序应为 Runtime -> Capacity Provider；异步删除完成后再删除 ECR 镜像和测试 Secret。

```bash
CP_ID=$(jq -r .capacity_provider_id deployment.json)
for SID in "${SESSION_A:-}" "${SESSION_B:-}" "${SESSION_C:-}" "${SESSION_A_NEW:-}"; do
  [ -n "$SID" ] && aws bedrock-agentcore delete-capacity-provider-session --capacity-provider-id "$CP_ID" --session-id "$SID" --region "$AWS_REGION" || true
done
for AGENT in agent-a agent-b agent-c; do
  RID=$(jq -r ".runtimes[\"$AGENT\"].agent_runtime_id" deployment.json)
  [ "$RID" != "null" ] && aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$RID" --region "$AWS_REGION" || true
done
aws bedrock-agentcore-control delete-capacity-provider --capacity-provider-id "$CP_ID" --region "$AWS_REGION" || true
```

AWS CLI 2.36.x 要求 AgentCore control-plane 删除操作的 `--client-token` 至少为 33 个字符；如果手工补充该参数，请使用 UUID，例如 `cleanup-runtime-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')`。删除 Runtime 后应等待其进入 `DELETED` 或确认不存在，再删除 Capacity Provider。Drive9 scoped token 不能调用 token 管理 API；撤销临时 token 时应切回 owner context，再执行 `drive9 token revoke --api-key-file agent-a.token` 等命令。

## 8. 故障排查

| 现象 | 检查 |
|---|---|
| `create-capacity-provider` 不存在 | 升级 AWS CLI/boto3；或设置 `AGENTCORE_CONTROL_METHOD=cli`。若当前 PyPI 镜像最高只提供 boto3 1.42.x，使用本项目 `requirements.txt` 的兼容下限并让部署脚本走 CLI control plane。|
| Runtime 启动 `SyntaxError` | 重新构建并检查 ECR digest，确认镜像为 `linux/arm64`。旧 Runtime 的 `SyntaxError: '(' was never closed` 不能代表当前代码。|
| Drive9 401/403 | 确认 token 未过期、Secret JSON 正确、scope 覆盖测试目录，并确认 Anonymous/TiDBCloud server 匹配。|
| `Drive9 secret JSON must contain token or api_key` 或 `Drive9 secret token is empty` | Secret 可以被读取但 token 字段缺失或为空。检查 `agent-*.token` 文件大小，重新执行 `drive9 token issue ... --print`，确认 `test -s` 通过后再用 `put-secret-value` 更新 Secret。|
| DNS/HTTPS timeout | 检查私有子网 NAT、DNS 支持、路由和出站 443。|
| Secret `AccessDenied` | 给 Runtime Execution Role 授予对应 Secret 的 `secretsmanager:GetSecretValue`。|
| Runtime 读取了旧 token 或找不到 Secret | 确认 `deployment.json` 的三个 `secret_id` 是 `drive9/agentcore/<TEST_RUN_ID>-agent-a|b|c`，不要使用无运行 ID 的旧 Secret 名。|
| FUSE `operation not permitted`、`/dev/fuse` 不存在，或 `/mnt/drive9-*` 创建返回 `Permission denied` | 平台能力限制。当前版本会将这些情况标记为 `PLATFORM_BLOCKED`，保留 CLI/API 结果并按 `CONDITIONAL GO` 记录；只有非权限类挂载错误才标记为 `FAIL`。|
