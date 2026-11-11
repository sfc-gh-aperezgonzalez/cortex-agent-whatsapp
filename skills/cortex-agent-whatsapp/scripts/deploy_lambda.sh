#!/usr/bin/env bash
# Deploy / update the Cortex Agent gateway Lambda. Idempotent.
#
#   bash deploy_lambda.sh up        role + secret + function + public Function URL (+ permissions)
#   bash deploy_lambda.sh code      update code only (gateway.py + index.html)
#   bash deploy_lambda.sh config    update environment variables only
#   bash deploy_lambda.sh secret    push SECRET_FILE to Secrets Manager (after rotating PAT / Meta token)
#   bash deploy_lambda.sh egress [N] force N cold starts (default 3) and print the egress IP each time
#   bash deploy_lambda.sh url       print the Function URL
#
# Reads settings from ./deploy.env (or DEPLOY_ENV=path). See deploy.env.example.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_DIR="$HERE/../assets/gateway"
DEPLOY_ENV="${DEPLOY_ENV:-./deploy.env}"
[[ -f "$DEPLOY_ENV" ]] || { echo "Missing $DEPLOY_ENV (copy scripts/deploy.env.example)"; exit 1; }
set -a; source "$DEPLOY_ENV"; set +a

: "${AWS_PROFILE:?}" "${AWS_REGION:?}" "${FUNCTION_NAME:?}" "${ROLE_NAME:?}" "${SECRET_NAME:?}" "${SECRET_FILE:?}"
: "${SNOWFLAKE_HOST:?}" "${AGENT_FQN:?}" "${BOT_ROLE:?}" "${BOT_WAREHOUSE:?}" "${STATE_SCHEMA:?}"
export AWS_PROFILE AWS_REGION AWS_PAGER=""
SUBNET_IDS="${SUBNET_IDS:-}"; SECURITY_GROUP_IDS="${SECURITY_GROUP_IDS:-}"
RESERVED_CONCURRENCY="${RESERVED_CONCURRENCY:-10}"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

aws_q() { aws "$@" 2>/dev/null; }
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
FUNCTION_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}"

env_json() {  # Lambda environment as JSON (values may contain commas, so no shorthand syntax)
  python3 - "$WORK/env.json" "${1:-}" <<'PY'
import json, os, sys
keys = ["SNOWFLAKE_HOST", "AGENT_FQN", "BOT_ROLE", "BOT_WAREHOUSE", "STATE_SCHEMA",
        "TIMEZONE", "BOT_LANG", "ENABLE_WEB", "HISTORY_TURNS", "CONTEXT_LINE"]
v = {k: os.environ[k] for k in keys if os.environ.get(k)}
v["SECRET_ID"] = os.environ["SECRET_NAME"]
if sys.argv[2]:
    v["DEPLOY_NONCE"] = sys.argv[2]
json.dump({"Variables": v}, open(sys.argv[1], "w"))
PY
  echo "file://$WORK/env.json"
}

build_zip() {
  (cd "$GATEWAY_DIR" && zip -q -j "$WORK/gw.zip" gateway.py index.html)
  echo "fileb://$WORK/gw.zip"
}

wait_updated() { aws lambda wait function-updated-v2 --function-name "$FUNCTION_NAME" 2>/dev/null || aws lambda wait function-updated --function-name "$FUNCTION_NAME"; }

