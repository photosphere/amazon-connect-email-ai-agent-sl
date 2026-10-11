#!/usr/bin/env bash
# Shared functions for deploy_week_1.sh ... deploy_week_4.sh. Do not run directly - it is sourced.
#
# Every step is idempotent (create if missing, otherwise update), so a weekly script can be re-run,
# and each week's script re-applies the earlier weeks' infrastructure before adding its own.
#
# 资源前缀与 Connect 实例：init_env 以中文交互方式询问（默认取上次的选择，保存在 .deploy_settings），
# 四个周脚本和 setup_kb.sh 共用，保证各周部署到同一实例、同一套名字。非交互运行时用环境变量提供。
#
# Common environment (all weeks):
#   CONNECT_INSTANCE_ARN   Connect 实例，arn:aws:connect:<region>:<account>:instance/<id>（交互时可从列表选择）
#   NAME_PREFIX            资源前缀，默认 sl（小写字母开头，小写字母/数字，最多 10 位）。由它派生：
#                            Lambda / IAM      <p>-email-ai-triage / <p>-email-ai-agent / <p>-email-ai-lambda-role
#                            DynamoDB          <p>-email-ai-poc
#                            Connect           <P>-Email-AI-Inbound（flow）、<P>-Email-AI-AgentGuide（agent guide flow，Show view 详情视图）、
#                                              <P>-Email-General / -Security /
#                                              -Reservations / -Loyalty（队列）、<P>-Email-AI-Agents（路由配置文件）
#                            邮箱              <p>_reservations@、<p>_rewards@、<p>_reservations.pen1@ ...
#                          （<p> = 前缀，<P> = 大写前缀）。下面的单项变量可覆盖派生名。
#   ASSUME_YES             非交互运行时设为 true（跳过确认）
#   AWS_PROFILE            optional
#   PREFIX / TABLE_NAME / FLOW_NAME / GUIDE_FLOW_NAME / QUEUE_GENERAL / QUEUE_SECURITY / QUEUE_RESERVATIONS / QUEUE_LOYALTY /
#   ROUTING_PROFILE_NAME   覆盖派生名（一般不需要）
#   EMAIL_DOMAIN           default <instance-alias>.email.connect.aws (must be an email domain of the instance)
#   EMAIL_LOCAL_PARTS      default reservations,rewards,reservations.pen1,reservations.pen2,reservations.szx1,reservations.hgh1
#                          (base names, must match lambda/data/hotels.json "mailboxes.rules"; the prefix is added)
#   BIND_EMAIL_ADDRESSES   default true - bind the PoC addresses above to the PoC flow
#   HOURS_NAME             default "Basic Hours" (hours of operation used by new queues)
#   AGENT_USERNAMES        optional comma list - these users are MOVED to the routing profile
#   TRIAGE_MODEL_ID        default us.anthropic.claude-haiku-4-5-20251001-v1:0
#   AGENT_MODEL_ID         default us.anthropic.claude-sonnet-4-6
#   GROUNDING_GATE         default true
#   POC_NOW                fixed "now" for demos, e.g. 2026-06-15T10:00:00+08:00 (dummy inventory is Jul-Oct 2026)
#   CHAT_ORIGIN_SENDERS    sender addresses of the live-chat offline-message system (manual handling only)
#   BEDROCK_KB_ID          normally unset: keeps the knowledge-base mode configured by setup_kb.sh
#                          (an id = use that Bedrock Knowledge Base, "none" = local BM25 index)
#   SKIP_SMOKE             default false - set true to skip the post-deploy agent self-tests

set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS_FILE="$HERE/.deploy_settings"
EMAIL_LOCAL_PARTS="${EMAIL_LOCAL_PARTS:-reservations,rewards,reservations.pen1,reservations.pen2,reservations.szx1,reservations.hgh1}"
BIND_EMAIL_ADDRESSES="${BIND_EMAIL_ADDRESSES:-true}"
HOURS_NAME="${HOURS_NAME:-Basic Hours}"
AGENT_USERNAMES="${AGENT_USERNAMES:-}"
TRIAGE_MODEL_ID="${TRIAGE_MODEL_ID:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"
AGENT_MODEL_ID="${AGENT_MODEL_ID:-us.anthropic.claude-sonnet-4-6}"
GROUNDING_GATE="${GROUNDING_GATE:-true}"
POC_NOW="${POC_NOW:-}"
CHAT_ORIGIN_SENDERS="${CHAT_ORIGIN_SENDERS:-}"
# Knowledge base mode is owned by setup_kb.sh: unless BEDROCK_KB_ID is given explicitly ("none" = local index),
# the week scripts keep the value already on the agent Lambda.
BEDROCK_KB_ID_GIVEN="${BEDROCK_KB_ID+yes}"
BEDROCK_KB_ID="${BEDROCK_KB_ID:-}"
SKIP_SMOKE="${SKIP_SMOKE:-false}"
ASSUME_YES="${ASSUME_YES:-false}"
SMOKE_FAILS=0
SUMMARY=()

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
warn() { printf '[%s] WARNING: %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note() { SUMMARY+=("$*"); }

# ---------------------------------------------------------------- interactive settings
lib_interactive() { [[ -t 0 && "$ASSUME_YES" != "true" ]]; }

lib_ask() {  # $1 提示 $2 默认 -> 答案（提示写 stderr，从终端读取）
  local v=""
  if lib_interactive; then read -r -p "$1 [$2]: " v </dev/tty || true; fi
  echo "${v:-$2}"
}

saved_setting() {  # $1 key -> value from .deploy_settings (plain KEY=VALUE lines, never sourced)
  [[ -f "$SETTINGS_FILE" ]] || return 0
  python3 -c "import sys; print(next((l.split('=',1)[1].strip() for l in open(sys.argv[1]) if l.startswith(sys.argv[2]+'=')), ''))" "$SETTINGS_FILE" "$1"
}

save_settings() {
  printf 'NAME_PREFIX=%s\nCONNECT_INSTANCE_ARN=%s\n' "$NAME_PREFIX" "$CONNECT_INSTANCE_ARN" >"$SETTINGS_FILE"
}

ARN_RE='^arn:aws[a-z-]*:connect:([a-z0-9-]+):([0-9]{12}):instance/([a-f0-9-]+)$'

choose_instance() {  # 交互：输入 ARN，或回车后从某个区域的实例列表中选择
  local cur="${CONNECT_INSTANCE_ARN:-$(saved_setting CONNECT_INSTANCE_ARN)}" v region n
  while :; do
    v="$(lib_ask "Connect 实例 ARN（直接回车使用默认；输入 list 从实例列表选择）" "${cur:-list}")"
    if [[ "$v" == "list" ]]; then
      region="$(lib_ask "列出哪个区域的 Connect 实例" "$(aws configure get region 2>/dev/null || echo us-west-2)")"
      aws --no-cli-pager --region "$region" ${AWS_PROFILE:+--profile "$AWS_PROFILE"} connect list-instances --output json >"$WORK/instances.json" \
        || { warn "无法列出 $region 的实例"; continue; }
      python3 -c "
import json,sys
xs=json.load(open(sys.argv[1])).get('InstanceSummaryList',[])
for i,x in enumerate(xs,1): print(f'  {i:>2}) {x[\"InstanceAlias\"]:<32} {x[\"Id\"]}  {x.get(\"InstanceStatus\",\"\")}', file=sys.stderr)
print(len(xs))" "$WORK/instances.json" >"$WORK/n.txt"
      n="$(cat "$WORK/n.txt")"; [[ "$n" -gt 0 ]] || { warn "$region 没有 Connect 实例"; continue; }
      v="$(lib_ask "请选择序号" "1")"
      [[ "$v" =~ ^[0-9]+$ && "$v" -ge 1 && "$v" -le "$n" ]] || { echo "序号无效" >&2; continue; }
      v="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['InstanceSummaryList'][int(sys.argv[2])-1]['Arn'])" "$WORK/instances.json" "$v")"
    fi
    [[ "$v" =~ $ARN_RE ]] && { CONNECT_INSTANCE_ARN="$v"; return; }
    echo "ARN 格式不正确：$v" >&2
  done
}

