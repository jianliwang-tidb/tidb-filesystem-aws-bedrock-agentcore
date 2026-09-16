
# Drive9 + AgentCore Execution Manual (ARM64)

All commands below are run from the project root; the AWS Region is fixed to `us-east-2`. Example EC2 connection:

```
ssh -i awsagentcore.pem ec2-user@ec2-3-16-165-159.us-east-2.compute.amazonaws.com
```

## 1. Pre-flight Checks

Prepare Python 3.10+, AWS CLI v2, Docker Buildx, `jq`, two subnets in the same VPC, a security group, the AgentCore Operator Role, the Runtime Execution Role, and three Drive9 tokens. The Runtime Execution Role's trust policy must allow `bedrock-agentcore.amazonaws.com` to assume it; allowing only `ec2.amazonaws.com` will cause `create-agent-runtime` to fail. The security group must allow outbound TCP 443; if private subnets are used, ECR, Secrets Manager, CloudWatch, and Drive9 must be reachable via NAT. As of AWS CLI 2.36.34, `invoke-agent-runtime` requires a positional output file; the invocation scripts in this repo are already compatible with this behavior.

```
export AWS_REGION=us-east-2
export AWS_DEFAULT_REGION=$AWS_REGION
aws sts get-caller-identity
python3 --version
aws --version
docker version
docker buildx version
```

This test uses the Anonymous workspace `[https://api.drive9.ai](https://api.drive9.ai)`. If using a TiDBCloud server (e.g. `[https://aws-us-east-1.drive9.ai](https://aws-us-east-1.drive9.ai)`), you must use the token corresponding to that server; do not mix them.

