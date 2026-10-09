"""Shared helpers for the Example Hotel Email AI PoC Lambdas.

Two Lambda functions are deployed from this one package:

* ``triage.lambda_handler``  - invoked SYNCHRONOUSLY by the Connect inbound email
  flow (8 s budget). Actions: ``triage`` (read the email, decide NEW vs REPLY,
  guards, fast Claude Haiku classification, start the agent) and ``poll``
  (return the agent's result once it is ready).
* ``agent.lambda_handler``   - invoked ASYNCHRONOUSLY by triage (InvocationType
  Event, up to 5 min). Runs the Claude tool-use loop against the knowledge base
  and the (mock) booking / Example Rewards systems and writes the reply.

Everything the two functions share lives here: configuration, the DynamoDB
single-table store (with an in-memory twin for local runs), reading the email
body and attachments through the Connect files APIs, and small text helpers.
"""
import base64
import html
import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

TABLE_NAME = os.environ.get("TABLE_NAME", "sl-email-ai-poc")
TRIAGE_MODEL_ID = os.environ.get("TRIAGE_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
AGENT_MODEL_ID = os.environ.get("AGENT_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
JUDGE_MODEL_ID = os.environ.get("JUDGE_MODEL_ID", AGENT_MODEL_ID)
# Weekly roll-out (deploy_week_N.sh). KB = hotel info / general; RSV_READ = quote + validate booking + remarks;
# RSV_WRITE = create / amend / cancel; LOYALTY = Example Rewards verification, account, points. Default: everything.
ALL_CAPABILITIES = ("KB", "RSV_READ", "RSV_WRITE", "LOYALTY")
ENABLED_CAPABILITIES = {c.strip().upper() for c in (os.environ.get("ENABLED_CAPABILITIES") or ",".join(ALL_CAPABILITIES)).split(",") if c.strip()}
ENABLED_CAPABILITIES.add("KB")  # hotel info / escalation are always on
POC_WEEK = os.environ.get("POC_WEEK", "")
# Dcode -> capability needed to answer it automatically (Dcodes not listed need only KB).
DCODE_CAPABILITY = {"RSV-QUOTE": "RSV_READ", "RSV-RECONFIRM": "RSV_READ", "RSV-SPECIAL": "RSV_READ",
                    "RSV-NEW": "RSV_WRITE", "RSV-AMEND": "RSV_WRITE", "RSV-CANCEL": "RSV_WRITE",
                    "LOYALTY-ACCOUNT": "LOYALTY", "LOYALTY-EARN-REDEEM": "LOYALTY"}


def disabled_dcodes(dcodes):
    return [d for d in dcodes if DCODE_CAPABILITY.get(d, "KB") not in ENABLED_CAPABILITIES]


# true: any reply statement the judge cannot ground in KB/tool evidence sends the email to HUMAN_REVIEW.
GROUNDING_GATE = os.environ.get("GROUNDING_GATE", "true").lower() == "true"
AGENT_FUNCTION_NAME = os.environ.get("AGENT_FUNCTION_NAME", "")
MAX_EMAIL_CHARS = int(os.environ.get("MAX_EMAIL_CHARS", "24000"))
MAX_POLL_ATTEMPTS = int(os.environ.get("MAX_POLL_ATTEMPTS", "30"))
CHAT_ORIGIN_SENDERS = [s.strip().lower() for s in os.environ.get("CHAT_ORIGIN_SENDERS", "").split(",") if s.strip()]
LOCAL_MODE = os.environ.get("POC_LOCAL", "") == "1"

_clients = {}


def client(name, **config):
    """Lazily create boto3 clients so local runs without AWS still import."""
    key = (name, json.dumps(config, sort_keys=True, default=str))  # timeouts differ per caller
    if key not in _clients:
        import boto3
        from botocore.config import Config
        _clients[key] = boto3.client(name, config=Config(**config) if config else None)
    return _clients[key]


def load_json(name):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


# PoC mailboxes are created as <prefix>_reservations@..., <prefix>_reservations.pen1@... (deploy_lib.sh NAME_PREFIX);
# hotels.json rules match the base local part, so the prefix is removed first.
MAILBOX_PREFIX = os.environ.get("MAILBOX_PREFIX", "").lower()


def mailbox_profile(system_address):
    """Connect address the guest wrote to -> (mailbox CXC|LOYALTY|HOTEL, default hotel code or None)."""
    rules = load_json("hotels.json")["mailboxes"]
    local = (system_address or "").split("@", 1)[0].lower()
    if MAILBOX_PREFIX and local.startswith(MAILBOX_PREFIX):
        local = local[len(MAILBOX_PREFIX):]
    for rule in rules["rules"]:
        if local.startswith(rule["prefix"]):
            return rule["mailbox"], rule["default_hotel"]
    return rules["default"]["mailbox"], rules["default"]["default_hotel"]


def now_utc():
    override = os.environ.get("POC_NOW")  # e.g. 2026-09-28T10:00:00+08:00 for reproducible demos
    if override:
        return datetime.fromisoformat(override).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or now_utc()).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Store: DynamoDB single table (pk/sk) or in-memory for local verification.
#   CASE#<case_id>        META                  case state (verification, hotel, language...)
#   CASE#<case_id>        TURN#<iso>#<contact>  one inbound email + AI outcome (report source)
#   CONTACT#<contact_id>  RESULT                agent result polled by the flow
#   THREAD#<contact_id>   META                  contact id -> case id (reply threading)
#   CUSTOMER#<email>      OPENCASE              latest case for that sender
#   RES#<confirmNum>      META                  reservation (overrides seed data)
#   LOYALTY#<membership>      META                  LOYALTY profile (overrides seed data)
# ---------------------------------------------------------------------------

def _to_dynamo(value):
    return json.loads(json.dumps(value, ensure_ascii=False), parse_float=Decimal)


def _from_dynamo(value):
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


class DynamoStore:
    def __init__(self, table_name):
        import boto3
        self.table = boto3.resource("dynamodb").Table(table_name)

    def get(self, pk, sk="META"):
        item = self.table.get_item(Key={"pk": pk, "sk": sk}).get("Item")
        return _from_dynamo(item) if item else None

    def put(self, pk, sk, data):
        self.table.put_item(Item=_to_dynamo({**data, "pk": pk, "sk": sk}))

    def put_if_absent(self, pk, sk, data):
        """Atomic first-writer-wins; returns False if the item already exists."""
        try:
            self.table.put_item(Item=_to_dynamo({**data, "pk": pk, "sk": sk}), ConditionExpression="attribute_not_exists(pk)")
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def query(self, pk, sk_prefix=""):
        from boto3.dynamodb.conditions import Key
        cond = Key("pk").eq(pk)
        if sk_prefix:
            cond = cond & Key("sk").begins_with(sk_prefix)
        items, kwargs = [], {"KeyConditionExpression": cond}
        while True:
            resp = self.table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return [_from_dynamo(i) for i in items]
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def scan_prefix(self, sk_prefix):
        from boto3.dynamodb.conditions import Attr
        items, kwargs = [], {"FilterExpression": Attr("sk").begins_with(sk_prefix)}
        while True:
            resp = self.table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return [_from_dynamo(i) for i in items]
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


class MemoryStore:
    def __init__(self):
        self.items = {}

    def get(self, pk, sk="META"):
        item = self.items.get((pk, sk))
        return json.loads(json.dumps(item)) if item else None

    def put(self, pk, sk, data):
        self.items[(pk, sk)] = json.loads(json.dumps({**data, "pk": pk, "sk": sk}, ensure_ascii=False))

    def put_if_absent(self, pk, sk, data):
        if (pk, sk) in self.items:
            return False
        self.put(pk, sk, data)
        return True

    def query(self, pk, sk_prefix=""):
        return [json.loads(json.dumps(v)) for (p, s), v in sorted(self.items.items()) if p == pk and s.startswith(sk_prefix)]

    def scan_prefix(self, sk_prefix):
        return [json.loads(json.dumps(v)) for (p, s), v in sorted(self.items.items()) if s.startswith(sk_prefix)]


_store = None


def store():
    global _store
    if _store is None:
        _store = MemoryStore() if LOCAL_MODE else DynamoStore(TABLE_NAME)
    return _store


def set_store(value):
    global _store
    _store = value


# ---------------------------------------------------------------------------
# Email content: Connect keeps the body/attachments in S3 behind references.
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip, self.links = [], 0, []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "head"}:
            self.skip += 1
        elif tag in {"br", "p", "div", "li", "tr", "table"}:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self.links.append({"href": href, "text_start": len(self.parts)})

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style", "head"} and self.skip:
            self.skip -= 1
        elif tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")
        elif tag == "a" and self.links and "text" not in self.links[-1]:
            self.links[-1]["text"] = "".join(self.parts[self.links[-1]["text_start"]:]).strip()

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(content):
    """Return (plain_text, links[{href,text}]) from HTML or plain text."""
    content = content if isinstance(content, str) else str(content or "")
    links = []
    if re.search(r"<\s*(html|body|div|p|br|a|table)\b", content, flags=re.I):
        parser = _TextExtractor()
        parser.feed(content)
        links = [{"href": l.get("href", ""), "text": l.get("text", "")} for l in parser.links]
        content = "".join(parser.parts)
    content = html.unescape(content).replace("\x00", " ")
    content = re.sub(r"[\t\r\f\v]+", " ", content)
    content = re.sub(r" *\n *", "\n", content)
    content = re.sub(r"\n{3,}", "\n\n", content)
    content = re.sub(r" {2,}", " ", content)
    for m in re.finditer(r"https?://[^\s<>\"')]+", content):
        if not any(l["href"] == m.group(0) for l in links):
            links.append({"href": m.group(0), "text": m.group(0)})
    return content.strip()[:MAX_EMAIL_CHARS], links