choose_prefix() {
  local cur="${NAME_PREFIX:-$(saved_setting NAME_PREFIX)}" v
  while :; do
    v="$(lib_ask "资源命名前缀（小写字母开头，小写字母/数字，最多 10 位）" "${cur:-sl}")"
    v="$(echo "$v" | tr '[:upper:]' '[:lower:]')"
    [[ "$v" =~ ^[a-z][a-z0-9]{0,9}$ ]] && { NAME_PREFIX="$v"; return; }
    echo "前缀不合法：$v" >&2
    lib_interactive || die "NAME_PREFIX 不合法：$v"
  done
}

derive_names() {  # explicit env overrides win; everything else comes from NAME_PREFIX
  local up; up="$(echo "$NAME_PREFIX" | tr '[:lower:]' '[:upper:]')"
  PREFIX="${PREFIX:-$NAME_PREFIX-email-ai}"
  TABLE_NAME="${TABLE_NAME:-$NAME_PREFIX-email-ai-poc}"
  FLOW_NAME="${FLOW_NAME:-$up-Email-AI-Inbound}"
  GUIDE_FLOW_NAME="${GUIDE_FLOW_NAME:-$up-Email-AI-AgentGuide}"
  QUEUE_GENERAL="${QUEUE_GENERAL:-$up-Email-General}"
  QUEUE_SECURITY="${QUEUE_SECURITY:-$up-Email-Security}"
  QUEUE_RESERVATIONS="${QUEUE_RESERVATIONS:-$up-Email-Reservations}"
  QUEUE_LOYALTY="${QUEUE_LOYALTY:-$up-Email-Loyalty}"
  ROUTING_PROFILE_NAME="${ROUTING_PROFILE_NAME:-$up-Email-AI-Agents}"
  MAILBOX_PREFIX="${NAME_PREFIX}_"
  TRIAGE_FN="$PREFIX-triage"
  AGENT_FN="$PREFIX-agent"
  ROLE_NAME="$PREFIX-lambda-role"
}

addr() { echo "$MAILBOX_PREFIX$1@$EMAIL_DOMAIN"; }  # $1 base local part -> full PoC address

# ---------------------------------------------------------------- setup
init_env() {
  command -v aws >/dev/null || die "aws CLI not found"
  command -v python3 >/dev/null || die "python3 not found"
  WORK="$(mktemp -d)"; trap '_on_exit' EXIT
  if lib_interactive; then
    printf '\n==== 部署目标%s ====\n' "${WEEK:+（第 $WEEK 周）}" >&2
    [[ "${CONNECT_INSTANCE_ARN:-}" =~ $ARN_RE ]] || choose_instance   # 环境变量已给出则不再询问
    [[ -n "${NAME_PREFIX:-}" ]] || choose_prefix
    NAME_PREFIX="$(echo "$NAME_PREFIX" | tr '[:upper:]' '[:lower:]')"
    [[ "$NAME_PREFIX" =~ ^[a-z][a-z0-9]{0,9}$ ]] || die "NAME_PREFIX 不合法：$NAME_PREFIX"
  else
    CONNECT_INSTANCE_ARN="${CONNECT_INSTANCE_ARN:-$(saved_setting CONNECT_INSTANCE_ARN)}"
    NAME_PREFIX="${NAME_PREFIX:-$(saved_setting NAME_PREFIX)}"
    NAME_PREFIX="${NAME_PREFIX:-sl}"
    [[ "$NAME_PREFIX" =~ ^[a-z][a-z0-9]{0,9}$ ]] || die "NAME_PREFIX 不合法：$NAME_PREFIX"
  fi
  [[ "${CONNECT_INSTANCE_ARN:-}" =~ $ARN_RE ]] \
    || die "Set CONNECT_INSTANCE_ARN=arn:aws:connect:<region>:<account>:instance/<id> (or run in a terminal to choose interactively)"
  REGION="${BASH_REMATCH[1]}"; ACCOUNT="${BASH_REMATCH[2]}"; INSTANCE_ID="${BASH_REMATCH[3]}"
  AWSC=(aws --no-cli-pager --region "$REGION")
  [[ -n "${AWS_PROFILE:-}" ]] && AWSC+=(--profile "$AWS_PROFILE")
  local caller; caller="$("${AWSC[@]}" sts get-caller-identity --query Account --output text)"
  [[ "$caller" == "$ACCOUNT" ]] || die "Credentials are for account $caller, instance is in $ACCOUNT"
  INSTANCE_ALIAS="$("${AWSC[@]}" connect describe-instance --instance-id "$INSTANCE_ID" --query Instance.InstanceAlias --output text)"
  EMAIL_DOMAIN="${EMAIL_DOMAIN:-$INSTANCE_ALIAS.email.connect.aws}"
  derive_names
  local saved_p; saved_p="$(saved_setting NAME_PREFIX)"
  if [[ -n "$saved_p" && "$saved_p" != "$NAME_PREFIX" && "$(saved_setting CONNECT_INSTANCE_ARN)" == "$CONNECT_INSTANCE_ARN" ]]; then
    warn "此实例上次使用的前缀是 $saved_p，本次是 $NAME_PREFIX：将部署一套新的、互不相干的资源。"
  fi
  if lib_interactive && [[ "${SKIP_INIT_CONFIRM:-false}" != "true" ]]; then
    local lps; lps="$(IFS=','; for x in $EMAIL_LOCAL_PARTS; do printf '%s ' "$(addr "$x")"; done)"
    printf '\n  实例         %s（%s，%s，账号 %s）\n  前缀         %s\n  Lambda       %s / %s\n  DynamoDB     %s\n  flow         %s / %s\n  队列         %s / %s / %s / %s\n  路由配置文件 %s\n  邮箱         %s\n\n' \
      "$INSTANCE_ALIAS" "$INSTANCE_ID" "$REGION" "$ACCOUNT" "$NAME_PREFIX" "$TRIAGE_FN" "$AGENT_FN" "$TABLE_NAME" "$FLOW_NAME" "$GUIDE_FLOW_NAME" \
      "$QUEUE_GENERAL" "$QUEUE_SECURITY" "$QUEUE_RESERVATIONS" "$QUEUE_LOYALTY" "$ROUTING_PROFILE_NAME" "$lps" >&2
    local ok; read -r -p "确认部署到以上实例并使用以上名称？[Y/n]: " ok </dev/tty || true
    [[ -z "$ok" || "$ok" =~ ^[YyＹ是] ]] || die "已取消。"
  fi
  save_settings
  log "Instance $INSTANCE_ALIAS ($INSTANCE_ID) in $REGION, prefix $NAME_PREFIX${WEEK:+, PoC week $WEEK, capabilities $CAPABILITIES}"
}

