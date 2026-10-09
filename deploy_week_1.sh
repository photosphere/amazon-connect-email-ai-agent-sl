#!/usr/bin/env bash
# 说明与本周验收清单：README_week_1.md
# PoC week 1 - environment + email-channel foundation + Focus 1 (Hotel General Information).
#
#   ./deploy_week_1.sh        # 中文交互：选择 Connect 实例（输入 ARN 或从列表选）、输入资源前缀（默认 sl），确认后部署
#
#   非交互：CONNECT_INSTANCE_ARN=arn:aws:connect:<region>:<acct>:instance/<id> NAME_PREFIX=sl ASSUME_YES=true \
#           EMAIL_BUCKET_NAME=<only if EMAIL_MESSAGES storage is not configured yet> \
#           POC_NOW=2026-06-15T10:00:00+08:00 ./deploy_week_1.sh </dev/null
#
# 前缀 sl 时：Lambda sl-email-ai-*、表 sl-email-ai-poc、flow SL-Email-AI-Inbound、队列 SL-Email-*、
# 邮箱 sl_reservations@ / sl_rewards@ / sl_reservations.pen1@ / sl_reservations.pen2@ / sl_reservations.szx1@ / sl_reservations.hgh1@。
# 选择保存在 .deploy_settings，第 2-4 周和 setup_kb.sh 默认沿用。
#
# Scenarios enabled (capability KB):
#   新邮件 / 回复邮件识别与多轮上下文 · 安全防护（垃圾/病毒、钓鱼、自动回复防回环、重复收件）· 聊天转邮件只由人工处理
#   多语言（英文 / 简体 / 繁体）· 多邮箱路由与签名 · 酒店基础信息问答（Focus 1）
# Booking (Area 2) and Example Rewards (Focus 3/4) emails are classified and routed to the General queue
# with an AI draft until their week is deployed.
#
# Knowledge base: this script only packages the existing lambda/data/kb_index.json (or keeps the Bedrock
# Knowledge Base already configured). Build / switch the knowledge base with ./setup_kb.sh (README_kb.md).
# Week-specific env: EMAIL_BUCKET_NAME, CHAT_ORIGIN_SENDERS. Common env: see deploy_lib.sh.
WEEK=1
CAPABILITIES="KB"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy_lib.sh"
init_env

# 1. Email channel prerequisites: message storage (the AI reads the body from it) and PoC mailboxes.
ensure_email_storage
ensure_email_addresses

# 2. Knowledge base (Focus 1) is prepared separately by ./setup_kb.sh; deploy_lambdas checks it exists.

[[ -n "$CHAT_ORIGIN_SENDERS" ]] || warn "CHAT_ORIGIN_SENDERS empty - chat-origin emails are detected by content only."
[[ -n "$POC_NOW" ]] || warn "POC_NOW empty - dummy inventory (Jul-Oct 2026) is in the past; set it before week 2/3 testing."

# 3. Lambdas (triage + agent) with only the KB capability.
deploy_lambdas

# 4. Human hand-off: General (complaints, human requests, not-yet-enabled scenarios, live-chat emails) and Security.
ensure_queue "$QUEUE_GENERAL" reservations
ensure_queue "$QUEUE_SECURITY" reservations
ensure_routing_profile "$QUEUE_GENERAL" "$QUEUE_SECURITY"

# 5. Inbound flow (loop filter -> triage -> agent poll -> Send message / queue) bound to all PoC mailboxes.
publish_flow
bind_email_addresses

# 6. Self-tests against the deployed agent (no email is sent).
smoke "Focus 1 - pool hours (KB answer)" reservations.hgh1 "Swimming pool" \
  "What time does the swimming pool open and where is it located? We are staying at Lakeside Hotel next month." \
  '"search_knowledge_base" in tools and disp in ("AUTO_SEND", "HUMAN_REVIEW")'
smoke "Traditional Chinese reply" reservations.szx1 "健身房開放時間" "請問深圳福田城市酒店的健身房開放時間是幾點？謝謝。" \
  '(r.get("attributes") or {}).get("language") == "zh-TW"'
smoke "Area 2 not enabled yet -> escalate" reservations "Lakeview room rate" \
  "Hi, can you quote me the rate for Lakeview room at Lakeside Hotel? Check in 1 July 2026 for 2 nights, 2 adults. Thanks" \
  'disp == "ESCALATE" and "check_availability" not in tools'

finish "  酒店基础信息问答 (Focus 1) · 新邮件/回复识别 · 安全防护与防回环 · 聊天转邮件转人工 · 三语 · 多邮箱签名" \
"  - Send a NEW email to $(addr reservations) from an external mailbox, reply to the AI answer, then
    aws connect describe-contact ... to confirm the reply's RelatedContactId / ContactAssociationId (thread detection).
  - Send from a no-reply@ address and an 'Automatic reply:' subject: no AI answer, contact disconnected.
  - Assign agents to routing profile $ROUTING_PROFILE_NAME (or set AGENT_USERNAMES) so escalated emails reach someone."