def segment_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("ValueString", "valueString", "Value", "value"):
            if isinstance(value.get(key), str):
                return value[key]
    return ""


def _file_id(candidate):
    if isinstance(candidate, dict):
        candidate = candidate.get("Arn") or candidate.get("Value") or candidate.get("value")
    if not isinstance(candidate, str) or not candidate.strip():
        return ""
    return candidate.strip().rstrip("/").rsplit("/", 1)[-1]


def list_references(instance_id, contact_id):
    """Return {'body': file_id, 'body_type': ..., 'attachments': [{file_id,name}]}."""
    connect = client("connect", connect_timeout=0.5, read_timeout=2, retries={"max_attempts": 1})
    found = {"body": "", "body_type": "", "attachments": []}
    token = None
    while True:
        req = {"InstanceId": instance_id, "ContactId": contact_id,
               "ReferenceTypes": ["EMAIL_MESSAGE", "EMAIL_MESSAGE_PLAIN_TEXT", "ATTACHMENT"]}
        if token:
            req["NextToken"] = token
        resp = connect.list_contact_references(**req)
        for summary in resp.get("ReferenceSummaryList", []):
            for member, kind in (("EmailMessagePlainText", "text"), ("EmailMessage", "html")):
                ref = summary.get(member)
                if isinstance(ref, dict) and (not found["body"] or (kind == "text" and found["body_type"] != "text")):
                    fid = _file_id(ref.get("Arn") or ref.get("Name"))
                    if fid:
                        found["body"], found["body_type"] = fid, kind
            att = summary.get("Attachment")
            if isinstance(att, dict) and att.get("Status", "APPROVED") in ("APPROVED", None):
                fid = _file_id(att.get("Arn") or att.get("Value"))
                if fid:
                    found["attachments"].append({"file_id": fid, "name": att.get("Name", "attachment")})
        token = resp.get("NextToken")
        if not token:
            return found