pyj() {  # pyj '<python expr over d>' < json
  python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"
}

# ---------------------------------------------------------------- data storage
ensure_bucket() {  # $1 bucket - create private, encrypted bucket in the instance region if absent
  local b="$1"
  if "${AWSC[@]}" s3api head-bucket --bucket "$b" --expected-bucket-owner "$ACCOUNT" >/dev/null 2>"$WORK/err"; then return; fi
  # Create only on "not found"; 403 means the name is taken by another account or we lack access.
  grep -qE "404|Not Found" "$WORK/err" || { cat "$WORK/err" >&2; die "Bucket $b exists but is not accessible from account $ACCOUNT"; }
  log "Creating S3 bucket $b"
  if [[ "$REGION" == "us-east-1" ]]; then "${AWSC[@]}" s3api create-bucket --bucket "$b" >/dev/null
  else "${AWSC[@]}" s3api create-bucket --bucket "$b" --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null; fi
  "${AWSC[@]}" s3api put-public-access-block --bucket "$b" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  "${AWSC[@]}" s3api put-bucket-encryption --bucket "$b" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
}

storage_count() {  # $1 resource type
  "${AWSC[@]}" connect list-instance-storage-configs --instance-id "$INSTANCE_ID" --resource-type "$1" --output json | pyj 'len(d.get("StorageConfigs",[]))'
}

storage_bucket() {  # $1 resource type -> bucket name of the first config
  "${AWSC[@]}" connect list-instance-storage-configs --instance-id "$INSTANCE_ID" --resource-type "$1" --output json \
    | pyj '(d.get("StorageConfigs") or [{}])[0].get("S3Config",{}).get("BucketName","")'
}

associate_storage() {  # $1 type $2 bucket $3 prefix
  python3 -c "import json,sys; print(json.dumps({'StorageType':'S3','S3Config':{'BucketName':sys.argv[1],'BucketPrefix':sys.argv[2]}}))" "$2" "$3" >"$WORK/storage.json"
  "${AWSC[@]}" connect associate-instance-storage-config --instance-id "$INSTANCE_ID" --resource-type "$1" --storage-config "file://$WORK/storage.json" >/dev/null
}

ensure_email_storage() {  # EMAIL_MESSAGES: required by the triage Lambda to read the email body
  if [[ "$(storage_count EMAIL_MESSAGES)" -ge 1 ]]; then
    note "EMAIL_MESSAGES storage: existing (s3://$(storage_bucket EMAIL_MESSAGES))"; return
  fi
  [[ -n "${EMAIL_BUCKET_NAME:-}" ]] || die "EMAIL_MESSAGES storage is not configured. Set EMAIL_BUCKET_NAME=<new-or-existing bucket in $REGION> and re-run."
  ensure_bucket "$EMAIL_BUCKET_NAME"
  associate_storage EMAIL_MESSAGES "$EMAIL_BUCKET_NAME" "connect-email-messages"
  note "EMAIL_MESSAGES storage: associated s3://$EMAIL_BUCKET_NAME/connect-email-messages"
}

ensure_attachment_storage() {  # ATTACHMENTS + CORS so agents can open attachments in the workspace
  local b
  if [[ "$(storage_count ATTACHMENTS)" -ge 1 ]]; then
    b="$(storage_bucket ATTACHMENTS)"; note "ATTACHMENTS storage: existing (s3://$b)"
  else
    b="${ATTACHMENT_BUCKET_NAME:-$(storage_bucket EMAIL_MESSAGES)}"
    [[ -n "$b" ]] || die "Set ATTACHMENT_BUCKET_NAME (or configure EMAIL_MESSAGES storage first with deploy_week_1.sh)."
    ensure_bucket "$b"
    associate_storage ATTACHMENTS "$b" "connect-attachments"
    note "ATTACHMENTS storage: associated s3://$b/connect-attachments"
  fi
  # Merge (never replace) a CORS rule for the instance's agent workspace origin.
  local origin="https://$INSTANCE_ALIAS.my.connect.aws"
  # Only "no CORS configuration yet" counts as empty; any other error (e.g. AccessDenied) must not lead to an overwrite.
  if ! "${AWSC[@]}" s3api get-bucket-cors --bucket "$b" --output json >"$WORK/cors.json" 2>"$WORK/err"; then
    grep -q "NoSuchCORSConfiguration" "$WORK/err" || { cat "$WORK/err" >&2; die "Cannot read CORS of s3://$b"; }
    echo '{"CORSRules":[]}' >"$WORK/cors.json"
  fi
  if python3 - "$WORK/cors.json" "$origin" <<'PY'
import json, sys
c = json.load(open(sys.argv[1])); o = sys.argv[2]
if any(o in r.get("AllowedOrigins", []) for r in c.get("CORSRules", [])):
    sys.exit(1)
c.setdefault("CORSRules", []).append({"AllowedMethods": ["PUT", "GET"], "AllowedOrigins": [o], "AllowedHeaders": ["*"], "MaxAgeSeconds": 3000})
json.dump({"CORSRules": c["CORSRules"]}, open(sys.argv[1], "w"))
PY
  then
    "${AWSC[@]}" s3api put-bucket-cors --bucket "$b" --cors-configuration "file://$WORK/cors.json"
    note "CORS: added $origin to s3://$b"
  fi
}

