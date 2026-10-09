"""Agent Lambda - invoked ASYNCHRONOUSLY by triage (InvocationType=Event).

Runs the Claude (Bedrock Converse) tool-use loop for one inbound email:
case history (multi-turn context) + email + attachments -> tools (KB, booking,
LOYALTY) -> submit_reply -> grounding judge -> compose the templated email ->
write the result the flow polls for, plus a per-email report row.
"""
import html
import json
import re
import time

import common
import tools
from common import LOGGER, iso, store
from prompts import AGENT_SYSTEM, DCODES, FAREWELL, JUDGE_SYSTEM, OPENING, SUBMIT_REPLY_SPEC, TEAM_BY_PREFIX

MAX_TURNS = 14
MAX_REPLY_BYTES = 20000  # Connect: 32 KB for all attributes of a contact
HOTELS = common.load_json("hotels.json")


# ---------------------------------------------------------------- composition

def _inline(text):
    text = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)


def body_to_html(body):
    out, lines, i = [], body.strip().split("\n"), 0
    while i < len(lines):
        line = re.sub(r"^\s*#{1,6}\s+", "", lines[i].rstrip())
        if not line.strip() or re.fullmatch(r"\s*(-{3,}|\*{3,}|_{3,})\s*", line):
            i += 1
            continue
        if line.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            t = ['<table style="border-collapse:collapse;margin:8px 0">']
            for r_i, cells in enumerate(rows):
                tag = "th" if r_i == 0 and len(rows) > 1 else "td"
                style = 'style="border:1px solid #bbb;padding:4px 8px;text-align:left;vertical-align:top"'
                t.append("<tr>" + "".join(f"<{tag} {style}>{_inline(c)}</{tag}>" for c in cells) + "</tr>")
            out.append("".join(t) + "</table>")
            continue
        if re.match(r"^\s*(-|\*|•)\s+", line) or re.match(r"^\s*\d+[.)]\s+", line):
            ordered = bool(re.match(r"^\s*\d+[.)]\s+", line))
            items = []
            while i < len(lines) and (re.match(r"^\s*(-|\*|•)\s+", lines[i]) or re.match(r"^\s*\d+[.)]\s+", lines[i])):
                items.append(re.sub(r"^\s*((-|\*|•)|\d+[.)])\s+", "", lines[i]))
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + f"</{tag}>")
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not lines[i].lstrip().startswith("|") \
                and not re.match(r"^\s*((-|\*|•)|\d+[.)])\s+", lines[i]):
            para.append(_inline(lines[i].strip()))
            i += 1
        out.append("<p>" + "<br>".join(para) + "</p>")
    return "\n".join(out)


def _table_to_text(rows):
    """Plain-text rendering of a pipe table (the flow's Send message block only sends text/plain)."""
    if not rows:
        return []
    if all(len(r) == 2 for r in rows):  # key/value recap table
        return [f"{k}: {v}" if k else f"    {v}" for k, v in rows]
    header, out = rows[0], []
    if not header[0].strip() or len(rows) > len(header):  # first column holds row labels (transposed recap)
        for r in rows[1:]:
            out.append(f"{r[0]}: " + "; ".join(f"{h}: {c}" for h, c in zip(header[1:], r[1:]) if c))
        return out
    for r in rows[1:]:
        out.append(" | ".join(f"{h}: {c}" for h, c in zip(header, r) if c))
    return out or [" | ".join(header)]


def body_to_text(body):
    out, rows = [], []
    plain = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", re.sub(r"\*\*(.+?)\*\*", r"\1", body))
    for line in plain.split("\n"):
        if re.fullmatch(r"\s*(-{3,}|\*{3,}|_{3,})\s*", line):
            continue  # horizontal rules
        line = re.sub(r"^\s*#{1,6}\s+", "", line)
        if line.lstrip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                rows.append(cells)
            continue
        if rows:
            out.extend(_table_to_text(rows))
            rows = []
        out.append(re.sub(r"^\s*(\*|•)\s+", "- ", line.rstrip()))
    out.extend(_table_to_text(rows))
    return "\n".join(out).strip()


