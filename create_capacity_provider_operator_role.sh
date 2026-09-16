#!/usr/bin/env bash

# Create the IAM role used by an AgentCore Capacity Provider.
# This role is separate from the Runtime execution role.

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-2}"
ROLE_NAME="${CAPACITY_PROVIDER_OPERATOR_ROLE_NAME:-AgentCoreCapacityProviderOperatorRole}"
POLICY_ARN="arn:aws:iam::aws:policy/BedrockAgentCoreRuntimeInstancesOperatorRolePolicy"

if [[ "$AWS_REGION" != "us-east-2" ]]; then
  echo "This project is configured for us-east-2; got AWS_REGION=$AWS_REGION" >&2
  exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
TRUST_FILE="$(mktemp)"
trap 'rm -f "$TRUST_FILE"' EXIT

cat >"$TRUST_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowBedrockAgentCoreAssumeRole",
      "Effect": "Allow",
      "Principal": {
        "Service": "bedrock-agentcore.amazonaws.com"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": "$ACCOUNT_ID"
        },
        "ArnLike": {
          "aws:SourceArn": "arn:aws:bedrock-agentcore:$AWS_REGION:$ACCOUNT_ID:capacity-provider/*"
        }
      }
    }
  ]
}
EOF

if aws iam get-role --role-name "$ROLE_NAME" --query Role.RoleName --output text >/dev/null 2>&1; then
  echo "Updating trust policy for existing role: $ROLE_NAME"
  aws iam update-assume-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-document "file://$TRUST_FILE"
else
  echo "Creating role: $ROLE_NAME"
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --description "AgentCore Capacity Provider operator role for Drive9 validation" \
    --assume-role-policy-document "file://$TRUST_FILE" \
    --tags Key=Project,Value=Drive9AgentCore Key=ManagedBy,Value=AWSCLI
fi

echo "Attaching AWS managed policy: $POLICY_ARN"
aws iam attach-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-arn "$POLICY_ARN"

ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)"
echo ""
echo "CAPACITY_PROVIDER_OPERATOR_ROLE_ARN=$ROLE_ARN"
echo ""
echo "Trust policy:"
aws iam get-role \
  --role-name "$ROLE_NAME" \
  --query Role.AssumeRolePolicyDocument \
  --output json
echo "Attached policies:"
aws iam list-attached-role-policies \
  --role-name "$ROLE_NAME" \
  --query "AttachedPolicies[?PolicyArn=='$POLICY_ARN'].[PolicyName,PolicyArn]" \
  --output table