# ---------------------------------------------------------------- DynamoDB / IAM / Lambda
ensure_table() {
  if ! "${AWSC[@]}" dynamodb describe-table --table-name "$TABLE_NAME" >/dev/null 2>&1; then
    log "Creating DynamoDB table $TABLE_NAME"
    "${AWSC[@]}" dynamodb create-table --table-name "$TABLE_NAME" \
      --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S \
      --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE \
      --billing-mode PAY_PER_REQUEST --sse-specification Enabled=true --tags Key=Project,Value=email-ai-poc >/dev/null
    "${AWSC[@]}" dynamodb wait table-exists --table-name "$TABLE_NAME"
    "${AWSC[@]}" dynamodb update-continuous-backups --table-name "$TABLE_NAME" \
      --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true >/dev/null
  fi
  TABLE_ARN="$("${AWSC[@]}" dynamodb describe-table --table-name "$TABLE_NAME" --query Table.TableArn --output text)"
}

ensure_role() {
  cat >"$WORK/trust.json" <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
  python3 - "$CONNECT_INSTANCE_ARN" "$TABLE_ARN" "arn:aws:lambda:$REGION:$ACCOUNT:function:$AGENT_FN" "$REGION" "$ACCOUNT" >"$WORK/policy.json" <<'PY'
import json, sys
inst, table, agent, region, acct = sys.argv[1:]
print(json.dumps({"Version": "2012-10-17", "Statement": [
    {"Sid": "Logs", "Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": f"arn:aws:logs:{region}:{acct}:*"},
    {"Sid": "Table", "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:Scan"], "Resource": table},
    {"Sid": "ConnectEmail", "Effect": "Allow", "Action": ["connect:ListContactReferences", "connect:GetAttachedFile", "connect:DescribeContact"], "Resource": [inst, inst + "/*"]},
    {"Sid": "Bedrock", "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:Retrieve"],
     "Resource": ["arn:aws:bedrock:*::foundation-model/anthropic.*", f"arn:aws:bedrock:*:{acct}:inference-profile/*", f"arn:aws:bedrock:{region}:{acct}:knowledge-base/*"]},
    {"Sid": "StartAgent", "Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": agent},
]}))
PY
  if ! "${AWSC[@]}" iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    log "Creating IAM role $ROLE_NAME"
    "${AWSC[@]}" iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "file://$WORK/trust.json" \
      --description "Email AI PoC Lambdas" >/dev/null
    sleep 10  # IAM propagation before Lambda can assume the role
  fi
  # Overwrite-style calls: same input -> same state, so re-runs converge (also repairs manual drift).
  "${AWSC[@]}" iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "file://$WORK/trust.json"
  "${AWSC[@]}" iam put-role-policy --role-name "$ROLE_NAME" --policy-name "$PREFIX-policy" --policy-document "file://$WORK/policy.json"
  ROLE_ARN="$("${AWSC[@]}" iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)"
}