def signature_for(mailbox, hotel_code, language):
    if mailbox == "LOYALTY":
        sigs = HOTELS["mailbox_signatures"]["LOYALTY"]
    elif mailbox == "HOTEL" and hotel_code in HOTELS["hotels"]:
        sigs = HOTELS["hotels"][hotel_code]["signature"]
    else:
        sigs = HOTELS["mailbox_signatures"]["CXC"]
    if language.startswith("zh"):
        return sigs.get(language) or sigs["zh"]  # zh-TW variant when provided, else the Chinese signature
    return sigs["en"]


def compose_email(reply, mailbox, mailbox_hotel, encounter):
    lang = reply.get("language") if reply.get("language") in ("en", "zh-CN", "zh-TW") else "en"
    opening_key = "subsequent" if encounter == "subsequent" else ("first_loyalty" if mailbox == "LOYALTY" else "first")
    opening = OPENING[opening_key][lang]
    farewell = FAREWELL["expect" if reply.get("expects_reply") else "no_reply"][lang]
    signature = signature_for(mailbox, mailbox_hotel, lang)
    salutation = reply.get("salutation") or ("Dear Mr./Ms.," if lang == "en" else "尊敬的賓客：" if lang == "zh-TW" else "尊敬的宾客：")
    body = reply.get("body", "").strip()
    text = "\n\n".join([salutation, opening, body_to_text(body), farewell, signature])
    html_parts = [f"<p>{_inline(salutation)}</p>", "<p>" + _inline(opening).replace("\n\n", "</p><p>") + "</p>",
                  body_to_html(body), f"<p>{_inline(farewell)}</p>",
                  "<p>" + "<br>".join(_inline(l) for l in signature.split("\n")) + "</p>"]
    doc = ('<html><body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222">'
           + "\n".join(html_parts) + "</body></html>")
    return text, doc


def reply_subject(original, case_id):
    base = re.sub(r"\s*\[Ref:\s*EA-[A-Z0-9]+\]", "", original or "", flags=re.I).strip() or "Your enquiry"
    if not re.match(r"^\s*(re|回复|回覆)\s*[:：]", base, re.I):
        base = f"Re: {base}"
    return f"{base} [Ref: {case_id}]"


# ---------------------------------------------------------------- context

def history_text(case_id, current_contact):
    turns = [t for t in store().query(f"CASE#{case_id}", "TURN#") if t.get("contact_id") != current_contact][-6:]
    if not turns:
        return "(no earlier emails in this case)"
    parts = []
    for t in turns:
        parts.append(f"--- {t.get('received_at')} guest email ({t.get('email_type')}):\n{(t.get('guest_text') or '')[:2500]}")
        if t.get("reply_text"):
            parts.append(f"--- our reply ({t.get('disposition')}):\n{t['reply_text'][:2500]}")
        if t.get("actions"):
            parts.append(f"--- system actions: {json.dumps(t['actions'], ensure_ascii=False)[:800]}")
    return "\n".join(parts)


def sender_history(customer_email, case_id):
    rec = store().get(f"CUSTOMER#{(customer_email or '').lower()}", "OPENCASE") or {}
    reservations = []
    for cid in rec.get("recent_cases", []):
        meta = store().get(f"CASE#{cid}") or {}
        reservations += (meta.get("state") or {}).get("reservations", [])
    return {"reservations_made_by_ai_for_this_sender": sorted(set(reservations)),
            "other_recent_case_ids": [c for c in rec.get("recent_cases", []) if c != case_id]}


def tool_config():
    specs = tools.enabled_specs() + [SUBMIT_REPLY_SPEC]
    return {"tools": [{"toolSpec": {"name": s["name"], "description": s["description"], "inputSchema": {"json": s["schema"]}}} for s in specs]}


# ---------------------------------------------------------------- grounding judge

