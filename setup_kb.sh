#!/usr/bin/env bash
# 说明：README_kb.md
# 配置 Example Hotel Email AI PoC 的知识库（中文交互式，可重复执行）。
#
#   ./setup_kb.sh
#
# 两种方式（二选一，随时可以再执行本脚本切换）：
#   1) 本地索引（默认）：pypdf 抽取 PDF 文本 -> lambda/data/kb_index.json，Lambda 内 BM25 检索。无额外 AWS 资源。
#   2) Amazon Bedrock Knowledge Base：脱敏后的文本 + 元数据上传 S3 -> Bedrock KB（S3 Vectors 向量库）-> Lambda 调 Retrieve。
#
# 脱敏：文档内容、文件名、目录 -> 酒店代码映射都经过 ../anonymize_map.json（PoC 目录之外，含真实名称，勿随 PoC 分发）。
# 找不到该文件时会提示，确认后才会用未脱敏的内容继续。原始 PDF 不会上传到 AWS。
#
# 非交互运行（CI 等）用环境变量提供答案，并设置 ASSUME_YES=true：
#   KB_DOC_DIR            知识库文档目录，默认 "00_Vendor KB (pdf)"（相对 PoC 上一级目录）
#   KB_MODE               local | bedrock，默认 local
#   CONNECT_INSTANCE_ARN  已部署 Lambda 的 Connect 实例（bedrock 模式必填；local 模式可选，用于更新 Lambda）
#   KB_NAME / KB_BUCKET / KB_VECTOR_BUCKET / KB_INDEX   Bedrock 资源名
#   KB_EMBED_MODEL        cohere.embed-multilingual-v3（默认）| amazon.titan-embed-text-v2:0
#   ANONYMIZE_MAP         脱敏规则文件，默认 ../anonymize_map.json
#   KB_FOLDER_MAP         额外的 "子目录=代码" 映射，逗号分隔，例如 "Example Hotel Kuala Lumpur=SLKL"
#   KB_VENV               本地模式 pypdf 虚拟环境，默认 ~/.cache/email-ai-kbenv
WEEK=""
CAPABILITIES=""
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy_lib.sh"
ROOT="$(cd "$HERE/.." && pwd)"
ASSUME_YES="${ASSUME_YES:-false}"
SKIP_INIT_CONFIRM=true   # 本脚本有自己的确认步骤；init_env 只询问资源前缀
INTERACTIVE=false; [[ -t 0 && "$ASSUME_YES" != "true" ]] && INTERACTIVE=true

ask() {  # $1 提示 $2 默认值 -> 答案（提示写到 stderr）
  local v=""
  if $INTERACTIVE; then read -r -p "$1 [$2]: " v </dev/tty || true; fi  # /dev/tty: also works inside while-read loops
  echo "${v:-$2}"
}
confirm() {  # $1 提示 $2 默认 y|n -> 0 = 是。非交互时：默认 y 的问题需要 ASSUME_YES=true，默认 n 的问题一律为否
  local v
  if ! $INTERACTIVE; then [[ "$2" == "y" && "$ASSUME_YES" == "true" ]]; return; fi
  read -r -p "$1 [$([[ "$2" == y ]] && echo Y/n || echo y/N)]: " v </dev/tty || true
  v="$(echo "${v:-$2}" | tr '[:upper:]' '[:lower:]')"
  [[ "$v" == y || "$v" == yes || "$v" == 是 ]]
}
title() { printf '\n==== %s ====\n' "$*"; }

title "Example Hotel Email AI PoC 知识库配置"
echo "本脚本把知识库文档整理成 AI 可检索的知识库，并（可选）更新已部署的 Lambda。可以重复执行。"