build_package() {  # deterministic zip (fixed timestamps/permissions) so unchanged code has the same CodeSha256
  python3 - "$HERE/lambda" "$WORK/lambda.zip" <<'PY'
import pathlib, sys, zipfile, hashlib, base64
src, dst = pathlib.Path(sys.argv[1]), sys.argv[2]
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
    for p in sorted(src.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix in (".py", ".json"):
            info = zipfile.ZipInfo(p.relative_to(src).as_posix(), (1980, 1, 1, 0, 0, 0))
            info.compress_type, info.create_system, info.external_attr = zipfile.ZIP_DEFLATED, 3, 0o100644 << 16
            z.writestr(info, p.read_bytes())
PY
  CODE_SHA="$(python3 -c "import base64,hashlib,sys; print(base64.b64encode(hashlib.sha256(open(sys.argv[1],'rb').read()).digest()).decode())" "$WORK/lambda.zip")"
}

_env_file() {
  python3 - "$WORK/env.json" <<PY
import json, sys
env = {"GROUNDING_GATE": "$GROUNDING_GATE", "TABLE_NAME": "$TABLE_NAME", "TRIAGE_MODEL_ID": "$TRIAGE_MODEL_ID",
       "AGENT_MODEL_ID": "$AGENT_MODEL_ID", "AGENT_FUNCTION_NAME": "$AGENT_FN", "CHAT_ORIGIN_SENDERS": "$CHAT_ORIGIN_SENDERS",
       "BEDROCK_KB_ID": "$BEDROCK_KB_ID", "POC_NOW": "$POC_NOW", "MAILBOX_PREFIX": "$MAILBOX_PREFIX", "POC_WEEK": "$WEEK", "ENABLED_CAPABILITIES": "$CAPABILITIES"}
json.dump({"Variables": {k: v for k, v in env.items() if v}}, open(sys.argv[1], "w"))
PY
}

_deploy_fn() {  # name handler timeout memory
  local name="$1" handler="$2" timeout="$3" memory="$4" i
  if "${AWSC[@]}" lambda get-function-configuration --function-name "$name" --output json >"$WORK/fn.json" 2>"$WORK/err"; then
    if [[ "$(pyj 'd.get("CodeSha256","")' <"$WORK/fn.json")" != "$CODE_SHA" ]]; then
      log "Updating code of Lambda $name"
      "${AWSC[@]}" lambda update-function-code --function-name "$name" --zip-file "fileb://$WORK/lambda.zip" >/dev/null
      "${AWSC[@]}" lambda wait function-updated --function-name "$name"
    fi
    if ! python3 - "$WORK/fn.json" "$WORK/env.json" "$handler" "$ROLE_ARN" "$timeout" "$memory" <<'PY'
import json, sys
c, env = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
same = (c.get("Handler") == sys.argv[3] and c.get("Runtime") == "python3.12" and c.get("Role") == sys.argv[4]
        and c.get("Timeout") == int(sys.argv[5]) and c.get("MemorySize") == int(sys.argv[6])
        and (c.get("Environment") or {}).get("Variables", {}) == env["Variables"])
sys.exit(0 if same else 1)
PY
    then
      log "Updating configuration of Lambda $name"
      "${AWSC[@]}" lambda update-function-configuration --function-name "$name" --handler "$handler" --runtime python3.12 \
        --role "$ROLE_ARN" --timeout "$timeout" --memory-size "$memory" --environment "file://$WORK/env.json" >/dev/null
    fi
  elif ! grep -q "ResourceNotFoundException" "$WORK/err"; then
    cat "$WORK/err" >&2; die "Cannot read Lambda $name"
  else
    log "Creating Lambda $name"
    for i in 1 2 3 4 5; do
      if "${AWSC[@]}" lambda create-function --function-name "$name" --runtime python3.12 --handler "$handler" \
          --role "$ROLE_ARN" --zip-file "fileb://$WORK/lambda.zip" --timeout "$timeout" --memory-size "$memory" \
          --environment "file://$WORK/env.json" --tags Project=email-ai-poc >/dev/null 2>"$WORK/err"; then
        break
      fi
      if grep -q "cannot be assumed" "$WORK/err" && [[ $i -lt 5 ]]; then sleep $((i * 3)); continue; fi
      cat "$WORK/err" >&2; die "create-function $name failed"
    done
  fi
  "${AWSC[@]}" lambda wait function-updated --function-name "$name"
}

current_agent_env() {  # $1 variable name -> value on the deployed agent Lambda, empty if none
  local v
  v="$("${AWSC[@]}" lambda get-function-configuration --function-name "$AGENT_FN" --query "Environment.Variables.$1" --output text 2>/dev/null || true)"
  [[ "$v" == "None" ]] && v=""
  echo "$v"
}

lambda_exists() { "${AWSC[@]}" lambda get-function-configuration --function-name "$1" >/dev/null 2>&1; }

set_agent_env() {  # $1 name $2 value ("" removes it) - merges into the agent's existing environment, no-op if unchanged
  "${AWSC[@]}" lambda get-function-configuration --function-name "$AGENT_FN" --output json >"$WORK/fn-env.json"
  if python3 - "$WORK/fn-env.json" "$1" "$2" "$WORK/env-new.json" <<'PY'
import json, sys
cur = (json.load(open(sys.argv[1])).get("Environment") or {}).get("Variables", {})
new = dict(cur)
if sys.argv[3]:
    new[sys.argv[2]] = sys.argv[3]
else:
    new.pop(sys.argv[2], None)
json.dump({"Variables": new}, open(sys.argv[4], "w"))
sys.exit(1 if new == cur else 0)
PY
  then
    "${AWSC[@]}" lambda update-function-configuration --function-name "$AGENT_FN" --environment "file://$WORK/env-new.json" >/dev/null
    "${AWSC[@]}" lambda wait function-updated --function-name "$AGENT_FN"
    return 0
  fi
  return 1
}

update_code_if_changed() {  # $1 function - upload $WORK/lambda.zip only when CodeSha256 differs
  local cur
  cur="$("${AWSC[@]}" lambda get-function-configuration --function-name "$1" --query CodeSha256 --output text)"
  [[ "$cur" == "$CODE_SHA" ]] && return 1
  "${AWSC[@]}" lambda update-function-code --function-name "$1" --zip-file "fileb://$WORK/lambda.zip" >/dev/null
  "${AWSC[@]}" lambda wait function-updated --function-name "$1"
}

deployed_week() {
  local w
  w="$("${AWSC[@]}" lambda get-function-configuration --function-name "$AGENT_FN" --query 'Environment.Variables.POC_WEEK' --output text 2>/dev/null || true)"
  [[ "$w" =~ ^[0-9]+$ ]] && echo "$w" || echo 0
}

deploy_lambdas() {  # uses $WEEK and $CAPABILITIES
  local dw; dw="$(deployed_week)"
  if [[ "$dw" -gt "$WEEK" && "${ALLOW_DOWNGRADE:-false}" != "true" ]]; then
    die "The PoC is already at week $dw; re-running week $WEEK would switch capabilities back to $CAPABILITIES. Re-run deploy_week_$dw.sh instead, or set ALLOW_DOWNGRADE=true."
  fi
  if [[ "$BEDROCK_KB_ID_GIVEN" != "yes" ]]; then
    BEDROCK_KB_ID="$(current_agent_env BEDROCK_KB_ID)"
  fi
  [[ "$BEDROCK_KB_ID" == "none" ]] && BEDROCK_KB_ID=""
  if [[ -n "$BEDROCK_KB_ID" ]]; then
    note "Knowledge base: Bedrock Knowledge Base $BEDROCK_KB_ID (change with ./setup_kb.sh)"
  else
    [[ -s "$HERE/lambda/data/kb_index.json" ]] || die "lambda/data/kb_index.json missing - run ./setup_kb.sh first."
    note "Knowledge base: local BM25 index lambda/data/kb_index.json (change with ./setup_kb.sh)"
  fi
  ensure_table
  ensure_role
  build_package
  _env_file
  _deploy_fn "$AGENT_FN" agent.lambda_handler 300 1024
  # The flow polls one agent run per email; async retries would produce duplicate replies.
  "${AWSC[@]}" lambda put-function-event-invoke-config --function-name "$AGENT_FN" --maximum-retry-attempts 0 >/dev/null
  _deploy_fn "$TRIAGE_FN" triage.lambda_handler 8 512
  TRIAGE_ARN="$("${AWSC[@]}" lambda get-function --function-name "$TRIAGE_FN" --query Configuration.FunctionArn --output text)"
  # Resource policy: add only if our statement is missing (errors are no longer swallowed).
  local sid="connect-${INSTANCE_ID:0:8}" pol
  pol="$("${AWSC[@]}" lambda get-policy --function-name "$TRIAGE_FN" --query Policy --output text 2>/dev/null || echo '{}')"
  if ! python3 -c "import json,sys; p=json.loads(sys.argv[1] if sys.argv[1] not in ('','None') else '{}'); sys.exit(0 if any(s.get('Sid')==sys.argv[2] for s in p.get('Statement',[])) else 1)" "$pol" "$sid"; then
    "${AWSC[@]}" lambda add-permission --function-name "$TRIAGE_FN" --statement-id "$sid" \
      --action lambda:InvokeFunction --principal connect.amazonaws.com --source-account "$ACCOUNT" \
      --source-arn "$CONNECT_INSTANCE_ARN" >/dev/null
  fi
  if ! "${AWSC[@]}" connect list-lambda-functions --instance-id "$INSTANCE_ID" --output json \
      | python3 -c "import json,sys; sys.exit(0 if sys.argv[1] in json.load(sys.stdin).get('LambdaFunctions',[]) else 1)" "$TRIAGE_ARN"; then
    "${AWSC[@]}" connect associate-lambda-function --instance-id "$INSTANCE_ID" --function-arn "$TRIAGE_ARN"
  fi
  note "Lambdas: $TRIAGE_FN + $AGENT_FN, POC_WEEK=$WEEK, ENABLED_CAPABILITIES=$CAPABILITIES"
}

require_week() {  # $1 previous week - warn if the deployed Lambdas are older
  local cur; cur="$(deployed_week)"
  if [[ "$cur" -eq 0 ]]; then
    warn "$AGENT_FN is not deployed yet - run deploy_week_1.sh first (continuing: this script re-applies the base setup)."
  elif [[ "$cur" -lt "$1" ]]; then
    warn "Deployed PoC is at week $cur, expected at least week $1. Weeks are cumulative; continuing."
  fi
}

# ---------------------------------------------------------------- email addresses
_load_emails() {  # SearchEmailAddresses has no CLI auto-pagination: follow NextToken so existing addresses are never missed
  local token="" page=0
  echo '{"EmailAddresses":[]}' >"$WORK/emails.json"
  while :; do
    page=$((page + 1))
    if [[ -n "$token" ]]; then
      "${AWSC[@]}" connect search-email-addresses --instance-id "$INSTANCE_ID" --max-results 100 --next-token "$token" --output json >"$WORK/emails-page.json"
    else
      "${AWSC[@]}" connect search-email-addresses --instance-id "$INSTANCE_ID" --max-results 100 --output json >"$WORK/emails-page.json"
    fi
    token="$(python3 - "$WORK/emails.json" "$WORK/emails-page.json" <<'PY'
import json, sys
acc, page = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
acc["EmailAddresses"] += page.get("EmailAddresses", [])
json.dump(acc, open(sys.argv[1], "w"))
print(page.get("NextToken") or "")
PY
)"
    [[ -n "$token" && $page -lt 50 ]] || break
  done
}

