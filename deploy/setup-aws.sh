#!/usr/bin/env bash
# One-time AWS setup for automatic deployment from GitHub Actions.
# Run it yourself, from Git Bash or any bash shell, with admin AWS credentials:
#
#     bash deploy/setup-aws.sh
#
# Safe to run more than once. It changes three things in your AWS account:
#
#   1. Lets the EC2 instance receive commands from AWS Systems Manager
#      (adds the AWS-managed AmazonSSMManagedInstanceCore policy to the
#      instance's role). This is how the deploy reaches the instance,
#      with no SSH key and no open port.
#   2. Creates the sign-in "trust" for GitHub (an OIDC provider) if the account
#      does not have one yet.
#   3. Creates an IAM role that GitHub Actions assumes. It can only be assumed
#      by THIS repository's main branch, and it can only push to the one ECR
#      repository and run a script on the one instance.
#
# Afterwards it prints the two values to enter in GitHub.

set -euo pipefail

REGION=us-east-1
GITHUB_REPO="TomYelnikoff/devleap_example_repo"
BRANCH=main
ECR_REPOSITORY=devleap-ecr-alexk
INSTANCE_ID=i-0fdc8357fdf0c281d
INSTANCE_ROLE=devleap-alexk-ec2-role
DEPLOY_ROLE=devleap-alexk-github-deploy

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
OIDC_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"

echo "Account $ACCOUNT_ID, region $REGION"

echo "1. Allow Systems Manager to send commands to the instance"
aws iam attach-role-policy --role-name "$INSTANCE_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

echo "2. GitHub sign-in trust (OIDC provider)"
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN" >/dev/null 2>&1; then
  echo "   already exists"
else
  aws iam create-open-id-connect-provider \
    --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com >/dev/null
  echo "   created"
fi

echo "3. Deploy role for GitHub Actions"
TRUST=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "${OIDC_ARN}" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:${GITHUB_REPO}:ref:refs/heads/${BRANCH}"
      }
    }
  }]
}
JSON
)
if aws iam get-role --role-name "$DEPLOY_ROLE" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$DEPLOY_ROLE" --policy-document "$TRUST"
  echo "   role exists; trust policy refreshed"
else
  aws iam create-role --role-name "$DEPLOY_ROLE" \
    --description "GitHub Actions deploy for ${GITHUB_REPO} (${BRANCH} branch only)" \
    --max-session-duration 3600 \
    --assume-role-policy-document "$TRUST" >/dev/null
  echo "   role created"
fi

aws iam put-role-policy --role-name "$DEPLOY_ROLE" --policy-name deploy-bedrock-chat --policy-document "$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "EcrLogin", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*" },
    { "Sid": "PushToOneRepository", "Effect": "Allow",
      "Action": ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
                 "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
      "Resource": "arn:aws:ecr:${REGION}:${ACCOUNT_ID}:repository/${ECR_REPOSITORY}" },
    { "Sid": "RunTheDeployScriptOnOneInstance", "Effect": "Allow", "Action": "ssm:SendCommand",
      "Resource": ["arn:aws:ssm:${REGION}::document/AWS-RunShellScript",
                   "arn:aws:ec2:${REGION}:${ACCOUNT_ID}:instance/${INSTANCE_ID}"] },
    { "Sid": "ReadTheResult", "Effect": "Allow",
      "Action": ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"], "Resource": "*" }
  ]
}
JSON
)"
echo "   permissions set"

echo
echo "Waiting up to 5 minutes for the instance to register with Systems Manager..."
for _ in $(seq 1 30); do
  PING="$(aws ssm describe-instance-information --region "$REGION" \
            --filters "Key=InstanceIds,Values=${INSTANCE_ID}" \
            --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || true)"
  if [ "$PING" = "Online" ]; then echo "   instance is Online in Systems Manager"; break; fi
  sleep 10
done
[ "$PING" = "Online" ] || echo "   not Online yet. It can take a few more minutes; re-run this script to check."

echo
echo "Done. In GitHub open:  ${GITHUB_REPO}  >  Settings  >  Secrets and variables  >  Actions  >  Variables"
echo "and add these two REPOSITORY VARIABLES (not secrets):"
echo
echo "  AWS_DEPLOY_ROLE_ARN = arn:aws:iam::${ACCOUNT_ID}:role/${DEPLOY_ROLE}"
echo "  EC2_INSTANCE_ID     = ${INSTANCE_ID}"