def judge(evidence, body, guest_email):
    """Self-evaluation used for the AI Accuracy / Knowledge Grounding reports and as a send gate."""
    if not body.strip():
        return {"claims_total": 0, "claims_supported": 0, "unsupported": [], "accuracy_score": 1.0}
    directory = {c: {"name_en": h["name_en"], "name_zh": h["name_zh"]} for c, h in HOTELS["hotels"].items()}
    full = [{"source": "SOP rules", "text": AGENT_SYSTEM}, {"source": "hotel directory", "hotels": directory},
            {"source": "guest email and case history", "text": guest_email[:15000]}] + evidence
    ev = json.dumps(full, ensure_ascii=False, default=str)[:90000]
    try:
        resp = common.converse(common.JUDGE_MODEL_ID, [{"text": JUDGE_SYSTEM}],
                               [{"role": "user", "content": [{"text": f"EVIDENCE:\n{ev}\n\nREPLY BODY:\n{body}"}]}], max_tokens=3000, read_timeout=90)
        claims = common.parse_json_text("".join(b.get("text", "") for b in resp["output"]["message"]["content"])).get("claims", [])
        unsupported = [{"claim": c.get("claim", ""), "why": c.get("why", "")} for c in claims if c.get("verdict") == "UNSUPPORTED"]
        total = len(claims)
        return {"claims_total": total, "claims_supported": total - len(unsupported), "unsupported": unsupported,
                "accuracy_score": round((total - len(unsupported)) / total, 3) if total else 1.0}
    except Exception as exc:
        LOGGER.warning("Grounding judge failed: %s", type(exc).__name__)
        return {"claims_total": None, "claims_supported": None, "unsupported": [], "accuracy_score": None, "error": type(exc).__name__}


# ---------------------------------------------------------------- main loop