unique_id() {  # $1 json file $2 list key $3 name field $4 name $5 id field -> id, empty if absent, die if duplicated
  python3 - "$@" <<'PY'
import json, sys
f, key, field, name, idf = sys.argv[1:]
m = [x for x in json.load(open(f)).get(key, []) if x.get(field) == name]
if len(m) > 1:
    sys.exit(f"ERROR: {len(m)} resources named '{name}' in {key} - rename or delete the duplicates, the script will not guess.")
print(m[0][idf] if m else "")
PY
}

email_id_for() {  # $1 base local part (prefix added) -> id or empty
  python3 -c "import json,sys; a=sys.argv[1].lower(); print(next((e['EmailAddressId'] for e in json.load(open(sys.argv[2])).get('EmailAddresses',[]) if e['EmailAddress'].lower()==a), ''))" \
    "$(addr "$1")" "$WORK/emails.json"
}

ensure_email_addresses() {
  _load_emails
  local lp created=""
  IFS=',' read -r -a _LPS <<<"$EMAIL_LOCAL_PARTS"
  for lp in ${_LPS[@]+"${_LPS[@]}"}; do
    [[ -n "$lp" ]] || continue
    if [[ -z "$(email_id_for "$lp")" ]]; then
      log "Creating email address $(addr "$lp")"
      "${AWSC[@]}" connect create-email-address --instance-id "$INSTANCE_ID" --email-address "$(addr "$lp")" \
        --display-name "Email AI PoC ($lp)" --description "Email AI PoC mailbox" >/dev/null
      created="$created $lp"
    fi
  done
  _load_emails
  note "Email addresses: ${MAILBOX_PREFIX}{$EMAIL_LOCAL_PARTS}@$EMAIL_DOMAIN${created:+ (created:$created)}"
}

# ---------------------------------------------------------------- queues / routing profile
_list_queues() { "${AWSC[@]}" connect list-queues --instance-id "$INSTANCE_ID" --queue-types STANDARD --output json >"$WORK/queues.json"; }

queue_field() {  # $1 name $2 Id|Arn -> value or empty (dies on duplicate names instead of creating another queue)
  unique_id "$WORK/queues.json" QueueSummaryList Name "$1" "$2"
}

ensure_queue() {  # $1 name $2 outbound-email local part (agents reply from this address)
  local name="$1" lp="$2" hours qid eid
  _list_queues
  eid="$(email_id_for "$lp")"
  qid="$(queue_field "$name" Id)"
  if [[ -z "$qid" ]]; then
    hours="$("${AWSC[@]}" connect list-hours-of-operations --instance-id "$INSTANCE_ID" --output json \
      | python3 -c "import json,sys; m=[h['Id'] for h in json.load(sys.stdin)['HoursOfOperationSummaryList'] if h['Name']==sys.argv[1]]; print(m[0] if m else '')" "$HOURS_NAME")"
    [[ -n "$hours" ]] || die "Hours of operation '$HOURS_NAME' not found (set HOURS_NAME)."
    log "Creating queue $name"
    if [[ -n "$eid" ]]; then
      "${AWSC[@]}" connect create-queue --instance-id "$INSTANCE_ID" --name "$name" --hours-of-operation-id "$hours" \
        --description "Email AI PoC" --outbound-email-config "OutboundEmailAddressId=$eid" >/dev/null
    else
      "${AWSC[@]}" connect create-queue --instance-id "$INSTANCE_ID" --name "$name" --hours-of-operation-id "$hours" \
        --description "Email AI PoC" >/dev/null
    fi
    _list_queues; qid="$(queue_field "$name" Id)"
  elif [[ -n "$eid" ]]; then
    local cur; cur="$("${AWSC[@]}" connect describe-queue --instance-id "$INSTANCE_ID" --queue-id "$qid" --query 'Queue.OutboundEmailConfig.OutboundEmailAddressId' --output text)"
    if [[ "$cur" == "None" || -z "$cur" ]]; then
      "${AWSC[@]}" connect update-queue-outbound-email-config --instance-id "$INSTANCE_ID" --queue-id "$qid" --outbound-email-config "OutboundEmailAddressId=$eid"
    fi
  fi
  [[ -n "$eid" ]] || warn "Queue $name has no outbound email address ($(addr "$lp") not found) - agents cannot reply from it."
  note "Queue $name (agent replies from $(addr "$lp"))"
}

ensure_routing_profile() {  # $@ queue names to serve on the EMAIL channel
  _list_queues
  local rp first qid q
  "${AWSC[@]}" connect list-routing-profiles --instance-id "$INSTANCE_ID" --output json >"$WORK/rps.json"
  rp="$(unique_id "$WORK/rps.json" RoutingProfileSummaryList Name "$ROUTING_PROFILE_NAME" Id)"
  if [[ -n "$rp" ]]; then
    # An existing profile (e.g. ROUTING_PROFILE_NAME pointed at a shared one) is not modified; only warn if it lacks Email.
    "${AWSC[@]}" connect describe-routing-profile --instance-id "$INSTANCE_ID" --routing-profile-id "$rp" --output json \
      | python3 -c "import json,sys; sys.exit(0 if any(m['Channel']=='EMAIL' and m.get('Concurrency',0)>0 for m in json.load(sys.stdin)['RoutingProfile'].get('MediaConcurrencies',[])) else 1)" \
      || warn "Routing profile $ROUTING_PROFILE_NAME has no EMAIL channel - agents on it will not receive emails. Enable Email on it in the console."
  else
    first="$(queue_field "$1" Id)"
    log "Creating routing profile $ROUTING_PROFILE_NAME (Email channel)"
    rp="$("${AWSC[@]}" connect create-routing-profile --instance-id "$INSTANCE_ID" --name "$ROUTING_PROFILE_NAME" \
      --description "Email AI PoC agents" --default-outbound-queue-id "$first" \
      --media-concurrencies 'Channel=EMAIL,Concurrency=5' --query RoutingProfileId --output text)"
  fi
  "${AWSC[@]}" connect list-routing-profile-queues --instance-id "$INSTANCE_ID" --routing-profile-id "$rp" --output json >"$WORK/rpq.json"
  for q in "$@"; do
    qid="$(queue_field "$q" Id)"; [[ -n "$qid" ]] || continue
    if ! python3 -c "import json,sys; sys.exit(0 if any(c['QueueId']==sys.argv[1] and c['Channel']=='EMAIL' for c in json.load(open(sys.argv[2])).get('RoutingProfileQueueConfigSummaryList',[])) else 1)" "$qid" "$WORK/rpq.json"; then
      "${AWSC[@]}" connect associate-routing-profile-queues --instance-id "$INSTANCE_ID" --routing-profile-id "$rp" \
        --queue-configs "QueueReference={QueueId=$qid,Channel=EMAIL},Priority=1,Delay=0"
    fi
  done
  local u uid
  IFS=',' read -r -a _USERS <<<"$AGENT_USERNAMES"
  for u in ${_USERS[@]+"${_USERS[@]}"}; do
    [[ -n "$u" ]] || continue
    [[ -s "$WORK/users.json" ]] || "${AWSC[@]}" connect list-users --instance-id "$INSTANCE_ID" --output json >"$WORK/users.json"
    uid="$(unique_id "$WORK/users.json" UserSummaryList Username "$u" Id)"
    if [[ -n "$uid" ]]; then
      if [[ "$("${AWSC[@]}" connect describe-user --instance-id "$INSTANCE_ID" --user-id "$uid" --query User.RoutingProfileId --output text)" != "$rp" ]]; then
        "${AWSC[@]}" connect update-user-routing-profile --instance-id "$INSTANCE_ID" --user-id "$uid" --routing-profile-id "$rp"
        note "User $u moved to routing profile $ROUTING_PROFILE_NAME"
      fi
    else
      warn "User $u not found"
    fi
  done
  note "Routing profile $ROUTING_PROFILE_NAME serves: $*"
}

