"""Triage Lambda - invoked SYNCHRONOUSLY by the Connect inbound email flow.

action=triage (8 s budget)
  1. read the email (subject/from/to from the contact, body + attachment list from S3 via Connect)
  2. guards: SES spam/virus verdict, auto-reply/bounce loop, email-from-chat, phishing
  3. NEW vs REPLY: Connect thread link (RelatedContactId), our [Ref: EA-xxxx] token,
     reply prefix + open case with the same subject, quoted history
  4. fast Claude Haiku classification (language, intents/Dcodes, hotel, sentiment, complaint, human request)
  5. create/update the case, start the agent Lambda asynchronously, return routing attributes
action=poll
  returns the agent result once the agent has written it (the flow loops Wait -> poll).

Returns a flat STRING_MAP (the flow validates STRING_MAP). Connect rejects empty
values, so absent values are "none".
"""
import json
import os
import re
import time
from datetime import timedelta

import common
from common import LOGGER, attr, iso, normalize_subject, segment_value, store
from prompts import DCODES, TEAM_BY_PREFIX, TRIAGE_SYSTEM

HOTELS = common.load_json("hotels.json")
REF_RE = re.compile(r"\[Ref:\s*(EA-[A-Z0-9]{6,12})\]", re.I)
REPLY_PREFIX_RE = re.compile(r"^\s*(re|aw|sv|antw|回复|回覆|答复|答覆)\s*[:：]", re.I)
QUOTE_MARKERS = [r"^On .{5,80} wrote:\s*$", r"^-{2,}\s*Original Message\s*-{2,}", r"^From:\s.+\nSent:\s", r"^发件人[:：]", r"^寄件者[:：]",
                 r"^在.{2,60}写道[:：]", r"^於.{2,60}寫道[:：]", r"^>{1,}\s"]
FORWARD_PREFIX_RE = re.compile(r"^\s*(fw|fwd|wg|tr|转发|轉寄|轉發)\s*[:：]", re.I)
# "Handling email loops": Connect does not suppress NDR / out-of-office mail. Sender patterns and bounce subject
# prefixes recommended by the doc (the flow applies the same filter before invoking this Lambda).
AUTO_SENDER_PATTERNS = ("mailer-daemon", "postmaster", "noreply", "no-reply", "donotreply", "do-not-reply", "bounces+", "bounce+")
AUTO_SUBJECT_PREFIXES = ("undeliverable:", "mail delivery failed", "delivery status notification", "returned mail",
                         "automatic reply:", "auto:", "out of office", "自动回复", "自動回覆", "退信", "无法投递")
LOOP_GUARD_MAX = int(os.environ.get("LOOP_GUARD_MAX_REPLIES_PER_HOUR", "5"))
AUTO_MARKERS = ("delivery status notification", "undeliverable", "mail delivery subsystem", "mailer-daemon", "automatic reply",
                "auto-reply", "autoreply", "out of office", "non-delivery report", "returned mail", "auto-submitted:",
                "自动回复", "自動回覆", "自动答复", "退信", "无法投递", "郵件無法遞送", "不在办公室", "休假中")
CHAT_MARKERS = ("chat transcript", "offline message", "live chat", "livechat", "chat conversation", "聊天记录", "在线客服留言",
                "聊天記錄", "線上客服留言", "离线留言", "離線留言")