ensure_role() {
  if ! aws_q iam get-role --role-name "$ROLE_NAME" >/dev/null; then
    echo "· creating IAM role $ROLE_NAME"
    aws iam create-role --role-name "$ROLE_NAME" --output text --query Role.Arn \
      --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
    NEW_ROLE=1
  fi
  aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  if [[ -n "$SUBNET_IDS" ]]; then
    aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole
  fi
  SECRET_ARN="$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --query ARN --output text)"
  python3 - "$WORK/inline.json" "$SECRET_ARN" "$FUNCTION_ARN" <<'PY'
import json, sys
json.dump({"Version": "2012-10-17", "Statement": [
    {"Sid": "ReadGatewaySecret", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": sys.argv[2]},
    {"Sid": "AsyncSelfInvoke", "Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": sys.argv[3]}]},
    open(sys.argv[1], "w"))
PY
  aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name "${FUNCTION_NAME}-inline" --policy-document "file://$WORK/inline.json"
  if [[ "${NEW_ROLE:-0}" == 1 ]]; then echo "· waiting for IAM propagation"; sleep 12; fi
}

push_secret() {
  [[ -f "$SECRET_FILE" ]] || { echo "Missing SECRET_FILE $SECRET_FILE"; exit 1; }
  if aws_q secretsmanager describe-secret --secret-id "$SECRET_NAME" >/dev/null; then
    aws secretsmanager put-secret-value --secret-id "$SECRET_NAME" --secret-string "file://$SECRET_FILE" --query VersionId --output text >/dev/null
    echo "· secret $SECRET_NAME updated"
  else
    aws secretsmanager create-secret --name "$SECRET_NAME" --secret-string "file://$SECRET_FILE" \
      --description "Cortex Agent gateway secrets (Snowflake PAT, Meta app secret and token)" --query ARN --output text >/dev/null
    echo "· secret $SECRET_NAME created"
  fi
}

ensure_function() {
  local zip env role_arn
  zip="$(build_zip)"; env="$(env_json)"
  role_arn="$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)"
  local vpc=()
  if [[ -n "$SUBNET_IDS" ]]; then
    [[ -n "$SECURITY_GROUP_IDS" ]] || { echo "SECURITY_GROUP_IDS is required with SUBNET_IDS"; exit 1; }
    vpc=(--vpc-config "SubnetIds=${SUBNET_IDS},SecurityGroupIds=${SECURITY_GROUP_IDS}")
  fi
  if aws_q lambda get-function --function-name "$FUNCTION_NAME" >/dev/null; then
    echo "· updating function $FUNCTION_NAME"
    aws lambda update-function-code --function-name "$FUNCTION_NAME" --zip-file "$zip" --query LastUpdateStatus --output text >/dev/null
    wait_updated
    aws lambda update-function-configuration --function-name "$FUNCTION_NAME" --environment "$env" "${vpc[@]}" \
      --timeout 120 --memory-size 512 --query LastUpdateStatus --output text >/dev/null
    wait_updated
  else
    echo "· creating function $FUNCTION_NAME"
    aws lambda create-function --function-name "$FUNCTION_NAME" --runtime python3.12 --handler gateway.handler \
      --role "$role_arn" --zip-file "$zip" --timeout 120 --memory-size 512 --environment "$env" "${vpc[@]}" \
      --description "Cortex Agent gateway: WhatsApp (Meta Cloud API) + web chat" --query FunctionArn --output text >/dev/null
    aws lambda wait function-active-v2 --function-name "$FUNCTION_NAME" 2>/dev/null || aws lambda wait function-active --function-name "$FUNCTION_NAME"
  fi
  aws lambda put-function-concurrency --function-name "$FUNCTION_NAME" \
    --reserved-concurrent-executions "$RESERVED_CONCURRENCY" >/dev/null
}

ensure_url() {
  if ! aws_q lambda get-function-url-config --function-name "$FUNCTION_NAME" >/dev/null; then
    aws lambda create-function-url-config --function-name "$FUNCTION_NAME" --auth-type NONE >/dev/null
  fi
  # Public URL needs both permissions (AWS, Oct 2025+). The InvokeFunction grant is conditioned on
  # lambda:InvokedViaFunctionUrl so nobody can invoke the function directly (e.g. to inject internal jobs).
  aws_q lambda add-permission --function-name "$FUNCTION_NAME" --statement-id FunctionURLAllowPublicAccess \
    --action lambda:InvokeFunctionUrl --principal '*' --function-url-auth-type NONE >/dev/null || true
  aws_q lambda add-permission --function-name "$FUNCTION_NAME" --statement-id FunctionURLInvokeAllowPublicAccess \
    --action lambda:InvokeFunction --principal '*' --invoked-via-function-url >/dev/null || true
  print_url
}

print_url() { aws lambda get-function-url-config --function-name "$FUNCTION_NAME" --query FunctionUrl --output text; }

egress() {
  local n="${1:-3}" i
  for ((i = 1; i <= n; i++)); do
    if (( i > 1 )); then  # new env value -> new execution environment -> fresh cold start
      aws lambda update-function-configuration --function-name "$FUNCTION_NAME" --environment "$(env_json "nonce-$RANDOM-$i")" \
        --query LastUpdateStatus --output text >/dev/null
      wait_updated
    fi
    aws lambda invoke --function-name "$FUNCTION_NAME" --cli-binary-format raw-in-base64-out \
      --payload '{"job":"egress_ip"}' "$WORK/out.json" >/dev/null
    echo "cold start $i: $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("egress_ip"))' "$WORK/out.json")"
  done
  aws lambda update-function-configuration --function-name "$FUNCTION_NAME" --environment "$(env_json)" \
    --query LastUpdateStatus --output text >/dev/null
  wait_updated
}

case "${1:-}" in
  up)     push_secret; ensure_role; ensure_function; ensure_url ;;
  code)   aws lambda update-function-code --function-name "$FUNCTION_NAME" --zip-file "$(build_zip)" --query LastUpdateStatus --output text >/dev/null; wait_updated; echo "· code updated" ;;
  config) aws lambda update-function-configuration --function-name "$FUNCTION_NAME" --environment "$(env_json)" --query LastUpdateStatus --output text >/dev/null; wait_updated; echo "· config updated" ;;
  secret) push_secret
          # force new execution environments so the cached secret is re-read
          aws lambda update-function-configuration --function-name "$FUNCTION_NAME" --environment "$(env_json "secret-$(date +%s)")" --query LastUpdateStatus --output text >/dev/null; wait_updated ;;
  egress) egress "${2:-3}" ;;
  url)    print_url ;;
  *)      sed -n '2,12p' "$0"; exit 1 ;;
esac