# ---------------------------------------------------------------- flow
_upsert_flow() {  # $1 flow name, $2 rendered content file, $3 description -> UPSERT_FLOW_ID (create, or update only if changed)
  local id
  "${AWSC[@]}" connect list-contact-flows --instance-id "$INSTANCE_ID" --contact-flow-types CONTACT_FLOW --output json >"$WORK/flows.json"
  id="$(unique_id "$WORK/flows.json" ContactFlowSummaryList Name "$1" Id)"
  if [[ -z "$id" ]]; then
    log "Creating flow $1"
    id="$("${AWSC[@]}" connect create-contact-flow --instance-id "$INSTANCE_ID" --name "$1" --type CONTACT_FLOW \
      --description "$3" --content "file://$2" --status PUBLISHED \
      --query ContactFlowId --output text)"
  else
    "${AWSC[@]}" connect describe-contact-flow --instance-id "$INSTANCE_ID" --contact-flow-id "$id" --output json >"$WORK/flow-cur.json"
    if python3 - "$WORK/flow-cur.json" "$2" <<'PY'
import json, sys
cur = json.load(open(sys.argv[1]))["ContactFlow"]
try:
    same = cur.get("Status") == "PUBLISHED" and json.loads(cur.get("Content") or "{}") == json.load(open(sys.argv[2], encoding="utf-8"))
except json.JSONDecodeError:
    same = False
sys.exit(0 if same else 1)
PY
    then
      log "Flow $1 unchanged"
    else
      log "Updating flow $1 ($id)"
      "${AWSC[@]}" connect update-contact-flow-content --instance-id "$INSTANCE_ID" --contact-flow-id "$id" --content "file://$2"
    fi
  fi
  UPSERT_FLOW_ID="$id"
}

publish_agent_guide_flow() {  # agent guide (Show view, AWS managed Detail view) started by the inbound flow's DefaultAgentUI event hook
  python3 - "$HERE/flow/sl_email_agent_guide_flow.json" "$WORK/guide-flow.json" "$CONNECT_INSTANCE_ARN" "$REGION" <<'PY'
import json, sys
src, dst, instance_arn, region = sys.argv[1:]
partition = instance_arn.split(":")[1]
rep = {"__DETAIL_VIEW_ARN__": f"arn:{partition}:connect:{region}:aws:view/detail:1"}
def render(n):
    if isinstance(n, dict):
        return {k: render(v) for k, v in n.items()}
    if isinstance(n, list):
        return [render(v) for v in n]
    return rep.get(n, n) if isinstance(n, str) else n
flow = render(json.load(open(src, encoding="utf-8")))
assert "__DETAIL_VIEW_ARN__" not in json.dumps(flow), "unrendered placeholder __DETAIL_VIEW_ARN__"
json.dump(flow, open(dst, "w", encoding="utf-8"), ensure_ascii=False)
PY
  _upsert_flow "$GUIDE_FLOW_NAME" "$WORK/guide-flow.json" "Email AI PoC agent guide - Detail view of triage / AI agent contact attributes"
  GUIDE_FLOW_ID="$UPSERT_FLOW_ID"
  GUIDE_FLOW_ARN="$CONNECT_INSTANCE_ARN/contact-flow/$GUIDE_FLOW_ID"
  note "Agent guide flow $GUIDE_FLOW_NAME ($GUIDE_FLOW_ID): Show view (Detail) of the triage / AI agent attributes in the agent workspace - agents' security profile needs 'Agent Applications - Custom views - All'"
}

publish_flow() {  # team queues that do not exist yet fall back to the General queue
  publish_agent_guide_flow
  _list_queues
  local gen_arn; gen_arn="$(queue_field "$QUEUE_GENERAL" Arn)"
  [[ -n "$gen_arn" ]] || die "Queue $QUEUE_GENERAL missing - run deploy_week_1.sh."
  python3 - "$HERE/flow/sl_email_inbound_flow.json" "$WORK/flow.json" "$TRIAGE_ARN" "$WORK/queues.json" \
    "$QUEUE_GENERAL" "$QUEUE_SECURITY" "$QUEUE_RESERVATIONS" "$QUEUE_LOYALTY" "$GUIDE_FLOW_ARN" "$GUIDE_FLOW_NAME" >"$WORK/flowqueues.txt" <<'PY'
import json, sys
src, dst, triage, qfile, gen, sec, res, loyalty, guide_arn, guide_name = sys.argv[1:]
queues = {q["Name"]: q["Arn"] for q in json.load(open(qfile))["QueueSummaryList"] if q.get("Name")}
rep, used = {"__TRIAGE_LAMBDA_ARN__": triage, "__AGENT_GUIDE_FLOW_ARN__": guide_arn, "__AGENT_GUIDE_FLOW_NAME__": guide_name}, []
for key, name in (("GENERAL", gen), ("SECURITY", sec), ("RESERVATIONS", res), ("LOYALTY", loyalty)):
    real = name if name in queues else gen
    rep[f"__QUEUE_{key}_NAME__"], rep[f"__QUEUE_{key}_ARN__"] = real, queues[real]
    used.append(f"{key.lower()}->{real}")
def render(n):
    if isinstance(n, dict):
        return {k: render(v) for k, v in n.items()}
    if isinstance(n, list):
        return [render(v) for v in n]
    return rep.get(n, n) if isinstance(n, str) else n
flow = render(json.load(open(src, encoding="utf-8")))
left = [k for k in rep if k in json.dumps(flow)]
assert not left, f"unrendered placeholders {left}"
json.dump(flow, open(dst, "w", encoding="utf-8"), ensure_ascii=False)
print(" ".join(used))
PY
  _upsert_flow "$FLOW_NAME" "$WORK/flow.json" "Email AI PoC inbound email flow"
  FLOW_ID="$UPSERT_FLOW_ID"
  note "Flow $FLOW_NAME ($FLOW_ID) queues: $(cat "$WORK/flowqueues.txt")"
}