def run(payload):
    t0 = time.time()
    st = store()
    contact_id, case_id = payload["contact_id"], payload["case_id"]
    meta = st.get(f"CASE#{case_id}") or {"case_id": case_id, "state": {}}
    triage = payload.get("triage") or {}
    mailbox = payload.get("mailbox", "CXC")
    mailbox_hotel = common.mailbox_profile(payload.get("system_address"))[1]
    hotel_code = triage.get("hotel_code") or meta.get("hotel_code") or mailbox_hotel
    encounter = "subsequent" if payload.get("email_type", "NEW").startswith("REPLY") or meta.get("ai_turns", 0) > 0 else "first"
    ctx = {"case_id": case_id, "customer_email": payload.get("customer_email"), "hotel_code": hotel_code,
           "case_state": meta.get("state") or {}, "evidence": [], "actions": []}

    attachment_blocks, attachment_notes = [], []
    if payload.get("attachments") and not common.LOCAL_MODE:
        attachment_blocks, attachment_notes = common.load_attachment_blocks(payload["instance_id"], payload["contact_arn"], payload["attachments"])
    attachment_blocks += payload.get("_local_attachment_blocks", [])

    today = common.now_utc().astimezone(tools.HOTEL_TZ).date().isoformat()
    context_block = {
        "TODAY": today, "case_id": case_id, "email_type": payload.get("email_type"), "encounter": encounter,
        "mailbox": mailbox, "mailbox_hotel": mailbox_hotel, "guest_address": payload.get("customer_email"),
        "guest_display_name": payload.get("customer_name"), "ai_mode": payload.get("ai_mode"),
        "triage_hints": {k: triage.get(k) for k in ("language", "intents", "hotel_code", "sentiment", "complaint", "explicit_human_request", "foul_language")},
        "case_state": ctx["case_state"], "sender_history": sender_history(payload.get("customer_email"), case_id),
        "attachments": attachment_notes or [a.get("name") for a in payload.get("attachments", [])],
    }
    off = [c for c in common.ALL_CAPABILITIES if c not in common.ENABLED_CAPABILITIES]
    if off:
        context_block["poc_phase"] = {
            "week": common.POC_WEEK or None, "enabled": sorted(common.ENABLED_CAPABILITIES), "not_enabled_yet": off,
            "rule": "Requests that need a capability not enabled yet (RSV_READ = quotes / booking lookup / remarks; RSV_WRITE = new booking, amendment, "
                    "cancellation; LOYALTY = Example Rewards account, verification, points) must NOT be answered from guesses: mark those items "
                    "ESCALATED with the correct Dcode, still answer the other items, and set disposition ESCALATE."}
    if payload.get("ai_mode") == "DRAFT_ONLY":
        context_block["instruction"] = "This email will be handled by a human agent. Prepare the best possible draft and set disposition ESCALATE."
    user_text = (f"CONTEXT:\n{json.dumps(context_block, ensure_ascii=False, default=str)}\n\nCASE HISTORY:\n{history_text(case_id, contact_id)}\n\n"
                 f"NEW EMAIL\nSubject: {payload.get('subject')}\nFrom: {payload.get('customer_name')} <{payload.get('customer_email')}>\n\n{payload.get('body')}")
    messages = [{"role": "user", "content": [{"text": user_text}] + attachment_blocks}]
    system = [{"text": AGENT_SYSTEM}, {"cachePoint": {"type": "default"}}]
    cfg = tool_config()

    reply, tool_calls, usage, error = None, [], {"inputTokens": 0, "outputTokens": 0}, None
    try:
        for _ in range(MAX_TURNS):
            resp = common.converse(common.AGENT_MODEL_ID, system, messages, max_tokens=4000, tools=cfg, read_timeout=120)
            for k in usage:
                usage[k] += resp.get("usage", {}).get(k, 0)
            msg = resp["output"]["message"]
            messages.append(msg)
            uses = [b["toolUse"] for b in msg["content"] if "toolUse" in b]
            if not uses:
                messages.append({"role": "user", "content": [{"text": "Call submit_reply now with the final reply."}]})
                continue
            results = []
            for use in uses:
                if use["name"] == "submit_reply":
                    reply = use["input"]
                    results.append({"toolResult": {"toolUseId": use["toolUseId"], "content": [{"json": {"ok": True}}]}})
                    continue
                out = tools.run(use["name"], ctx, use["input"])
                tool_calls.append({"tool": use["name"], "input": use["input"], "status": out.get("status") or ("ERROR" if "error" in out else "OK")})
                results.append({"toolResult": {"toolUseId": use["toolUseId"], "content": [{"json": json.loads(json.dumps(out, default=str))}]}})
            messages.append({"role": "user", "content": results})
            if reply:
                break
    except Exception as exc:
        LOGGER.exception("Agent loop failed for %s", contact_id)
        error = f"{type(exc).__name__}: {exc}"[:300]

    if not reply:
        reply = {"language": triage.get("language", "en"), "salutation": "", "body": "", "expects_reply": False,
                 "disposition": "ESCALATE", "escalation_reason": error or "AI could not complete the reply",
                 "items": [{"question": i.get("summary", ""), "dcode": i.get("dcode", "OTH-OTHER"), "status": "ESCALATED"} for i in triage.get("intents", [])],
                 "agent_summary": "AI failed to produce a reply; please handle manually."}

    # Earlier emails of the case are evidence too (multi-turn: the guest may have given details before).
    grounding = judge(ctx["evidence"], reply.get("body", ""),
                      f"{payload.get('body') or ''}\n\nEARLIER EMAILS IN THIS CASE:\n{history_text(case_id, contact_id)}")
    disposition = reply.get("disposition", "ESCALATE")
    reasons = [reply.get("escalation_reason")] if reply.get("escalation_reason") else []
    if payload.get("ai_mode") == "DRAFT_ONLY" and disposition == "AUTO_SEND":
        disposition, reasons = "ESCALATE", reasons + ["human handling requested / complaint"]
    if ctx["case_state"].get("requires_human_validation") and disposition == "AUTO_SEND":
        disposition, reasons = "HUMAN_REVIEW", reasons + ["high-risk change needs human validation"]
    if common.GROUNDING_GATE and grounding.get("unsupported") and disposition == "AUTO_SEND":
        disposition, reasons = "HUMAN_REVIEW", reasons + ["grounding check: " + "; ".join(u.get("claim", "")[:80] for u in grounding["unsupported"][:3])]
    off_items = common.disabled_dcodes([i.get("dcode") for i in reply.get("items", [])])
    if off_items and disposition == "AUTO_SEND":
        disposition, reasons = "ESCALATE", reasons + [f"scenario not enabled in this PoC phase (week {common.POC_WEEK or '?'}): {','.join(sorted(set(off_items)))}"]
    if any(i.get("status") in ("UNANSWERED", "ESCALATED") for i in reply.get("items", [])) and disposition == "AUTO_SEND":
        disposition, reasons = "ESCALATE", reasons + ["some questions unanswered (General Logic #2)"]

    text, html_doc = compose_email(reply, mailbox, mailbox_hotel if mailbox == "HOTEL" else hotel_code, encounter) if reply.get("body") else ("", "")
    if disposition == "AUTO_SEND" and len(text.encode()) > MAX_REPLY_BYTES:
        disposition, reasons = "HUMAN_REVIEW", reasons + ["reply too long for flow attribute"]
    subject = reply_subject(payload.get("subject"), case_id)
    items = reply.get("items") or []
    dcodes = [i.get("dcode") for i in items if i.get("dcode") in DCODES] or [i.get("dcode") for i in triage.get("intents", [])] or ["OTH-OTHER"]
    team = TEAM_BY_PREFIX.get(dcodes[0].split("-")[0], "GENERAL")
    open_items = [i["question"] for i in items if i.get("status") in ("UNANSWERED", "ESCALATED")]
    fallback = "ESCALATION" if disposition in ("ESCALATE", "HUMAN_REVIEW") else ("CLARIFICATION" if any(i.get("status") == "NEED_MORE_INFO" for i in items) else "NONE")
    finished = iso()

    draft = text.encode()[:MAX_REPLY_BYTES].decode(errors="ignore")
    attributes = {
        "disposition": disposition, "reply_subject": subject,
        # Plain text: the flow's native Send message block only accepts ContentType text/plain
        # (validated against CreateContactFlow). html_doc is kept in the report row.
        "reply_body": draft or "none",
        "team": team, "escalation_reason": "; ".join(r for r in reasons if r)[:500] or "none",
        "ai_summary": reply.get("agent_summary", "")[:900], "open_items": " | ".join(open_items)[:900] or "none",
        "dcodes": ",".join(dcodes), "hotel_code": reply.get("hotel_code") or hotel_code or "none",
        "language": reply.get("language", "en"), "sentiment": reply.get("sentiment") or triage.get("sentiment", "neutral"),
        "grounding": f"{grounding.get('claims_supported')}/{grounding.get('claims_total')}",
        "actions": ",".join(a["type"] for a in ctx["actions"]) or "none",
    }
    st.put(f"CONTACT#{contact_id}", "RESULT", {"status": "READY", "attributes": attributes, "finished_at": finished})
    if disposition == "AUTO_SEND" and payload.get("customer_email"):  # feeds the triage loop guard
        key = f"CUSTOMER#{payload['customer_email'].lower()}"
        rec = st.get(key, "OPENCASE") or {}
        rec["ai_reply_times"] = (rec.get("ai_reply_times", []) + [finished])[-20:]
        st.put(key, "OPENCASE", {k: v for k, v in rec.items() if k not in ("pk", "sk")})

    meta.update(state=ctx["case_state"], ai_turns=meta.get("ai_turns", 0) + 1, updated_at=finished,
                language=reply.get("language", meta.get("language")), hotel_code=attributes["hotel_code"],
                status="RESOLVED_BY_AI" if disposition == "AUTO_SEND" and not reply.get("expects_reply") else
                       "WAITING_GUEST" if disposition == "AUTO_SEND" else "WITH_AGENT")
    st.put(f"CASE#{case_id}", "META", meta)
    st.put(f"CASE#{case_id}", f"TURN#{payload.get('received_at')}#{contact_id}", {
        "case_id": case_id, "contact_id": contact_id, "received_at": payload.get("received_at"), "replied_at": finished,
        "email_type": payload.get("email_type"), "route": "AI", "ai_mode": payload.get("ai_mode"), "mailbox": mailbox,
        "expects_reply": bool(reply.get("expects_reply")), "subject": payload.get("subject"),
        "guest_text": (payload.get("body") or "")[:6000], "reply_subject": subject, "reply_text": text, "reply_html": html_doc, "disposition": disposition,
        "escalation_reason": attributes["escalation_reason"], "dcodes": dcodes, "triage_dcodes": [i.get("dcode") for i in triage.get("intents", [])],
        "items": items, "knowledge_gaps": reply.get("knowledge_gaps", []), "kb_chunks_seen": sorted(set(ctx.get("kb_chunks_seen", []))),
        "grounding": grounding, "tool_calls": tool_calls, "actions": ctx["actions"], "fallback_type": fallback,
        "language": reply.get("language"), "sentiment": attributes["sentiment"], "hotel_code": attributes["hotel_code"],
        "usage": usage, "agent_ms": common.elapsed_ms(t0), "error": error})
    LOGGER.info("Agent %s case=%s disposition=%s dcodes=%s tools=%s grounding=%s ms=%s", contact_id, case_id, disposition,
                dcodes, [c["tool"] for c in tool_calls], attributes["grounding"], common.elapsed_ms(t0))
    return {"disposition": disposition, "reply_text": text, "attributes": attributes, "tool_calls": tool_calls, "grounding": grounding}


def lambda_handler(event, context):
    return run(event)