Recorded environment verification: EC2 is reachable via the SSH address provided by the user; AWS CLI `2.36.34` provides `create-capacity-provider`, `create-agent-runtime`, and `invoke-agent-runtime`; the Drive9 CLI successfully completed file read/write using a bearer token. If the account has no private subnets, a subnet with outbound access in the same VPC can be used; this account actually used one public subnet for verification. The old Runtime's failure log was `SyntaxError: '(' was never closed`, which is a syntax error in the old image and must not be treated as an acceptance result for the new ARM64 image.

### Creating the Capacity Provider Operator Role

This project provides [`scripts/create_capacity_provider_operator_role.sh`](../scripts/create_capacity_provider_operator_role.sh), which uses the AWS CLI to create or update `CAPACITY_PROVIDER_OPERATOR_ROLE`. The script attaches the AWS official managed policy `BedrockAgentCoreRuntimeInstancesOperatorRolePolicy` and restricts the trust policy to Capacity Providers created by the current account in this Region:

```
export AWS_REGION=us-east-2
export CAPACITY_PROVIDER_OPERATOR_ROLE_NAME=AgentCoreCapacityProviderOperatorRole
bash scripts/create_capacity_provider_operator_role.sh
```

The script prints `CAPACITY_PROVIDER_OPERATOR_ROLE_ARN` at the end; use that value for deployment:

```
export CAPACITY_PROVIDER_OPERATOR_ROLE_ARN=arn:aws:iam::<ACCOUNT_ID>:role/AgentCoreCapacityProviderOperatorRole
```

This Role only handles the EC2, Auto Scaling, EBS, network interface, EventBridge, and related service-linked role operations managed by the Capacity Provider; it does not read Secrets Manager for the Runtime container, nor does it invoke the Runtime. The Runtime Execution Role must be configured separately and must allow `bedrock-agentcore.amazonaws.com` to assume it.

## 2. Creating Tokens and Secrets

Generate scoped tokens in an environment where the Drive9 CLI is already logged in. A/B share shared-directory permissions; C can only access its own separate directory; C must not have `read` or `list` permission on the A/B shared directory:

```
export TEST_RUN_ID=agentcore-$(date -u +%Y%m%dT%H%M%SZ)
export DRIVE9_SERVER=[https://api.drive9.ai](https://api.drive9.ai)
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

`--print` outputs a bearer token. The Runtime uses it via `DRIVE9_API_KEY`; do not run `drive9 ctx import`. In this test, agent-a and agent-b must be written with the same token, and both use `/agentcore-tests/<TEST_RUN_ID>/shared`; agent-c uses a different read-only token authorized only for `/agentcore-tests/<TEST_RUN_ID>/agent-c`. The SecretString is `{"token":"<TOKEN>","server":"[https://api.drive9.ai](https://api.drive9.ai)"}`:

```
aws secretsmanager create-secret --name "drive9/agentcore/${TEST_RUN_ID}-agent-a" --secret-string "$(jq -n --arg t "$(cat agent-a.token)" --arg s "$DRIVE9_SERVER" '{token:$t,server:$s}')"
aws secretsmanager create-secret --name "drive9/agentcore/${TEST_RUN_ID}-agent-b" --secret-string "$(jq -n --arg t "$(cat agent-b.token)" --arg s "$DRIVE9_SERVER" '{token:$t,server:$s}')"
aws secretsmanager create-secret --name "drive9/agentcore/${TEST_RUN_ID}-agent-c" --secret-string "$(jq -n --arg t "$(cat agent-c.token)" --arg s "$DRIVE9_SERVER" '{token:$t,server:$s}')"
```

After creation, verify that the token in the Secret is non-empty; do not rely only on the `create-secret` return code:

```
for agent in a b c; do
  secret_id="drive9/agentcore/${TEST_RUN_ID}-agent-${agent}"
  token_length=$(aws secretsmanager get-secret-value --secret-id "$secret_id" --region "$AWS_REGION" --query SecretString --output text | jq -r '.token // .api_key // "" | length')
  test "$token_length" -gt 0 || { echo "empty token in $secret_id" >&2; exit 1; }
done
```

## 3. Building and Pushing the ARM64 Image

```
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ECR_REPO=drive9-agentcore-test
export IMAGE_TAG=$(date -u +%Y%m%d%H%M%S)
export IMAGE_URI=$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:$IMAGE_TAG
aws ecr describe-repositories --repository-name $ECR_REPO --region $AWS_REGION >/dev/null 2>&1 || aws ecr create-repository --repository-name $ECR_REPO --region $AWS_REGION
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
docker buildx build --platform linux/arm64 --tag $IMAGE_URI --push .
docker buildx imagetools inspect $IMAGE_URI | rg 'linux/arm64'
```

The output must contain `linux/arm64`. The Dockerfile uses the ARM64 Drive9 CLI download URL; no CPU tests need to be run.

## 4. Deploying the Capacity Provider and Runtimes

```
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

`deploy.py` creates a `LINUX_ARM64` Capacity Provider by default, generates `deployment.json`, and injects `DRIVE9_SERVER`, `DRIVE9_SECRET_ID`, and `EXPECTED_ARCHITECTURE=arm64` into the Runtimes. The default Secret names are `drive9/agentcore/<TEST_RUN_ID>-agent-a|b|c`, matching the short-lived tokens created in the previous step one-to-one; to override, set `DRIVE9_SECRET_ID_AGENT_A/B/C`. `auto` prefers the boto3 control-plane SDK; if boto3 lacks the Capacity Provider methods, it automatically falls back to `aws bedrock-agentcore-control`. You can also explicitly set `AGENTCORE_CONTROL_METHOD=sdk` or `cli`.

## 5. SDK/CLI Single-Step Invocation

Generate a Session ID at least 33 characters long:

```
export SESSION_A=agent-a-primary-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
```

SDK invocation:

```
python3 tests/invoke_action.py --method sdk --deployment deployment.json --agent agent-a --session-id "$SESSION_A" --payload '{"action":"preflight"}'
```

CLI invocation:

```
python3 tests/invoke_action.py --method cli --deployment deployment.json --agent agent-a --session-id "$SESSION_A" --payload '{"action":"preflight"}'
```

`--method auto` tries boto3 first by default and falls back to the AWS CLI on failure. The response should be `status: PASS`; HTTP/DNS, Drive9 workspace list, token scope, and architecture information are retained in the response.

## 6. Regression Tests

First skip FUSE/Git, which may be platform-limited:

```
python3 tests/run_tests.py --deployment deployment.json --skip-mount
```

The tests cover preflight, CRUD, append, copy/move/list/stat, A/B directory-snapshot comparison on the same path, C's invisibility to the A/B shared directory, cross-Runtime persistence, handoff, C Agent's read-only permission on its own directory, find/search, and new-Session recovery. The A/B snapshot test lists `functional/` through the two Runtimes separately, normalizes, and compares the results; the C isolation test makes agent-c `list` the A/B shared directory, expecting an inaccessible result — Drive9 may return `fs access denied` or `not found` to hide unauthorized paths. Both directories' contents, C's denial result, and the comparison conclusion are printed to the regression log and written to `results.jsonl` and `report.md`. The script contains no CPU probe, CPU benchmark, or concurrent CPU workload. Results are written to `artifacts/<TEST_RUN_ID>/results.jsonl`, `report.md`, and `sessions.json`. The exit code is 1 on any `FAIL`.

With `--skip-mount`, the report's conclusions only cover the CLI/API core regression and explicitly mark FUSE/Git as `SKIPPED`; omit the flag for a full platform verdict.

To verify FUSE/Git:

```
python3 tests/run_tests.py --deployment deployment.json
```

When AgentCore does not provide `/dev/fuse` or mount capability, FUSE/Git is marked `PLATFORM_BLOCKED`, the overall result is `CONDITIONAL GO`, and this does not affect CLI/API acceptance.

## 7. Cleanup

After keeping the reports, use the IDs in `deployment.json` to delete the Runtimes, Capacity Provider sessions, and Capacity Provider, and revoke the temporary Drive9 tokens. Only clean up `:/agentcore-tests/<TEST_RUN_ID>/shared` and `:/agentcore-tests/<TEST_RUN_ID>/agent-c`; do not delete other workspace data. The deletion order should be Runtime -> Capacity Provider; after asynchronous deletion completes, delete the ECR image and test Secrets.

```
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

AWS CLI 2.36.x requires `--client-token` to be at least 33 characters for AgentCore control-plane delete operations; if you supply this parameter manually, use a UUID, e.g. `cleanup-runtime-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')`. After deleting a Runtime, wait until it reaches `DELETED` or is confirmed gone before deleting the Capacity Provider. Drive9 scoped tokens cannot call the token management API; when revoking temporary tokens, switch back to the owner context, then run commands such as `drive9 token revoke --api-key-file agent-a.token`.

## 8. Troubleshooting

| Symptom | Check |
| --- | --- |
| `create-capacity-provider` does not exist | Upgrade AWS CLI/boto3; or set `AGENTCORE_CONTROL_METHOD=cli`. If the current PyPI mirror only provides boto3 up to 1.42.x, use the compatible floor in this project's `requirements.txt` and let the deployment script use the CLI control plane. |
| Runtime `SyntaxError` on startup | Rebuild and check the ECR digest, confirming the image is `linux/arm64`. The old Runtime's `SyntaxError: '(' was never closed` does not represent the current code. |
| Drive9 401/403 | Confirm the token has not expired, the Secret JSON is correct, the scope covers the test directory, and the Anonymous/TiDBCloud server matches. |
| `Drive9 secret JSON must contain token or api_key` or `Drive9 secret token is empty` | The Secret is readable but the token field is missing or empty. Check the `agent-*.token` file sizes, re-run `drive9 token issue ... --print`, confirm `test -s` passes, then update the Secret with `put-secret-value`. |
| DNS/HTTPS timeout | Check the private subnet NAT, DNS support, routing, and outbound 443. |
| Secret `AccessDenied` | Grant the Runtime Execution Role `secretsmanager:GetSecretValue` on the corresponding Secret. |
| Runtime reads an old token or cannot find the Secret | Confirm the three `secret_id`s in `deployment.json` are `drive9/agentcore/-agent-a |
| FUSE `operation not permitted`, `/dev/fuse` missing, or `/mnt/drive9-*` creation returns `Permission denied` | Platform capability limitation. The current version marks these cases as `PLATFORM_BLOCKED`, keeps the CLI/API results, and records them as `CONDITIONAL GO`; only non-permission mount errors are marked as `FAIL`. |