def download_attached(instance_id, contact_arn, file_id, max_bytes=4_500_000):
    connect = client("connect", connect_timeout=0.5, read_timeout=2, retries={"max_attempts": 1})
    meta = connect.get_attached_file(InstanceId=instance_id, FileId=file_id,
                                     AssociatedResourceArn=contact_arn, UrlExpiryInSeconds=60)
    url = meta.get("DownloadUrlMetadata", {}).get("Url")
    if not url:
        raise ValueError("GetAttachedFile returned no URL")
    with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 - presigned S3 URL from Connect
        data = resp.read(max_bytes + 1)
    return data[:max_bytes], meta.get("FileName") or "", meta.get("FileSizeInBytes") or len(data)


def decode_email_payload(raw_bytes):
    text = raw_bytes.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if isinstance(payload, dict):
        for key in ("messageContent", "content", "body", "text", "message"):
            if payload.get(key) is not None:
                value = payload[key]
                return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return text


def fetch_email(contact_data):
    """Read body (+ attachment list) of the inbound email contact."""
    instance_arn = contact_data["InstanceARN"]
    instance_id = instance_arn.rsplit("/", 1)[-1]
    contact_id = contact_data["ContactId"]
    contact_arn = f"{instance_arn}/contact/{contact_id}"
    refs = list_references(instance_id, contact_id)
    body_text, links = "", []
    if refs["body"]:
        raw, _, _ = download_attached(instance_id, contact_arn, refs["body"])
        body_text, links = html_to_text(decode_email_payload(raw))
    return {"body": body_text, "links": links, "attachments": refs["attachments"],
            "instance_id": instance_id, "contact_arn": contact_arn}