DANGEROUS_EXT = (".exe", ".scr", ".js", ".vbs", ".bat", ".cmd", ".jar", ".msi", ".iso", ".html", ".htm", ".hta", ".lnk")
# Look-alike detection for the operator's own domain (e.g. example-hotels.com -> "example-hotels" in any other host).
BRAND_DOMAIN = os.environ.get("BRAND_DOMAIN", "example-hotels.com").lower()
BRAND_KEY = BRAND_DOMAIN.split(".")[0]
SHORTENERS = ("bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "rb.gy", "cutt.ly")


# ---------------------------------------------------------------- helpers

mailbox_profile = common.mailbox_profile


def latest_message(body):
    """Text above the first quoted-history marker."""
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if any(re.search(p, "\n".join(lines[i:i + 2]), re.M | re.I) for p in QUOTE_MARKERS):
            return "\n".join(lines[:i]).strip(), True
    return body, False


def phishing_signals(links, attachments, text):
    signals = []
    for link in links:
        href, label = link.get("href", ""), link.get("text", "")
        host = re.sub(r"^https?://", "", href).split("/")[0].lower()
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}(:\d+)?$", host):
            signals.append(f"IP-address link {host}")
        if any(host == s or host.endswith("." + s) for s in SHORTENERS):
            signals.append(f"shortened link {host}")
        shown = re.search(r"([a-z0-9-]+\.)+[a-z]{2,}", (label or "").lower())
        if shown and host and shown.group(0) not in host and host not in shown.group(0):
            signals.append(f"link text {shown.group(0)} points to {host}")
        if BRAND_KEY and BRAND_KEY in host and not (host == BRAND_DOMAIN or host.endswith("." + BRAND_DOMAIN)):
            signals.append(f"look-alike domain {host}")
    for att in attachments:
        if (att.get("name") or "").lower().endswith(DANGEROUS_EXT):
            signals.append(f"risky attachment {att.get('name')}")
    if re.search(r"(verify|confirm|update).{0,30}(password|card number|cvv|bank account|login)", text, re.I):
        signals.append("credential/payment request wording")
    return signals


def classify(subject, text, attachment_names, budget_s):
    """Claude Haiku triage. Returns (dict, ok)."""
    payload = json.dumps({"subject": subject[:500], "body": text[:8000], "attachments": attachment_names}, ensure_ascii=False)
    try:
        resp = common.converse(common.TRIAGE_MODEL_ID, [{"text": TRIAGE_SYSTEM}],
                               [{"role": "user", "content": [{"text": payload}]}], max_tokens=700,
                               read_timeout=max(1.0, budget_s))
        text_out = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
        result = common.parse_json_text(text_out)
        intents = [i for i in result.get("intents") or [] if i.get("dcode") in DCODES]
        result["intents"] = intents or [{"dcode": "OTH-OTHER", "summary": "unclassified"}]
        if result.get("primary_dcode") not in DCODES:
            result["primary_dcode"] = result["intents"][0]["dcode"]
        if result.get("hotel_code") not in HOTELS["hotels"]:
            result["hotel_code"] = None
        return result, True
    except Exception as exc:  # the agent re-classifies anyway; never fail the flow here
        LOGGER.warning("Triage classification failed: %s", type(exc).__name__)
        return {"language": "en", "intents": [{"dcode": "OTH-OTHER", "summary": "classification unavailable"}],
                "primary_dcode": "OTH-OTHER", "hotel_code": None, "sentiment": "neutral", "complaint": False,
                "foul_language": False, "explicit_human_request": False, "payment_or_sensitive": False,
                "phishing_suspected": False, "is_chat_transcript": False, "is_auto_reply": False, "confidence": 0}, False


def _describe(instance_id, contact_id):
    return common.client("connect", connect_timeout=0.5, read_timeout=1.5, retries={"max_attempts": 1}).describe_contact(
        InstanceId=instance_id, ContactId=contact_id)["Contact"]


