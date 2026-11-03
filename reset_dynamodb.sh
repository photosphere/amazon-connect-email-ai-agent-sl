#!/usr/bin/env bash
# 清空 PoC DynamoDB 表中的全部数据（只删除条目，表本身、索引、TTL 等设置保留）。
#
#   ./reset_dynamodb.sh
#
# 交互式询问区域（默认 us-west-2）和表名（默认 sl-email-ai-poc），列出条目数量与按 pk 前缀的分布，
# 可选先把全部条目导出到本地 JSON 备份，然后要求输入表名确认，再分批（每批 25 条）删除并复核剩余数量。
# 可选环境变量：AWS_PROFILE
#
# 注意：删除期间如果有新邮件进入，Lambda 会继续写入新条目；需要彻底清空时请在没有测试邮件进入时运行。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REGION="us-west-2"
DEFAULT_TABLE="sl-email-ai-poc"

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
ask() { local v=""; read -r -p "$1 [$2]: " v </dev/tty || true; echo "${v:-$2}"; }

{ exec 3</dev/tty; } 2>/dev/null || die "需要在终端中交互运行（读取 /dev/tty 失败）。"
exec 3<&-
command -v aws >/dev/null || die "未找到 aws CLI。"
command -v python3 >/dev/null || die "未找到 python3。"

REGION="$(ask "DynamoDB 所在区域" "$DEFAULT_REGION")"
TABLE="$(ask "要清空的表名" "$DEFAULT_TABLE")"
AWSC=(aws --no-cli-pager --region "$REGION" --output json)
[[ -n "${AWS_PROFILE:-}" ]] && AWSC+=(--profile "$AWS_PROFILE")

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

ACCOUNT="$("${AWSC[@]}" sts get-caller-identity --query Account --output text)" || die "无法获取 AWS 身份，请检查凭证。"
"${AWSC[@]}" dynamodb describe-table --table-name "$TABLE" >"$WORK/table.json" 2>"$WORK/err" \
  || die "无法读取表 ${TABLE}（区域 ${REGION}）：$(cat "$WORK/err")"

# Key attributes (e.g. pk / sk) -> projection expression and attribute-name map for the key-only scan.
python3 - "$WORK/table.json" "$WORK/proj.txt" "$WORK/names.json" <<'PY'
import json, sys
keys = [k["AttributeName"] for k in json.load(open(sys.argv[1]))["Table"]["KeySchema"]]
names = {f"#k{i}": k for i, k in enumerate(keys)}
open(sys.argv[2], "w").write(",".join(names))
json.dump(names, open(sys.argv[3], "w"))
PY
PROJ="$(cat "$WORK/proj.txt")"
NAMES="$(cat "$WORK/names.json")"

log "扫描表 $TABLE 的全部主键 ..."
"${AWSC[@]}" dynamodb scan --table-name "$TABLE" --projection-expression "$PROJ" \
  --expression-attribute-names "$NAMES" >"$WORK/keys.json"

TOTAL="$(python3 - "$WORK/keys.json" "$WORK/table.json" "$WORK/by_prefix.txt" <<'PY'
import json, sys
from collections import Counter
items = json.load(open(sys.argv[1])).get("Items", [])
pk = json.load(open(sys.argv[2]))["Table"]["KeySchema"][0]["AttributeName"]
by = Counter()
for it in items:
    v = str(next(iter(it.get(pk, {}).values()), ""))
    by[v.split("#", 1)[0] + "#" if "#" in v else "(无 # 前缀)"] += 1
with open(sys.argv[3], "w") as f:
    for prefix, n in sorted(by.items(), key=lambda x: (-x[1], x[0])):
        f.write(f"            {prefix:<16} {n}\n")
print(len(items))
PY
)"

echo
echo "  账号:   $ACCOUNT"
echo "  区域:   $REGION"
echo "  表:     $TABLE"
echo "  条目数: $TOTAL"
if [[ "$TOTAL" != "0" ]]; then
  echo "  按 pk 前缀:"
  cat "$WORK/by_prefix.txt"
fi
echo
if [[ "$TOTAL" == "0" ]]; then
  log "表已为空，无需删除。"
  exit 0
fi

ans="$(ask "删除前先把全部条目导出到本地 JSON 备份？(Y/n)" "Y")"
if [[ "$ans" =~ ^[YyＹ是] ]]; then
  mkdir -p "$HERE/ddb-backups"
  BACKUP="$HERE/ddb-backups/${TABLE}-${REGION}-$(date '+%Y%m%d-%H%M%S').json"
  log "导出全部条目到 $BACKUP ..."
  "${AWSC[@]}" dynamodb scan --table-name "$TABLE" >"$BACKUP"
  log "备份完成（$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("Items", [])))' "$BACKUP") 条，DynamoDB JSON 格式）。"
fi

echo
read -r -p "此操作不可撤销。请输入表名 $TABLE 以确认删除全部 $TOTAL 条数据: " confirm </dev/tty || true
[[ "$confirm" == "$TABLE" ]] || die "输入不匹配，已取消，未删除任何数据。"

# Split the keys into batch-write-item request files of 25 DeleteRequests each.
mkdir -p "$WORK/batches"
python3 - "$WORK/keys.json" "$TABLE" "$WORK/batches" <<'PY'
import json, sys
items = json.load(open(sys.argv[1])).get("Items", [])
table, out = sys.argv[2], sys.argv[3]
for n, i in enumerate(range(0, len(items), 25)):
    reqs = [{"DeleteRequest": {"Key": k}} for k in items[i:i + 25]]
    json.dump({table: reqs}, open(f"{out}/{n:06d}.json", "w"))
PY

BATCHES=("$WORK"/batches/*.json)
log "开始删除：${#BATCHES[@]} 批，每批最多 25 条 ..."
done_batches=0
for f in "${BATCHES[@]}"; do
  for attempt in 1 2 3 4 5 6 7 8; do
    "${AWSC[@]}" dynamodb batch-write-item --request-items "file://$f" >"$WORK/resp.json"
    left="$(python3 - "$WORK/resp.json" "$f" <<'PY'
import json, sys
un = json.load(open(sys.argv[1])).get("UnprocessedItems") or {}
n = sum(len(v) for v in un.values())
if n:
    json.dump(un, open(sys.argv[2], "w"))  # retry only the unprocessed requests
print(n)
PY
)"
    [[ "$left" == "0" ]] && break
    [[ "$attempt" == 8 ]] && die "批次 $(basename "$f") 重试 8 次后仍有 $left 条未删除，请稍后重新运行本脚本。"
    sleep $((attempt * attempt))
  done
  done_batches=$((done_batches + 1))
  if (( done_batches % 20 == 0 || done_batches == ${#BATCHES[@]} )); then
    log "  已完成 $done_batches / ${#BATCHES[@]} 批"
  fi
done

REMAIN="$("${AWSC[@]}" dynamodb scan --table-name "$TABLE" --select COUNT --query Count --output text)"
if [[ "$REMAIN" == "0" ]]; then
  log "完成：表 $TABLE 已清空（删除 $TOTAL 条）。"
else
  log "删除了 $TOTAL 条，但表中现有 $REMAIN 条：多半是删除期间 Lambda 新写入的数据，可再运行一次本脚本。"
fi