def load_attachment_blocks(instance_id, contact_arn, attachments, limit=4):
    """Turn attachments into Bedrock Converse content blocks (image / document)."""
    blocks, notes = [], []
    image_formats = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "gif": "gif", "webp": "webp"}
    doc_formats = {"pdf": "pdf", "docx": "docx", "doc": "doc", "xlsx": "xlsx", "xls": "xls", "csv": "csv",
                   "txt": "txt", "html": "html", "md": "md"}
    for att in attachments[:limit]:
        name = att.get("name") or "attachment"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        try:
            data, _, size = download_attached(instance_id, contact_arn, att["file_id"])
        except Exception as exc:  # attachment problems must never block the reply
            notes.append(f"{name}: could not be read ({type(exc).__name__})")
            continue
        safe_name = re.sub(r"[^A-Za-z0-9 ()\[\]-]", "-", name.rsplit(".", 1)[0])[:60] or "attachment"
        if ext in image_formats and size <= 3_750_000:
            blocks.append({"image": {"format": image_formats[ext], "source": {"bytes": data}}})
            notes.append(f"{name}: image attached for review")
        elif ext in doc_formats and size <= 4_500_000:
            blocks.append({"document": {"format": doc_formats[ext], "name": safe_name, "source": {"bytes": data}}})
            notes.append(f"{name}: document attached for review")
        else:
            notes.append(f"{name}: unsupported type or too large, not read by AI")
    return blocks, notes


# ---------------------------------------------------------------------------
# Model helpers
# ---------------------------------------------------------------------------

def converse(model_id, system, messages, max_tokens=1500, temperature=0, tools=None, read_timeout=60):
    bedrock = client("bedrock-runtime", connect_timeout=1, read_timeout=read_timeout,
                     retries={"max_attempts": 2, "mode": "adaptive"})
    req = {"modelId": model_id, "system": system, "messages": messages,
           "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature}}
    if tools:
        req["toolConfig"] = tools
    return bedrock.converse(**req)


def parse_json_text(text):
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


def attr(value, limit=1000):
    """Connect attribute value: non-empty string, bounded length."""
    if value is None or value == "" or value == []:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    value = str(value)
    return value[:limit] if len(value) > limit else value


def normalize_subject(subject):
    s = subject or ""
    s = re.sub(r"\[Ref:\s*EA-[A-Z0-9]+\]", "", s, flags=re.I)
    while True:
        new = re.sub(r"^\s*(re|fw|fwd|aw|sv|antw|回复|回覆|答复|转发|轉寄)\s*[:：]\s*", "", s, flags=re.I)
        if new == s:
            break
        s = new
    return re.sub(r"\s+", " ", s).strip().lower()


def new_case_id():
    return "EA-" + base64.b32encode(os.urandom(5)).decode().rstrip("=")


def elapsed_ms(start):
    return int((time.time() - start) * 1000)


def within(dt_iso, days):
    try:
        return now_utc() - datetime.fromisoformat(dt_iso) <= timedelta(days=days)
    except (TypeError, ValueError):
        return False
