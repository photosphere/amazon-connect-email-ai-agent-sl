"""Export the 11 "POC Report" metrics (CXC_AI ... Vendor's response.xlsx, POC Report tab) as CSV.

Source: the DynamoDB table written by the Lambdas - one TURN row per inbound email
(AI-handled or not) under CASE#<case_id>.

Usage:
    python3 export_reports.py --table sl-email-ai-poc --region us-west-2 --out ./out
    python3 export_reports.py --from-json turns.json --out ./out      # offline (list of TURN rows)

Output: one CSV per report (01_automation_rate.csv ... 11_fallback_rate.csv) with a
case-level detail section and a TOTAL row, plus summary.csv. Open them in Excel.
"""
import argparse
import csv
import json
import os
from collections import defaultdict
from datetime import datetime

AI_SENT = "AUTO_SEND"
ESCALATED = ("ESCALATE", "HUMAN_REVIEW", "NOT_AI_HANDLED")


def load_turns(args):
    if args.from_json:
        rows = json.load(open(args.from_json, encoding="utf-8"))
    else:
        import boto3
        from boto3.dynamodb.conditions import Attr
        table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
        rows, kwargs = [], {"FilterExpression": Attr("sk").begins_with("TURN#")}
        while True:
            resp = table.scan(**kwargs)
            rows += resp.get("Items", [])
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        rows = json.loads(json.dumps(rows, default=lambda d: float(d)))
    # auto-replies/bounces are not guest interactions; SMOKE-* rows come from deploy_week_N.sh self-tests
    return [r for r in rows if r.get("disposition") != "DROPPED" and not str(r.get("case_id", "")).startswith("SMOKE-")]


def ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def pct(n, d):
    return f"{(100.0 * n / d):.1f}%" if d else "n/a"


def minutes(a, b):
    a, b = ts(a), ts(b)
    return round((b - a).total_seconds() / 60, 1) if a and b else None