def connect_thread_info(contact_data, instance_id, contact_id, remaining_ms):
    """Connect's own email threading fields (How Connect Customer email works > Email threads).

    Observed on a live instance and documented in the Contact data model:
      * a NEW inbound email: no RelatedContactId, ContactAssociationId == its own ContactId
      * the reply the flow sends (Send message, InitiationMethod FLOW): RelatedContactId = inbound, same ContactAssociationId
      * the guest's reply to that email: RelatedContactId = the email it answers (our OUTBOUND contact),
        ContactAssociationId = root contact of the thread (the first inbound email) - also for thread trees.
    The Lambda event does not document RelatedContactId, so DescribeContact is always used.
    """
    info = {"related": contact_data.get("RelatedContactId") or "", "association": contact_data.get("ContactAssociationId") or "",
            "related_parent": "", "related_direction": "", "ok": False}
    if common.LOCAL_MODE:  # local harness passes the fields in ContactData
        info["ok"] = True
        return info
    try:
        if remaining_ms() > 5500:
            c = _describe(instance_id, contact_id)
            info.update(related=c.get("RelatedContactId") or info["related"], association=c.get("ContactAssociationId") or info["association"], ok=True)
        # One hop up: the guest answered an OUTBOUND contact; its RelatedContactId is the inbound email we mapped.
        if info["related"] and remaining_ms() > 5000:
            r = _describe(instance_id, info["related"])
            info["related_parent"] = r.get("RelatedContactId") or ""
            info["related_direction"] = segment_value((r.get("SegmentAttributes") or {}).get("connect:Direction"))
    except Exception as exc:  # threading falls back to the [Ref] token / subject rules
        LOGGER.warning("DescribeContact failed: %s", type(exc).__name__)
    return info


def detect_thread(customer, subject, body, contact_id, thread):
    """Decide NEW vs REPLY and which case the email belongs to.

    Order (strongest first): Connect thread fields -> our [Ref: EA-xxxx] token -> reply prefix + open case with the
    same subject (clients that drop threading headers) -> quoted history only (REPLY_UNLINKED).
    """
    st = store()
    evidence, case_id = [], None
    assoc = thread.get("association") if thread.get("association") not in ("", contact_id) else ""
    connect_threaded = bool(thread.get("related") or assoc)
    for label, cid in (("related", thread.get("related")), ("related_parent", thread.get("related_parent")), ("thread_root", assoc)):
        if not cid:
            continue
        link = st.get(f"THREAD#{cid}")
        if link:
            case_id = link["case_id"]
            evidence.append(f"connect_{label}:{cid[:8]}")
            break
    if connect_threaded and not case_id:
        evidence.append("connect_thread_not_in_poc")  # e.g. thread started by an agent-initiated email
    m = REF_RE.search(subject) or REF_RE.search(body[:20000])
    if m:
        ref = m.group(1).upper()
        if st.get(f"CASE#{ref}"):
            case_id = case_id or ref
            evidence.append(f"case_ref_token:{ref}")
        else:
            evidence.append(f"unknown_ref_token:{ref}")
    prefix = bool(REPLY_PREFIX_RE.search(subject or ""))
    if prefix:
        evidence.append("reply_prefix")
    _, quoted = latest_message(body)
    if quoted:
        evidence.append("quoted_history")
    if not case_id and prefix and customer:
        open_case = st.get(f"CUSTOMER#{customer.lower()}", "OPENCASE")
        if open_case and common.within(open_case.get("updated_at", ""), 30):
            meta = st.get(f"CASE#{open_case['case_id']}") or {}
            if meta.get("subject_norm") and meta["subject_norm"] == normalize_subject(subject):
                case_id = open_case["case_id"]
                evidence.append("same_subject_open_case")
    forwarded = bool(FORWARD_PREFIX_RE.search(subject or ""))
    if forwarded:
        evidence.append("forward_prefix")
    if case_id:
        return "REPLY", case_id, evidence
    # A reply to something outside this PoC (agent-initiated email, pre-PoC mail). A forward is a new enquiry.
    if connect_threaded or prefix or (quoted and not forwarded):
        return "REPLY_UNLINKED", None, evidence
    return "NEW", None, evidence


def team_for(dcode):
    return TEAM_BY_PREFIX.get((dcode or "OTH").split("-")[0], "GENERAL")