bind_email_addresses() {
  [[ "$BIND_EMAIL_ADDRESSES" == "true" ]] || { note "Email addresses NOT bound (BIND_EMAIL_ADDRESSES=false)"; return; }
  local lp eid cur
  IFS=',' read -r -a _LPS <<<"$EMAIL_LOCAL_PARTS"
  for lp in ${_LPS[@]+"${_LPS[@]}"}; do
    eid="$(email_id_for "$lp")"; [[ -n "$eid" ]] || continue
    cur="$("${AWSC[@]}" connect get-flow-association --instance-id "$INSTANCE_ID" --resource-id "$eid" --resource-type INBOUND_EMAIL --query FlowId --output text 2>/dev/null || true)"
    [[ "$cur" == "$FLOW_ID" ]] && continue
    if [[ -n "$cur" && "$cur" != "None" ]]; then
      log "Re-binding $(addr "$lp") from flow $cur to $FLOW_NAME"
      "${AWSC[@]}" connect disassociate-flow --instance-id "$INSTANCE_ID" --resource-id "$eid" --resource-type INBOUND_EMAIL
    fi
    "${AWSC[@]}" connect associate-flow --instance-id "$INSTANCE_ID" --resource-id "$eid" --resource-type INBOUND_EMAIL --flow-id "$FLOW_ID"
  done
  note "Flow bound to: ${MAILBOX_PREFIX}{$EMAIL_LOCAL_PARTS}@$EMAIL_DOMAIN"
}

# ---------------------------------------------------------------- self-test
# smoke <label> <mailbox local part> <subject> <body> <python check over r (agent result)>
# Calls the agent Lambda directly (no email is sent); rows use case_id SMOKE-* and are excluded from reports.
smoke() {
  [[ "$SKIP_SMOKE" == "true" ]] && return
  local label="$1" lp="$2" subject="$3" body="$4" check="$5" id
  id="SMOKE-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:10].upper())')"
  SMOKE_RAN=1
  python3 - "$id" "$(addr "$lp")" "$subject" "$body" "$lp" >"$WORK/smoke-in.json" <<'PY'
import json, sys, datetime
cid, sysaddr, subj, body, lp = sys.argv[1:]
lp = lp.lower()  # base local part (without the resource prefix)
mailbox = "LOYALTY" if lp.startswith(("rewards", "loyalty")) else ("HOTEL" if lp.startswith("reservations.") else "CXC")
print(json.dumps({"contact_id": cid, "case_id": cid, "instance_id": "smoke", "contact_arn": "smoke", "subject": subj, "body": body,
                  "attachments": [], "customer_email": "smoke.test@example.com", "customer_name": "", "system_address": sysaddr,
                  "mailbox": mailbox, "email_type": "NEW", "ai_mode": "AUTO", "triage": {},
                  "received_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}, ensure_ascii=False))
PY
  log "Self-test: $label"
  if ! "${AWSC[@]}" lambda invoke --function-name "$AGENT_FN" --cli-read-timeout 320 \
      --payload "fileb://$WORK/smoke-in.json" "$WORK/smoke-out.json" >"$WORK/smoke-meta.json" 2>"$WORK/err"; then
    cat "$WORK/err" >&2; SMOKE_FAILS=$((SMOKE_FAILS + 1)); note "SELF-TEST FAIL  $label (invoke error)"; return
  fi
  if python3 - "$WORK/smoke-out.json" "$check" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
if "errorMessage" in r:
    print("   Lambda error:", r["errorMessage"]); sys.exit(1)
tools = [c["tool"] for c in r.get("tool_calls", [])]
actions = (r.get("attributes") or {}).get("actions", "")
print(f"   disposition={r.get('disposition')} tools={tools} actions={actions} reason={(r.get('attributes') or {}).get('escalation_reason')}")
# names go into globals: eval() locals are invisible inside generator expressions such as any(t in tools for t in ...)
sys.exit(0 if eval(sys.argv[2], {"r": r, "tools": tools, "actions": actions, "disp": r.get("disposition")}) else 1)
PY
  then note "self-test PASS  $label"
  else SMOKE_FAILS=$((SMOKE_FAILS + 1)); note "SELF-TEST FAIL  $label"; fi
}

_on_exit() {  # also clean self-test data when the script stops half-way
  if [[ "${SMOKE_RAN:-0}" == 1 && "${SMOKE_CLEANED:-0}" == 0 ]]; then cleanup_smoke || warn "self-test cleanup failed"; fi
  rm -rf "$WORK"
}

cleanup_smoke() {  # remove everything the self-tests wrote, so re-runs leave no trace (incl. the week-3 test booking)
  [[ "${SMOKE_RAN:-0}" == 1 ]] || return 0
  SMOKE_CLEANED=1
  "${AWSC[@]}" dynamodb scan --table-name "$TABLE_NAME" --projection-expression "pk, sk" \
    --filter-expression "begins_with(pk, :c) OR begins_with(pk, :t) OR begins_with(case_id, :s) OR pk = :u" \
    --expression-attribute-values '{":c":{"S":"CASE#SMOKE-"},":t":{"S":"CONTACT#SMOKE-"},":s":{"S":"SMOKE-"},":u":{"S":"CUSTOMER#smoke.test@example.com"}}' \
    --output json >"$WORK/smoke-items.json"
  local n=0 key
  while IFS= read -r key; do
    [[ -n "$key" ]] || continue
    "${AWSC[@]}" dynamodb delete-item --table-name "$TABLE_NAME" --key "$key"
    n=$((n + 1))
  done < <(python3 -c "import json,sys; [print(json.dumps({'pk': i['pk'], 'sk': i['sk']})) for i in json.load(open(sys.argv[1])).get('Items', [])]" "$WORK/smoke-items.json")
  log "Self-test data removed ($n items)"
}

finish() {
  cleanup_smoke
  printf '\n========== PoC week %s deployed ==========\n' "$WEEK"
  local s; for s in ${SUMMARY[@]+"${SUMMARY[@]}"}; do printf '  - %s\n' "$s"; done
  printf '\nScenarios enabled this week:\n%s\n' "$1"
  printf '\nManual checks this week:\n%s\n' "$2"
  [[ "$SMOKE_FAILS" -eq 0 ]] || { printf '\n%s self-test(s) failed - check CloudWatch logs of %s.\n' "$SMOKE_FAILS" "$AGENT_FN"; exit 2; }
}