# ---------------------------------------------------------------- 1. 文档目录
title "1/4 知识库文档目录"
while :; do
  DOC_DIR="$(ask "请输入知识库文档目录（相对 $ROOT 或绝对路径）" "${KB_DOC_DIR:-00_Vendor KB (pdf)}")"
  DOC_DIR="${DOC_DIR/#\~/$HOME}"
  [[ "$DOC_DIR" = /* ]] || DOC_DIR="$ROOT/$DOC_DIR"
  if [[ -d "$DOC_DIR" ]] && find "$DOC_DIR" -iname '*.pdf' -print -quit | grep -q .; then break; fi
  echo "目录不存在或其中没有 PDF：$DOC_DIR" >&2
  $INTERACTIVE || exit 1
done
DOC_DIR="$(cd "$DOC_DIR" && pwd)"
echo "文档目录：$DOC_DIR"

# 子目录 -> 酒店代码（检索时按酒店过滤）。未知子目录询问代码。
MAP_ARGS=()
IFS=',' read -r -a _M <<<"${KB_FOLDER_MAP:-}"
for m in ${_M[@]+"${_M[@]}"}; do [[ -n "$m" ]] && MAP_ARGS+=(--map "$m"); done
folder_code() {  # $1 子目录名 -> 代码（脱敏规则文件的 folders + KB_FOLDER_MAP），未知为空
  python3 - "$HERE/tools" "$1" "${KB_FOLDER_MAP:-}" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from build_kb_index import folder_codes  # pypdf is imported lazily, not needed here
extra = dict(m.split("=", 1) for m in sys.argv[3].split(",") if "=" in m)
print((extra.get(sys.argv[2]) or folder_codes().get(sys.argv[2], "")).strip().upper())
PY
}
MAP_FILE="$(python3 -c "import sys; sys.path.insert(0, sys.argv[1]); import anonymize; print(anonymize.map_path())" "$HERE/tools")"
if [[ -f "$MAP_FILE" ]]; then
  echo "脱敏规则：$MAP_FILE"
else
  warn "未找到脱敏规则文件 $MAP_FILE：生成的知识库会保留文档中的真实名称。"
  confirm "仍然继续？" n || die "已取消。请提供脱敏规则文件（或设置 ANONYMIZE_MAP）后重跑。"
fi
printf '%-28s %-8s %s\n' "子目录" "代码" "PDF 数"
while IFS= read -r -d '' sub; do
  name="$(basename "$sub")"
  n="$(find "$sub" -iname '*.pdf' | wc -l | tr -d ' ')"
  [[ "$n" -gt 0 ]] || continue
  code="$(folder_code "$name")"
  if [[ -z "$code" ]]; then
    code="$(ask "  新子目录「${name}」对应的代码（酒店代码，如 SLKL；通用文档填 GENERAL）" GENERAL | tr '[:lower:]' '[:upper:]')"
    MAP_ARGS+=(--map "$name=$code")
    [[ "$code" =~ ^(HGH1|SZX1|PEN1|PEN2|LOYALTY|GENERAL|BOOKING)$ ]] || \
      warn "代码 $code 不在 lambda/kb.py 的 SHARED_SCOPES 和 hotels.json 中：检索不会按该酒店过滤，需同步补充这两处。"
  fi
  printf '%-28s %-8s %s\n' "$name" "$code" "$n"
done < <(find "$DOC_DIR" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)

# ---------------------------------------------------------------- 2. 方式
title "2/4 检索方式"
echo "  1) 本地索引（默认）：Lambda 内 BM25 检索，无额外 AWS 资源与费用；图片型 PDF 无法检索"
echo "  2) Amazon Bedrock Knowledge Base：S3 + S3 Vectors 向量检索（语义检索；只上传脱敏文本，图片型 PDF 同样无法检索）"
case "$(ask "请选择 1 或 2" "$([[ "${KB_MODE:-local}" == bedrock ]] && echo 2 || echo 1)")" in
  2|bedrock) MODE=bedrock ;;
  *) MODE=local ;;
esac
echo "方式：$MODE"

# ---------------------------------------------------------------- 3. Connect 实例 / Lambda
title "3/4 部署目标"
while [[ -z "${CONNECT_INSTANCE_ARN:-}" ]] || ! [[ "$CONNECT_INSTANCE_ARN" =~ $ARN_RE ]]; do
  [[ -n "${CONNECT_INSTANCE_ARN:-}" ]] && echo "ARN 格式不正确：$CONNECT_INSTANCE_ARN" >&2
  CONNECT_INSTANCE_ARN="$(ask "Connect 实例 ARN（本地方式可留空，只生成索引不更新 Lambda）" "$(saved_setting CONNECT_INSTANCE_ARN)")"
  [[ -z "$CONNECT_INSTANCE_ARN" ]] && break
  $INTERACTIVE || [[ "$CONNECT_INSTANCE_ARN" =~ $ARN_RE ]] || die "CONNECT_INSTANCE_ARN 格式不正确"
done
export CONNECT_INSTANCE_ARN
if [[ -n "$CONNECT_INSTANCE_ARN" ]]; then
  init_env
  if lambda_exists "$AGENT_FN"; then HAVE_LAMBDA=true; else HAVE_LAMBDA=false; warn "未找到 ${AGENT_FN}：本次只准备知识库，运行 deploy_week_1.sh 时会一并部署。"; fi
else
  [[ "$MODE" == local ]] || die "Bedrock Knowledge Base 方式需要 Connect 实例 ARN（用于确定区域、账号和要更新的 Lambda）。"
  WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
  HAVE_LAMBDA=false
fi

# ---------------------------------------------------------------- 4a. 本地索引
pypdf_python() {  # -> 可 import pypdf 的 python（必要时询问后安装到虚拟环境）
  local venv="${KB_VENV:-$HOME/.cache/email-ai-kbenv}"
  if python3 -c "import pypdf" 2>/dev/null; then echo python3; return; fi
  if [[ ! -x "$venv/bin/python" ]] || ! "$venv/bin/python" -c "import pypdf" 2>/dev/null; then
    confirm "需要把 pypdf==5.1.0 安装到虚拟环境 ${venv}，继续？" y || die "已取消。"
    { python3 -m venv "$venv" && "$venv/bin/pip" install -q pypdf==5.1.0; } >&2 || die "安装 pypdf 失败"
  fi
  echo "$venv/bin/python"
}

build_local() {
  local py; py="$(pypdf_python)"
  log "从 PDF 生成本地索引（脱敏）"
  "$py" "$HERE/tools/build_kb_index.py" --kb-dir "$DOC_DIR" ${MAP_ARGS[@]+"${MAP_ARGS[@]}"} --out "$HERE/lambda/data"
  local chunks gaps
  chunks="$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$HERE/lambda/data/kb_index.json")"
  gaps="$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$HERE/lambda/data/kb_gaps.json")"
  note "本地索引：$chunks 个段落；$gaps 个 PDF 无可抽取文本（见 lambda/data/kb_gaps.json）"
  if $HAVE_LAMBDA; then
    build_package
    local f; for f in "$AGENT_FN" "$TRIAGE_FN"; do update_code_if_changed "$f" && note "已更新 $f 代码（新索引）" || note "$f 代码无变化"; done
    local cur; cur="$(current_agent_env BEDROCK_KB_ID)"
    if [[ -n "$cur" ]]; then
      if confirm "Lambda 当前使用 Bedrock Knowledge Base ${cur}，切换回本地索引？" y; then
        set_agent_env BEDROCK_KB_ID "" && note "Lambda 已切换为本地索引（Bedrock KB $cur 保留未删除）"
      else
        note "Lambda 仍使用 Bedrock KB ${cur}（新本地索引已打包，仅作备用）"
      fi
    fi
  fi
}

# ---------------------------------------------------------------- 4b. Bedrock Knowledge Base
bedrock_list() {  # $1 json $2 list key $3 name -> id / die on duplicates
  python3 - "$@" <<'PY'
import json, sys
f, key, name, idk = sys.argv[1:]
m = [x for x in json.load(open(f)).get(key, []) if x.get("name") == name]
if len(m) > 1:
    sys.exit(f"ERROR: 有 {len(m)} 个名为 {name} 的资源，请先处理重复项。")
print(m[0][idk] if m else "")
PY
}

build_bedrock() {
  local kb_name bucket vbucket index embed dim parse=false parse_model=""
  kb_name="$(ask "Knowledge Base 名称" "${KB_NAME:-$PREFIX-kb}")"
  bucket="$(ask "存放文档的 S3 桶（不存在则创建）" "${KB_BUCKET:-$PREFIX-kb-$ACCOUNT-$REGION}")"
  vbucket="$(ask "S3 Vectors 向量桶" "${KB_VECTOR_BUCKET:-$PREFIX-kb-vectors-$ACCOUNT}")"
  index="$(ask "向量索引名" "${KB_INDEX:-$PREFIX-kb-index}")"
  echo "  嵌入模型：1) cohere.embed-multilingual-v3（默认，中英繁多语言）  2) amazon.titan-embed-text-v2:0"
  case "$(ask "请选择嵌入模型" "$([[ "${KB_EMBED_MODEL:-}" == amazon.titan-embed-text-v2:0 ]] && echo 2 || echo 1)")" in
    2|amazon.titan-embed-text-v2:0) embed=amazon.titan-embed-text-v2:0 ;;
    *) embed=cohere.embed-multilingual-v3 ;;
  esac
  dim=1024
  # 只上传脱敏后的文本：图片型 PDF（无可抽取文本）不入库，列在 lambda/data/kb_gaps.json，需 OCR 并脱敏后再加入。

  title "确认"
  printf '  文档目录   %s\n  区域       %s\n  KB 名称    %s\n  文档桶     s3://%s/kb/\n  向量库     %s / %s（S3 Vectors，%s 维，cosine）\n  嵌入模型   %s\n  图片解析   %s\n' \
    "$DOC_DIR" "$REGION" "$kb_name" "$bucket" "$vbucket" "$index" "$dim" "$embed" "否（只上传脱敏文本）"
  echo "  将创建（已存在则复用）：S3 桶、S3 Vectors 桶和索引、IAM 角色 $PREFIX-kb-role、Knowledge Base、数据源，并启动同步。"
  echo "  注意：同步会让 s3://$bucket/kb/ 与文档目录保持一致（目录里删除的文件会从该前缀删除）。"
  confirm "开始？" y || die "已取消。"

  # 文档 + 元数据（hotel_code 用于按酒店过滤），固定元数据文件时间戳，s3 sync 只上传有变化的文件
  local py; py="$(pypdf_python)"
  "$py" "$HERE/tools/build_kb_index.py" --kb-dir "$DOC_DIR" ${MAP_ARGS[@]+"${MAP_ARGS[@]}"} --export-text "$WORK/stage/kb" --manifest "$WORK/manifest.json"
  python3 - "$WORK/manifest.json" <<'PY'
import json, os, pathlib, sys
for d in json.load(open(sys.argv[1])):
    txt = pathlib.Path(d["path"])
    meta = txt.with_name(txt.name + ".metadata.json")
    meta.write_text(json.dumps({"metadataAttributes": {"hotel_code": d["hotel_code"], "category": d["category"], "source": d["source"]}}, ensure_ascii=False), encoding="utf-8")
    for f in (txt, meta):  # fixed mtime: s3 sync then only uploads files whose content (size) changed
        os.utime(f, (315532800, 315532800))
PY
  ensure_bucket "$bucket"
  log "同步文档到 s3://$bucket/kb/"
  "${AWSC[@]}" s3 sync "$WORK/stage/kb" "s3://$bucket/kb" --delete --only-show-errors
  note "文档：$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$WORK/manifest.json") 份脱敏文本（+ 元数据）已同步到 s3://$bucket/kb/"

  # S3 Vectors：AMAZON_BEDROCK_TEXT / AMAZON_BEDROCK_METADATA 设为不可过滤，避免超过可过滤元数据上限
  if ! "${AWSC[@]}" s3vectors get-vector-bucket --vector-bucket-name "$vbucket" >/dev/null 2>"$WORK/err"; then
    grep -q NotFoundException "$WORK/err" || { cat "$WORK/err" >&2; die "无法读取向量桶 $vbucket"; }
    log "创建 S3 Vectors 桶 $vbucket"
    "${AWSC[@]}" s3vectors create-vector-bucket --vector-bucket-name "$vbucket" >/dev/null
  fi
  if "${AWSC[@]}" s3vectors get-index --vector-bucket-name "$vbucket" --index-name "$index" --output json >"$WORK/index.json" 2>"$WORK/err"; then
    [[ "$(pyj 'd["index"]["dimension"]' <"$WORK/index.json")" == "$dim" ]] || die "索引 $index 的维度与嵌入模型（${dim}）不一致，请换一个索引名。"
  else
    grep -q NotFoundException "$WORK/err" || { cat "$WORK/err" >&2; die "无法读取索引 $index"; }
    log "创建向量索引 $index"
    "${AWSC[@]}" s3vectors create-index --vector-bucket-name "$vbucket" --index-name "$index" --data-type float32 \
      --dimension "$dim" --distance-metric cosine \
      --metadata-configuration '{"nonFilterableMetadataKeys":["AMAZON_BEDROCK_TEXT","AMAZON_BEDROCK_METADATA"]}' >/dev/null
    "${AWSC[@]}" s3vectors get-index --vector-bucket-name "$vbucket" --index-name "$index" --output json >"$WORK/index.json"
  fi
  local index_arn; index_arn="$(pyj 'd["index"]["indexArn"]' <"$WORK/index.json")"

  # Knowledge Base 服务角色（与控制台快速创建的信任策略一致）
  local role="$PREFIX-kb-role" embed_arn="arn:aws:bedrock:$REGION::foundation-model/$embed"
  python3 - "$ACCOUNT" "$REGION" >"$WORK/kb-trust.json" <<'PY'
import json, sys
a, r = sys.argv[1:]
print(json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "bedrock.amazonaws.com"}, "Action": "sts:AssumeRole",
      "Condition": {"StringEquals": {"aws:SourceAccount": a}, "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{r}:{a}:knowledge-base/*"}}}]}))
PY
  python3 - "$embed_arn" "$bucket" "$index_arn" "$parse" "$parse_model" "$ACCOUNT" <<'PY' >"$WORK/kb-policy.json"
import json, sys
embed, bucket, index, parse, pmodel, acct = sys.argv[1:]
models = [embed]
if parse == "true":
    base = pmodel.split(".", 1)[1] if pmodel.split(".")[0] in ("us", "eu", "apac", "global") else pmodel
    models += [f"arn:aws:bedrock:*::foundation-model/{base}", f"arn:aws:bedrock:*:{acct}:inference-profile/{pmodel}"]
print(json.dumps({"Version": "2012-10-17", "Statement": [
    {"Sid": "Models", "Effect": "Allow", "Action": ["bedrock:InvokeModel"], "Resource": models},
    {"Sid": "List", "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": [f"arn:aws:s3:::{bucket}"]},
    {"Sid": "Read", "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{bucket}/kb/*"]},
    {"Sid": "Vectors", "Effect": "Allow", "Action": ["s3vectors:PutVectors", "s3vectors:GetVectors", "s3vectors:DeleteVectors", "s3vectors:QueryVectors", "s3vectors:GetIndex"], "Resource": [index]}]}))
PY
  if ! "${AWSC[@]}" iam get-role --role-name "$role" >/dev/null 2>&1; then
    log "创建 IAM 角色 $role"
    "${AWSC[@]}" iam create-role --role-name "$role" --assume-role-policy-document "file://$WORK/kb-trust.json" --description "Example Hotel Email AI PoC Bedrock Knowledge Base" >/dev/null
    sleep 10
  fi
  "${AWSC[@]}" iam update-assume-role-policy --role-name "$role" --policy-document "file://$WORK/kb-trust.json"
  "${AWSC[@]}" iam put-role-policy --role-name "$role" --policy-name "$PREFIX-kb-policy" --policy-document "file://$WORK/kb-policy.json"
  local role_arn; role_arn="$("${AWSC[@]}" iam get-role --role-name "$role" --query Role.Arn --output text)"

  # Knowledge Base
  "${AWSC[@]}" bedrock-agent list-knowledge-bases --output json >"$WORK/kbs.json"
  KB_ID="$(bedrock_list "$WORK/kbs.json" knowledgeBaseSummaries "$kb_name" knowledgeBaseId)"
  if [[ -n "$KB_ID" ]]; then
    "${AWSC[@]}" bedrock-agent get-knowledge-base --knowledge-base-id "$KB_ID" --output json >"$WORK/kb.json"
    [[ "$(pyj 'd["knowledgeBase"]["knowledgeBaseConfiguration"]["vectorKnowledgeBaseConfiguration"]["embeddingModelArn"]' <"$WORK/kb.json")" == "$embed_arn" ]] \
      || die "已有 KB $kb_name 使用的嵌入模型与本次选择不同（创建后不能修改），请换一个 KB 名称。"
  else
    log "创建 Knowledge Base $kb_name"
    local i
    for i in 1 2 3 4 5 6; do
      if KB_ID="$("${AWSC[@]}" bedrock-agent create-knowledge-base --name "$kb_name" --role-arn "$role_arn" \
          --description "Example Hotel Email AI PoC hotel / LOYALTY knowledge base" \
          --knowledge-base-configuration "{\"type\":\"VECTOR\",\"vectorKnowledgeBaseConfiguration\":{\"embeddingModelArn\":\"$embed_arn\"}}" \
          --storage-configuration "{\"type\":\"S3_VECTORS\",\"s3VectorsConfiguration\":{\"indexArn\":\"$index_arn\"}}" \
          --query knowledgeBase.knowledgeBaseId --output text 2>"$WORK/err")"; then break; fi
      if [[ $i -lt 6 ]] && grep -qiE "assume|not authorized|AccessDenied" "$WORK/err"; then sleep $((i * 5)); continue; fi  # IAM propagation
      cat "$WORK/err" >&2
      die "创建 Knowledge Base 失败。若为 AccessDenied：确认执行者有 bedrock:CreateKnowledgeBase 和对 $role 的 iam:PassRole，并检查 SCP / 权限边界。已创建的桶、向量索引、角色可保留，修复后重跑本脚本即可。"
    done
  fi
  local st
  for i in $(seq 1 60); do
    st="$("${AWSC[@]}" bedrock-agent get-knowledge-base --knowledge-base-id "$KB_ID" --query knowledgeBase.status --output text)"
    [[ "$st" == ACTIVE ]] && break
    [[ "$st" == FAILED ]] && die "Knowledge Base 状态 FAILED"
    sleep 5
  done
  note "Knowledge Base：${kb_name}（${KB_ID}），S3 Vectors 索引 $index"

  # 数据源：固定大小分块；可选 Claude 解析
  local ds_name="$PREFIX-kb-docs" ds
  "${AWSC[@]}" bedrock-agent list-data-sources --knowledge-base-id "$KB_ID" --output json >"$WORK/dss.json"
  ds="$(bedrock_list "$WORK/dss.json" dataSourceSummaries "$ds_name" dataSourceId)"
  python3 - "$parse" "$parse_model" "$REGION" "$ACCOUNT" >"$WORK/ingest.json" <<'PY'
import json, sys
parse, model, region, acct = sys.argv[1:]
cfg = {"chunkingConfiguration": {"chunkingStrategy": "FIXED_SIZE", "fixedSizeChunkingConfiguration": {"maxTokens": 400, "overlapPercentage": 20}}}
if parse == "true":
    arn = f"arn:aws:bedrock:{region}:{acct}:inference-profile/{model}" if model.split(".")[0] in ("us", "eu", "apac", "global") else f"arn:aws:bedrock:{region}::foundation-model/{model}"
    cfg["parsingConfiguration"] = {"parsingStrategy": "BEDROCK_FOUNDATION_MODEL", "bedrockFoundationModelConfiguration": {"modelArn": arn}}
print(json.dumps(cfg))
PY
  if [[ -z "$ds" ]]; then
    log "创建数据源 $ds_name"
    ds="$("${AWSC[@]}" bedrock-agent create-data-source --knowledge-base-id "$KB_ID" --name "$ds_name" --data-deletion-policy DELETE \
      --data-source-configuration "{\"type\":\"S3\",\"s3Configuration\":{\"bucketArn\":\"arn:aws:s3:::$bucket\",\"inclusionPrefixes\":[\"kb/\"]}}" \
      --vector-ingestion-configuration "file://$WORK/ingest.json" --query dataSource.dataSourceId --output text)"
  else
    "${AWSC[@]}" bedrock-agent get-data-source --knowledge-base-id "$KB_ID" --data-source-id "$ds" --output json >"$WORK/ds.json"
    python3 -c "import json,sys; d=json.load(open(sys.argv[1]))['dataSource'].get('vectorIngestionConfiguration',{}); sys.exit(0 if ('parsingConfiguration' in d)==(sys.argv[2]=='true') else 1)" "$WORK/ds.json" "$parse" \
      || warn "已有数据源的图片解析设置与本次选择不同；分块/解析在创建后不能修改，如需更改请删除数据源 $ds_name 后重跑。"
  fi

  # 同步（只处理新增/修改/删除的文档）
  log "启动同步（Ingestion），文档较多时需要几分钟"
  local job; job="$("${AWSC[@]}" bedrock-agent start-ingestion-job --knowledge-base-id "$KB_ID" --data-source-id "$ds" --query ingestionJob.ingestionJobId --output text)"
  local deadline=$(( $(date +%s) + ${KB_INGEST_TIMEOUT:-1800} ))
  while :; do
    "${AWSC[@]}" bedrock-agent get-ingestion-job --knowledge-base-id "$KB_ID" --data-source-id "$ds" --ingestion-job-id "$job" --output json >"$WORK/job.json"
    st="$(pyj 'd["ingestionJob"]["status"]' <"$WORK/job.json")"
    [[ "$st" == COMPLETE || "$st" == FAILED || "$st" == STOPPED ]] && break
    [[ $(date +%s) -lt $deadline ]] || { warn "同步超时，仍在后台进行（job ${job}）"; break; }
    printf '.'; sleep 15
  done
  echo
  python3 - "$WORK/job.json" <<'PY'
import json, sys
j = json.load(open(sys.argv[1]))["ingestionJob"]; s = j.get("statistics", {})
print(f"  同步状态 {j['status']}：扫描 {s.get('numberOfDocumentsScanned', 0)}，新增 {s.get('numberOfNewDocumentsIndexed', 0)}，"
      f"修改 {s.get('numberOfModifiedDocumentsIndexed', 0)}，删除 {s.get('numberOfDocumentsDeleted', 0)}，失败 {s.get('numberOfDocumentsFailed', 0)}")
for r in (j.get("failureReasons") or [])[:5]:
    print("   -", r[:300])
PY
  [[ "$st" == COMPLETE ]] || warn "同步未成功完成（${st}），检查上面的失败原因。"
  note "同步：${st}（job ${job}）"

  # 检索验证：按酒店过滤
  "${AWSC[@]}" bedrock-agent-runtime retrieve --knowledge-base-id "$KB_ID" --retrieval-query text="What time does the swimming pool open and where is it?" \
    --retrieval-configuration '{"vectorSearchConfiguration":{"numberOfResults":3,"filter":{"equals":{"key":"hotel_code","value":"HGH1"}}}}' \
    --output json >"$WORK/ret.json" 2>"$WORK/err" || { cat "$WORK/err" >&2; warn "检索测试失败"; }
  [[ -s "$WORK/ret.json" ]] && python3 - "$WORK/ret.json" <<'PY'
import json, sys
rs = json.load(open(sys.argv[1])).get("retrievalResults", [])
print(f"  检索测试（HGH1 泳池）：{len(rs)} 条结果")
for r in rs:
    m = r.get("metadata") or {}
    print(f"   - {r.get('score', 0):.3f} {m.get('source')} p{m.get('x-amz-bedrock-kb-document-page-number', '?')}")
PY

  if $HAVE_LAMBDA; then
    build_package
    local f; for f in "$AGENT_FN" "$TRIAGE_FN"; do update_code_if_changed "$f" && note "已更新 $f 代码" || true; done
    set_agent_env BEDROCK_KB_ID "$KB_ID" && note "Lambda 已切换为 Bedrock Knowledge Base $KB_ID" || note "Lambda 已在使用 Bedrock KB $KB_ID"
  else
    note "部署 Lambda 后执行：BEDROCK_KB_ID=$KB_ID ./deploy_week_1.sh，或部署后再运行本脚本"
  fi
}

title "4/4 执行"
if [[ "$MODE" == local ]]; then build_local; else build_bedrock; fi

printf '\n========== 知识库配置完成 ==========\n'
for s in ${SUMMARY[@]+"${SUMMARY[@]}"}; do printf '  - %s\n' "$s"; done
echo "  之后执行 deploy_week_N.sh 会保留当前的知识库方式；切换方式请再次运行 ./setup_kb.sh。"