def start_agent(payload):
    if common.LOCAL_MODE:
        import agent  # local verification runs the agent inline
        agent.run(payload)
        return
    common.client("lambda", read_timeout=3).invoke(FunctionName=common.AGENT_FUNCTION_NAME, InvocationType="Event",
                                                    Payload=json.dumps(payload, ensure_ascii=False).encode())


# ---------------------------------------------------------------- actions

def triage(event, context):
    t0 = time.time()
    remaining = (lambda: context.get_remaining_time_in_millis()) if hasattr(context, "get_remaining_time_in_millis") else (lambda: 8000 - common.elapsed_ms(t0))
    cd = event["Details"]["ContactData"]
    contact_id = cd["ContactId"]
    instance_id = cd["InstanceARN"].rsplit("/", 1)[-1]
    seg = cd.get("SegmentAttributes") or {}
    subject = segment_value(seg.get("connect:EmailSubject"))
    customer = ((cd.get("CustomerEndpoint") or {}).get("Address") or "").strip()
    customer_name = (cd.get("CustomerEndpoint") or {}).get("DisplayName") or ""
    system_addr = ((cd.get("SystemEndpoint") or {}).get("Address") or "").strip()
    mailbox, mailbox_hotel = mailbox_profile(system_addr)
    received_at = cd.get("InitiationTimestamp") or iso()

    route, reason, email = "AI", "", {"body": "", "links": [], "attachments": []}
    spam = segment_value(seg.get("connect:X-SES-SPAM-VERDICT")).upper()
    virus = segment_value(seg.get("connect:X-SES-VIRUS-VERDICT")).upper()
    if spam == "FAILED" or virus == "FAILED":
        route, reason = "SECURITY", f"SES verdict spam={spam or 'n/a'} virus={virus or 'n/a'}"
    if route == "AI" and (any(p in customer.lower() for p in AUTO_SENDER_PATTERNS)
                          or subject.strip().lower().startswith(AUTO_SUBJECT_PREFIXES)):
        route, reason = "DROP", "automated sender / bounce subject - not answered to prevent mail loops"
    # "Every email message is a unique email contact": one message sent to several instance addresses (To/CC)
    # creates several inbound contacts with the same Message-ID. Only the first one is processed.
    message_id = segment_value(seg.get("connect:EmailMessageId")).strip()
    duplicate_of = None
    if message_id and not store().put_if_absent(f"MSGID#{message_id}", "META", {"contact_id": contact_id, "system_address": system_addr, "at": iso()}):
        duplicate_of = (store().get(f"MSGID#{message_id}") or {}).get("contact_id")
        if duplicate_of != contact_id:  # Lambda retries of the same contact are not duplicates
            route, reason = "DROP", f"same Message-ID already handled as contact {duplicate_of} (sent to several instance addresses)"
    recent = (store().get(f"CUSTOMER#{customer.lower()}", "OPENCASE") or {}).get("ai_reply_times", []) if customer else []
    cutoff = (common.now_utc() - timedelta(hours=1)).isoformat()
    if route == "AI" and sum(1 for t in recent if t >= cutoff) >= LOOP_GUARD_MAX:
        route, reason = "HUMAN", f"loop guard: {LOOP_GUARD_MAX}+ AI replies to this sender in the last hour"
    try:
        email = event.get("_local_email") or common.fetch_email(cd)
    except Exception as exc:
        LOGGER.exception("Reading email body failed")
        if route == "AI":
            route, reason = "HUMAN", f"email body unreadable ({type(exc).__name__})"
    body = email.get("body", "")
    latest, _ = latest_message(body)
    sample = f"{subject}\n{latest[:3000]}".lower()

    if route == "AI" and any(m in sample for m in AUTO_MARKERS):
        route, reason = "DROP", "auto-reply / bounce - not answered to prevent mail loops"
    if route == "AI" and (customer.lower() in common.CHAT_ORIGIN_SENDERS or any(m in sample for m in CHAT_MARKERS)):
        route, reason = "HUMAN", "email originated from live chat - manual handling only (Req. 2)"

    email_type, case_id, thread_evidence = "NEW", None, []
    if route != "DROP":
        thread = connect_thread_info(cd, instance_id, contact_id, remaining)
        email_type, case_id, thread_evidence = detect_thread(customer, subject, body, contact_id, thread)

    cls, cls_ok = ({}, False)
    if route in ("AI", "HUMAN"):
        cls, cls_ok = classify(subject, latest or body, [a.get("name") for a in email.get("attachments", [])],
                               budget_s=max(1.0, (remaining() - 1500) / 1000))
    signals = phishing_signals(email.get("links", []), email.get("attachments", []), latest)
    if route == "AI" and (cls.get("phishing_suspected") and signals or len(signals) >= 2):
        route, reason = "SECURITY", "possible phishing: " + "; ".join(signals[:3] or [cls.get("phishing_reason", "")])
    if route == "AI" and cls.get("is_chat_transcript"):
        route, reason = "HUMAN", "email originated from live chat - manual handling only (Req. 2)"
    if route == "AI" and cls.get("is_auto_reply"):
        route, reason = "DROP", "auto-reply detected by classifier"

    ai_mode = "NONE"
    if route == "AI":
        ai_mode = "AUTO"
        off = common.disabled_dcodes([i.get("dcode") for i in cls.get("intents", [])])
        if cls.get("explicit_human_request") or cls.get("complaint"):
            ai_mode, reason = "DRAFT_ONLY", "guest asked for a human agent" if cls.get("explicit_human_request") else "complaint - human handles, AI drafts"
        elif off:
            ai_mode, reason = "DRAFT_ONLY", f"scenario not enabled yet (PoC week {common.POC_WEEK or '?'}): {','.join(sorted(set(off)))} - AI drafts, agent replies"
    elif route == "HUMAN" and "live chat" not in reason:
        ai_mode = "DRAFT_ONLY"  # body unreadable etc.: let an agent pick it up with whatever context we have

    st = store()
    if route == "DROP":
        case_id = "DROPPED"  # loops / duplicates never open or touch a guest case
    elif not case_id:
        case_id = common.new_case_id()
        st.put(f"CASE#{case_id}", "META", {"case_id": case_id, "customer_email": customer, "customer_name": customer_name,
               "mailbox": mailbox, "system_address": system_addr, "subject": subject, "subject_norm": normalize_subject(subject),
               "hotel_code": cls.get("hotel_code") or mailbox_hotel, "language": cls.get("language", "en"),
               "created_at": received_at, "updated_at": iso(), "status": "OPEN", "state": {}, "ai_turns": 0})
    else:
        meta = st.get(f"CASE#{case_id}")
        meta.update(updated_at=iso(), status="OPEN")
        if cls.get("hotel_code"):
            meta["hotel_code"] = cls["hotel_code"]
        st.put(f"CASE#{case_id}", "META", meta)
    if route != "DROP":
        st.put(f"THREAD#{contact_id}", "META", {"case_id": case_id})
        if customer:
            prev = st.get(f"CUSTOMER#{customer.lower()}", "OPENCASE") or {}
            prev.update(case_id=case_id, updated_at=iso(),
                        recent_cases=([case_id] + [c for c in prev.get("recent_cases", []) if c != case_id])[:5])
            st.put(f"CUSTOMER#{customer.lower()}", "OPENCASE", {k: v for k, v in prev.items() if k not in ("pk", "sk")})

    dcodes = [i["dcode"] for i in cls.get("intents", [])] or ["OTH-OTHER"]
    triage_record = {"contact_id": contact_id, "case_id": case_id, "email_type": email_type, "thread_evidence": thread_evidence,
                     "route": route, "ai_mode": ai_mode, "reason": reason, "classification": cls, "classification_ok": cls_ok,
                     "phishing_signals": signals, "received_at": received_at, "triaged_at": iso(), "mailbox": mailbox,
                     "subject": subject, "customer_email": customer, "triage_ms": common.elapsed_ms(t0)}
    st.put(f"CONTACT#{contact_id}", "TRIAGE", triage_record)

    if ai_mode != "NONE":
        st.put(f"CONTACT#{contact_id}", "RESULT", {"status": "PENDING", "attempts": 0, "started_at": iso()})
        start_agent({"contact_id": contact_id, "case_id": case_id, "instance_id": instance_id, "contact_arn": f"{cd['InstanceARN']}/contact/{contact_id}",
                     "subject": subject, "body": body[:common.MAX_EMAIL_CHARS], "attachments": email.get("attachments", []),
                     "customer_email": customer, "customer_name": customer_name, "system_address": system_addr,
                     "mailbox": mailbox, "email_type": email_type, "ai_mode": ai_mode, "triage": cls, "received_at": received_at})
    else:
        _record_turn_without_ai(triage_record)

    LOGGER.info("Triage %s case=%s type=%s route=%s mode=%s dcodes=%s", contact_id, case_id, email_type, route, ai_mode, dcodes)
    return {
        "case_id": case_id, "email_type": "REPLY" if email_type.startswith("REPLY") else "NEW",
        "thread_evidence": attr(",".join(thread_evidence)), "route": route, "ai_mode": ai_mode,
        "team": "SECURITY" if route == "SECURITY" else team_for(cls.get("primary_dcode")),
        "triage_reason": attr(reason, 300), "language": attr(cls.get("language")), "primary_dcode": attr(cls.get("primary_dcode", "OTH-OTHER")),
        "dcodes": attr(",".join(dcodes)), "hotel_code": attr(cls.get("hotel_code") or mailbox_hotel), "sentiment": attr(cls.get("sentiment", "neutral")),
        "mailbox": mailbox,
    }


