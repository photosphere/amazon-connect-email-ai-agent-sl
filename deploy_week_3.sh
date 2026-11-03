#!/usr/bin/env bash
# 说明与本周验收清单：README_week_3.md
# PoC week 3 - Area 2 write scenarios: create, amend and cancel reservations.
#
#   POC_NOW=2026-11-15T10:00:00+08:00 ./deploy_week_3.sh   # 交互确认实例与前缀（默认沿用第 1 周的选择）
#
# Scenarios enabled (capabilities KB, RSV_READ, RSV_WRITE):
#   预订：查房 + 下单 + 回顾（Check Available Book）· 改单 · 取消订单（Cancel Booking FIT）
# Reservations are written to the DynamoDB table (simulated PMS) so later emails find them (General Logic #1).
# To connect the real PMS / Open API instead, replace _load_reservation / _save_reservation in lambda/tools.py.
# Example Rewards (week 4) still goes to a human with an AI draft. Common env: see deploy_lib.sh.
WEEK=3
CAPABILITIES="KB,RSV_READ,RSV_WRITE"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy_lib.sh"
init_env
require_week 2

ensure_email_storage
ensure_email_addresses
resolve_lambda_env
[[ -n "$POC_NOW" ]] || warn "POC_NOW not set in the shell or on the deployed Lambdas - bookings for Dec 2026 - Apr 2027 are rejected as PAST_DATE; the booking self-test is skipped."
deploy_lambdas

ensure_queue "$QUEUE_GENERAL" reservations
ensure_queue "$QUEUE_SECURITY" reservations
ensure_queue "$QUEUE_RESERVATIONS" reservations
ensure_routing_profile "$QUEUE_GENERAL" "$QUEUE_SECURITY" "$QUEUE_RESERVATIONS"
publish_flow
bind_email_addresses

# Self-tests. The booking test creates one reservation for guest "Smoke Test" (smoke.test@example.com).
if [[ -n "$POC_NOW" ]]; then
  smoke "Area 2 booking - create reservation + recap" reservations.hgh1 "Booking request" \
    "Please book a Garden View Room at Lakeside Hotel Hangzhou, check-in 1 December 2026, check-out 3 December 2026, 2 adults, no children, 1 room, flexible rate. Guest name: Mr. Smoke Test. Thank you." \
    '"CREATE_RESERVATION" in actions'
fi
smoke "Example Rewards not enabled yet -> escalate" rewards "Points" \
  "Hi, my membership number is 223422345569. How many points do I have and can I use 750 points on a room?" \
  'disp == "ESCALATE" and "verify_loyalty_member" not in tools'

finish "  预订下单 · 改单（AP 不可改）· 取消（免费 / 罚金需确认 / 减免转人工 / 在住转前台）（+ 第 1-2 周场景）" \
"  - Sample 1 in two emails (child ages in the reply): 3 confirmation numbers and total incl. taxes.
  - Sample 5 example 1 from megan.lim@example.com (seed booking 12312312312): cancellation number returned.
    Note: this really cancels the seed booking in the table; delete item pk=RES#12312312312 to reset it.
  - Booking 11100000003 (Advance Purchase) amendment from charlie.white@gmail.com: refused, no change."