def write(out, name, header, rows):
    path = os.path.join(out, name)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:  # BOM so Excel shows Chinese correctly
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default=os.environ.get("TABLE_NAME", "sl-email-ai-poc"))
    ap.add_argument("--region", default=os.environ.get("AWS_REGION"))
    ap.add_argument("--from-json")
    ap.add_argument("--out", default="./poc-reports")
    ap.add_argument("--fcr-window-days", type=int, default=5)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    turns = sorted(load_turns(args), key=lambda t: str(t.get("received_at")))
    cases = defaultdict(list)
    for t in turns:
        cases[t["case_id"]].append(t)
    summary = []

    # 1 Automation rate
    rows, ai_int, full_cases = [], 0, 0
    for cid, ts_ in cases.items():
        handled = sum(1 for t in ts_ if t.get("disposition") == AI_SENT)
        full = all(t.get("disposition") == AI_SENT for t in ts_)
        ai_int += handled
        full_cases += full
        rows.append([cid, len(ts_), handled, pct(handled, len(ts_)), "Y" if full else "N"])
    rows.append(["TOTAL", len(turns), ai_int, pct(ai_int, len(turns)), f"{full_cases}/{len(cases)} = {pct(full_cases, len(cases))}"])
    write(args.out, "01_automation_rate.csv", ["Case ID", "Interactions", "Handled by AI", "Automation rate (interaction)", "Case fully handled by AI"], rows)
    summary.append(["1 Automation Rate", f"interaction {pct(ai_int, len(turns))}; case {pct(full_cases, len(cases))}"])

    # 2 AI accuracy (self-evaluation against KB + tool evidence)
    rows, tot, ok = [], 0, 0
    for t in turns:
        g = t.get("grounding") or {}
        if g.get("claims_total") is None:
            continue
        tot += g.get("claims_total") or 0
        ok += g.get("claims_supported") or 0
        rows.append([t["case_id"], t["contact_id"], g.get("claims_total"), g.get("claims_supported"),
                     " | ".join(f"{u.get('claim')} ({u.get('why')})" for u in g.get("unsupported", []))])
    rows.append(["TOTAL", "", tot, ok, f"AI accuracy {pct(ok, tot)}"])
    write(args.out, "02_ai_accuracy.csv", ["Case ID", "Contact ID", "AI factual statements", "Correct (supported)", "Error breakdown"], rows)
    summary.append(["2 AI Accuracy", pct(ok, tot)])

    # 3 Intent recognition accuracy (triage tag vs verified agent tag; add a human QA column for final scoring)
    rows, n_enq, agree = [], 0, 0
    for t in turns:
        final, first = t.get("dcodes") or [], t.get("triage_dcodes") or []
        if not final:
            continue
        match = len(set(final) & set(first))
        n_enq += len(set(final))
        agree += match
        rows.append([t["case_id"], t["contact_id"], len(t.get("items") or final), ",".join(first), ",".join(final),
                     pct(match, len(set(final))), ",".join(sorted(set(final) - set(first))) or "-", ""])
    rows.append(["TOTAL", "", n_enq, "", "", pct(agree, n_enq), "", ""])
    write(args.out, "03_intent_recognition.csv", ["Case ID", "Contact ID", "Number of enquiries", "Dcode (triage)", "Dcode (agent, after tools)",
          "Agreement", "Incorrect recognition breakdown", "Human QA Dcode (fill in)"], rows)
    summary.append(["3 Intent Recognition (triage vs agent agreement)", pct(agree, n_enq)])

    # 4 Escalation rate
    rows, esc_int, esc_cases = [], 0, 0
    for cid, ts_ in cases.items():
        esc = [t for t in ts_ if t.get("disposition") in ESCALATED]
        esc_int += len(esc)
        esc_cases += bool(esc)
        rows.append([cid, len(ts_), len(esc), pct(len(esc), len(ts_)), "100%" if esc else "0%",
                     " | ".join(t.get("escalation_reason") or "" for t in esc), ",".join(sorted({d for t in ts_ for d in t.get("dcodes") or []}))])
    rows.append(["TOTAL", len(turns), esc_int, pct(esc_int, len(turns)), pct(esc_cases, len(cases)), "", ""])
    write(args.out, "04_escalation_rate.csv", ["Case ID", "AI handled emails", "Escalated emails", "Escalation rate (interaction)",
          "Escalation rate (case)", "Escalation reason", "Dcode"], rows)
    summary.append(["4 Escalation Rate", f"interaction {pct(esc_int, len(turns))}; case {pct(esc_cases, len(cases))}"])

    # 5 First contact resolution: AI answered the first email completely and no follow-up on the same issue in the window
    rows, fcr = [], 0
    for cid, ts_ in cases.items():
        first = ts_[0]
        resolved = first.get("disposition") == AI_SENT and not first.get("expects_reply") and \
            all((minutes(first.get("received_at"), t.get("received_at")) or 0) > args.fcr_window_days * 1440 for t in ts_[1:])
        fcr += resolved
        rows.append([cid, "Y" if resolved else "N", ",".join(first.get("dcodes") or []), minutes(first.get("received_at"), first.get("replied_at"))])
    rows.append(["TOTAL", f"{fcr}/{len(cases)} = {pct(fcr, len(cases))}", "", ""])
    write(args.out, "05_first_contact_resolution.csv", ["Case ID", "Resolved in first interaction", "Dcode", "Resolved time (min)"], rows)
    summary.append(["5 First Contact Resolution", pct(fcr, len(cases))])

    # 6 Resolution time
    rows, seg = [], defaultdict(list)
    for cid, ts_ in cases.items():
        end = max((t.get("replied_at") or t.get("received_at") for t in ts_), key=str)
        m = minutes(ts_[0].get("received_at"), end)
        kind = "AI" if all(t.get("disposition") == AI_SENT for t in ts_) else "Escalated"
        if m is not None:
            seg[kind].append(m)
        rows.append([cid, ts_[0].get("received_at"), end, m, kind])
    for kind, vals in seg.items():
        rows.append([f"AVERAGE {kind}", "", "", round(sum(vals) / len(vals), 1), kind])
    write(args.out, "06_resolution_time.csv", ["Case ID", "First email", "Final email", "Resolution time (min)", "Segment"], rows)
    summary.append(["6 Resolution Time (avg min)", "; ".join(f"{k} {round(sum(v) / len(v), 1)}" for k, v in seg.items())])

    # 7 Response time (AI generation time per response)
    rows, vals = [], []
    for t in turns:
        m = minutes(t.get("received_at"), t.get("replied_at"))
        if m is None:
            continue
        vals.append(m)
        rows.append([t["case_id"], t["contact_id"], t.get("received_at"), t.get("replied_at"), m, round((t.get("agent_ms") or 0) / 1000, 1)])
    rows.append(["AVERAGE", "", "", "", round(sum(vals) / len(vals), 2) if vals else "n/a", ""])
    write(args.out, "07_response_time.csv", ["Case ID", "Contact ID", "Email received", "AI reply ready", "Response time (min)", "Agent processing (s)"], rows)
    summary.append(["7 Response Time (avg min)", round(sum(vals) / len(vals), 2) if vals else "n/a"])

    # 8 Knowledge gap detection
    rows, topics = [], defaultdict(int)
    for t in turns:
        for g in t.get("knowledge_gaps") or []:
            topics[g.get("topic", "")] += 1
            rows.append([t["case_id"], t["contact_id"], g.get("topic"), g.get("missing_info"), " ; ".join((t.get("kb_chunks_seen") or [])[:5]), g.get("guest_question")])
    rows += [["TOP TOPIC", "", k, f"{v} occurrence(s)", "", ""] for k, v in sorted(topics.items(), key=lambda x: -x[1])[:10]]
    write(args.out, "08_knowledge_gaps.csv", ["Case ID", "Message ID", "Topic", "Missing / unclear information", "KB link (chunks searched)", "Guest enquiry"], rows)
    summary.append(["8 Knowledge Gaps", f"{sum(topics.values())} gap(s), top: {', '.join(list(topics)[:3])}"])

    # 9 Knowledge grounding rate
    rows, grounded, n = [], 0, 0
    for t in turns:
        g = t.get("grounding") or {}
        if g.get("claims_total") is None or t.get("route") != "AI":
            continue
        n += 1
        ok_ = not g.get("unsupported")
        grounded += ok_
        rows.append([t["case_id"], t["contact_id"], "Y" if ok_ else "N", len(g.get("unsupported") or []),
                     " ; ".join(sorted({i for it in t.get("items") or [] for i in it.get("kb_chunk_ids") or []}) or (t.get("kb_chunks_seen") or [])[:5]),
                     (t.get("guest_text") or "")[:300].replace("\n", " ")])
    rows.append(["TOTAL", n, grounded, pct(grounded, n), "", ""])
    write(args.out, "09_knowledge_grounding.csv", ["Case ID", "Contact ID", "Fully grounded", "Ungrounded statements", "KB link", "Guest enquiry"], rows)
    summary.append(["9 Knowledge Grounding Rate", pct(grounded, n)])

    # 10 Auto tagging rate
    tagged = [t for t in turns if any(d and d != "OTH-OTHER" for d in t.get("dcodes") or [])]
    rows = [[t["case_id"], t["contact_id"], ",".join(t.get("dcodes") or []), "Y" if t in tagged else "N"] for t in turns]
    rows.append(["TOTAL", len(turns), f"tagged {len(tagged)}", pct(len(tagged), len(turns))])
    write(args.out, "10_auto_tagging.csv", ["Case ID", "Contact ID", "Dcode", "Tagged"], rows)
    summary.append(["10 Auto Tagging Rate", pct(len(tagged), len(turns))])

    # 11 Fallback / refusal rate
    rows, fb_int, fb_cases = [], 0, 0
    for cid, ts_ in cases.items():
        fb = [t for t in ts_ if t.get("fallback_type") in ("ESCALATION", "CLARIFICATION")]
        fb_int += len(fb)
        fb_cases += bool(fb)
        rows.append([cid, len(ts_), len(fb), pct(len(fb), len(ts_)), "100%" if fb else "0%",
                     f"escalation {sum(t.get('fallback_type') == 'ESCALATION' for t in fb)} / clarification {sum(t.get('fallback_type') == 'CLARIFICATION' for t in fb)}"])
    rows.append(["TOTAL", len(turns), fb_int, pct(fb_int, len(turns)), pct(fb_cases, len(cases)), ""])
    write(args.out, "11_fallback_rate.csv", ["Case ID", "Interactions", "Fallback interactions", "Fallback rate (interaction)", "Fallback rate (case)", "Breakdown"], rows)
    summary.append(["11 Fallback / Refusal Rate", f"interaction {pct(fb_int, len(turns))}; case {pct(fb_cases, len(cases))}"])

    write(args.out, "summary.csv", ["Report", "Result"], summary)
    for s in summary:
        print(f"{s[0]}: {s[1]}")
    print(f"CSV files written to {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