def _record_turn_without_ai(t):
    """Report rows also cover emails the AI deliberately did not answer."""
    store().put(f"CASE#{t['case_id']}", f"TURN#{t['received_at']}#{t['contact_id']}", {
        "case_id": t["case_id"], "contact_id": t["contact_id"], "received_at": t["received_at"], "email_type": t["email_type"],
        "route": t["route"], "disposition": "NOT_AI_HANDLED" if t["route"] != "DROP" else "DROPPED", "escalation_reason": t["reason"],
        "dcodes": [i["dcode"] for i in t["classification"].get("intents", [])] or ["SYS-" + t["route"]],
        "language": t["classification"].get("language"), "sentiment": t["classification"].get("sentiment"), "items": [],
        "fallback_type": "ESCALATION" if t["route"] in ("HUMAN", "SECURITY") else "NONE"})


def poll(event, context):
    contact_id = event["Details"]["ContactData"]["ContactId"]
    st = store()
    result = st.get(f"CONTACT#{contact_id}", "RESULT")
    if not result:
        return {"status": "ERROR", "error": "no agent job for this contact"}
    if result["status"] == "PENDING":
        result["attempts"] = result.get("attempts", 0) + 1
        st.put(f"CONTACT#{contact_id}", "RESULT", result)
        if result["attempts"] > common.MAX_POLL_ATTEMPTS:
            return {"status": "TIMEOUT", "disposition": "ESCALATE", "escalation_reason": "AI did not finish in time", "team": "GENERAL"}
        return {"status": "PENDING", "attempts": str(result["attempts"])}
    # reply_body is already capped at 20 KB (UTF-8) by the agent; other values stay short.
    out = {k: attr(v, 20000 if k == "reply_body" else 1000) for k, v in result.get("attributes", {}).items()}
    out["status"] = result["status"]
    return out


def lambda_handler(event, context):
    action = ((event.get("Details") or {}).get("Parameters") or {}).get("action", "triage")
    if action == "poll":
        return poll(event, context)
    if action == "triage":
        return triage(event, context)
    raise ValueError(f"Unsupported action {action}")
