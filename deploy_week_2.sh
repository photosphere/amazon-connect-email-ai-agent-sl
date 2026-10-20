#!/usr/bin/env bash
# 说明与本周验收清单：README_week_2.md
# PoC week 2 - Area 2 read-only scenarios + human escalation + attachments.
#
#   POC_NOW=2026-06-15T10:00:00+08:00 ./deploy_week_2.sh   # 交互确认实例与前缀（默认沿用第 1 周的选择）  # 重跑时不设置 POC_NOW 会沿用 Lambda 上的值
#
# Scenarios enabled (capabilities KB, RSV_READ):
#   询价报价 / 暂不预订（No Book Pending Decision）· 订单确认与特殊要求（加床、早餐、提前入住、航班备注）
#   多问题邮件：能答先答，其余升级人工 · 投诉 / 情绪 / 要求人工 · 附件读取（PDF / 图片）
# New bookings, amendments, cancellations (week 3) and Example Rewards (week 4) still go to a human with an AI draft.
#
# Data used: POC Testing Data rows 7-10 (lambda/data/hotels.json) and seed bookings 12345678910 / 12345678909
# (lambda/data/seed_data.json) - bundled in the Lambda package.
# Week-specific env: ATTACHMENT_BUCKET_NAME (default: the EMAIL_MESSAGES bucket). Common env: see deploy_lib.sh.
WEEK=2
CAPABILITIES="KB,RSV_READ"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy_lib.sh"
init_env
require_week 1

# 1. Attachments: storage + CORS so the AI can read PDFs/images and agents can open them.
ensure_email_storage
ensure_attachment_storage
ensure_email_addresses

# 2. Lambdas with reservation lookup / quotation / booking remarks.
resolve_lambda_env   # POC_NOW not set in the shell keeps the value already on the Lambdas
[[ -n "$POC_NOW" ]] || warn "POC_NOW not set in the shell or on the deployed Lambdas (or set to empty) - quotations for Jul-Oct 2026 will return PAST_DATE."
deploy_lambdas

# 3. Reservations queue (Area 2 escalations, e.g. no inventory, partly answered emails).
ensure_queue "$QUEUE_GENERAL" reservations
ensure_queue "$QUEUE_SECURITY" reservations
ensure_queue "$QUEUE_RESERVATIONS" reservations
ensure_routing_profile "$QUEUE_GENERAL" "$QUEUE_SECURITY" "$QUEUE_RESERVATIONS"

# 4. Re-publish the flow so the RESERVATIONS team branch uses the new queue.
publish_flow
bind_email_addresses

# 5. Self-tests (read-only: no booking is changed).
smoke "Area 2 quote - Lakeview 2A2C asks child ages" reservations "Lakeview room rate" \
  "Hi, can you quote me the rate for Lakeview room? Check in date is 1 July 2026 for 2 nights. We have 2 adults and 2 children in total. Thanks" \
  '"check_availability" in tools'
smoke "New booking not enabled yet -> escalate" reservations "Please book" \
  "Please book a Garden View Room at Lakeside Hotel, 1-3 July 2026, 2 adults, flexible rate, under Mr. Smoke Test." \
  'disp == "ESCALATE" and "create_reservation" not in tools'

finish "  询价报价 · 订单确认与特殊要求 · 多问题邮件部分升级 · 投诉/要求人工 · 附件读取（+ 第 1 周场景）" \
"  - Sample 2 / Sample 3 emails (confirmation numbers 12345678910 / 12345678909) from any mailbox: the AI asks for
    contact verification unless sent from the address on the seed booking (test.kim@example.com / david.peeters@example.com).
  - Sample 4 (July 2027): no inventory -> HUMAN_REVIEW in $QUEUE_RESERVATIONS.
  - A complaint and an email with a PDF attachment: both reach an agent with the AI draft in contact attribute reply_body."
