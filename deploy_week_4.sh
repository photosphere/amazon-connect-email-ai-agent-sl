#!/usr/bin/env bash
# 说明与本周验收清单：README_week_4.md
# PoC week 4 - Focus 3 (Example Rewards Account), Focus 4 (Earn & Redeem), Focus 5 (Report Generation)
# + full regression.
#
#   POC_NOW=2026-06-15T10:00:00+08:00 ./deploy_week_4.sh   # 交互确认实例与前缀（默认沿用第 1 周的选择）
#
# Scenarios enabled (all capabilities KB, RSV_READ, RSV_WRITE, LOYALTY):
#   示例会员俱乐部账户（3 项身份验证 + 风险分级）· 积分赚取与兑换（Cash & Points）· 报表导出（POC Report 11 项）
# Member data: Customer Profile A/B/C from POC Testing Data (lambda/data/seed_data.json).
# High-risk profile changes (e.g. date of birth) are verified by the AI and then wait for Member Services in the LOYALTY queue.
# Week-specific env: REPORT_DIR (default ./poc-reports). Common env: see deploy_lib.sh.
WEEK=4
CAPABILITIES="KB,RSV_READ,RSV_WRITE,LOYALTY"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy_lib.sh"
init_env
require_week 3

ensure_email_storage
ensure_email_addresses
deploy_lambdas

# LOYALTY queue: agents reply from rewards@ (high-risk LOYALTY changes, verification locked after 3 failures).
ensure_queue "$QUEUE_GENERAL" reservations
ensure_queue "$QUEUE_SECURITY" reservations
ensure_queue "$QUEUE_RESERVATIONS" reservations
ensure_queue "$QUEUE_LOYALTY" rewards
ensure_routing_profile "$QUEUE_GENERAL" "$QUEUE_SECURITY" "$QUEUE_RESERVATIONS" "$QUEUE_LOYALTY"
publish_flow
bind_email_addresses

# Self-tests (read-only: no profile or points change).
smoke "Focus 4 - 750 points on room rate (zh-TW)" rewards "積分使用" \
  "您好，我有750點積分將於年底到期，可以用來支付房價嗎？謝謝。" \
  '"not enabled" not in str((r.get("attributes") or {}).get("escalation_reason")) and any(t in tools for t in ("quote_cash_and_points", "search_knowledge_base"))'
smoke "Focus 3 - DOB change asks for verification" rewards "Wrong date of birth" \
  "I noticed the date of birth under my membership is updated wrongly. Can you change it for me? My membership number is 223422345569." \
  '"not enabled" not in str((r.get("attributes") or {}).get("escalation_reason")) and "update_loyalty_profile" not in tools'

# Focus 5: export the 11 POC Report CSVs (self-test rows SMOKE-* are excluded).
REPORT_DIR="${REPORT_DIR:-./poc-reports}"
REPORT_ARGS=(--table "$TABLE_NAME" --region "$REGION" --out "$REPORT_DIR")
if python3 -c "import boto3" 2>/dev/null; then
  if python3 "$HERE/reports/export_reports.py" "${REPORT_ARGS[@]}"; then
    note "Reports: 11 CSV + summary.csv in $REPORT_DIR"
  else
    warn "Report export failed"; note "Reports: export FAILED"
  fi
else
  note "Reports: boto3 not installed locally - run: python3 reports/export_reports.py ${REPORT_ARGS[*]}"
fi

finish "  示例会员俱乐部账户（Focus 3）· 积分兑换（Focus 4）· 报表导出（Focus 5）（+ 第 1-3 周全部场景）" \
"  - Focus 3 in two emails from wx.tan@gmail.com: 3 criteria -> VERIFIED -> DOB change HUMAN_REVIEW in $QUEUE_LOYALTY.
  - 3 wrong verification attempts in one case: locked, transferred to $QUEUE_LOYALTY.
  - Full regression with the evaluation email set (all 17 scenarios), then re-run the report export."
