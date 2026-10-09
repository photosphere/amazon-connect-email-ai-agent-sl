"""Knowledge-base retrieval.

Default: BM25 over data/kb_index.json (built by tools/build_kb_index.py from the
00_Vendor KB PDFs), scoped to the hotel in question plus shared documents.
Tokenisation handles English words and Chinese character bigrams, so the same
index serves English, Simplified and Traditional Chinese queries.

Optional: BEDROCK_KB_ID (set by ../setup_kb.sh, Bedrock mode) switches to an Amazon Bedrock
Knowledge Base on S3 Vectors (Retrieve API), filtered by the hotel_code document metadata.
See ../README_kb.md.
"""
import math
import os
import re
from collections import Counter

from common import client, load_json

BEDROCK_KB_ID = os.environ.get("BEDROCK_KB_ID", "")

# Documents every hotel query may also draw on.
SHARED_SCOPES = {
    "HGH1": ["HGH1", "GENERAL", "BOOKING"],
    "SZX1": ["SZX1", "GENERAL", "BOOKING"],
    "PEN1": ["PEN1", "PEN2", "GENERAL", "BOOKING"],   # PEN1 guests may use PEN2 kids facilities
    "PEN2": ["PEN2", "PEN1", "GENERAL", "BOOKING"],
    "LOYALTY": ["LOYALTY", "GENERAL"],
}
_T2S = str.maketrans("會員積分兌換賬戶預訂取消確認房間酒店餐廳機場接送電話郵件點數禮遇隊獎賞網上號碼", "会员积分兑换账户预订取消确认房间酒店餐厅机场接送电话邮件点数礼遇队奖赏网上号码")


def tokenize(text):
    text = (text or "").lower().translate(_T2S)
    tokens = re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)*", text)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return [t for t in tokens if t not in _STOP]


_STOP = set("a an the of to and or in on at for is are be by with from as it this that can do does what which how i you we our your my me any please there".split())


class _Bm25:
    def __init__(self, chunks, k1=1.4, b=0.75):
        self.chunks = chunks
        self.docs = [tokenize(c["category"] + " " + c["text"]) for c in chunks]
        self.tf = [Counter(d) for d in self.docs]
        self.avg = sum(len(d) for d in self.docs) / max(1, len(self.docs))
        df = Counter(t for d in self.docs for t in set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.k1, self.b = k1, b

    def score(self, q_tokens, i):
        tf, dl, s = self.tf[i], len(self.docs[i]), 0.0
        for t in q_tokens:
            if t in tf:
                f = tf[t]
                s += self.idf.get(t, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avg))
        return s


_index = None


def _bm25():
    global _index
    if _index is None:
        _index = _Bm25(load_json("kb_index.json"))
    return _index


def search(query, hotel_code=None, top_k=6, max_chars=1400):
    if BEDROCK_KB_ID:
        return _bedrock_search(query, hotel_code, top_k, max_chars)
    idx = _bm25()
    scopes = SHARED_SCOPES.get((hotel_code or "").upper())
    q = tokenize(query)
    scored = []
    for i, chunk in enumerate(idx.chunks):
        if scopes and chunk["hotel_code"] not in scopes:
            continue
        s = idx.score(q, i)
        if s > 0:
            if scopes and chunk["hotel_code"] == scopes[0]:
                s *= 1.3  # prefer the hotel's own documents
            scored.append((s, i))
    scored.sort(reverse=True)
    results = []
    for s, i in scored[:top_k]:
        c = idx.chunks[i]
        results.append({"chunk_id": c["id"], "hotel_code": c["hotel_code"], "source": c["source"],
                        "page": c["page"], "score": round(s, 2), "text": c["text"][:max_chars]})
    return results


def _bedrock_search(query, hotel_code, top_k, max_chars):
    """Bedrock Knowledge Base (created by setup_kb.sh). Each document carries a hotel_code metadata
    attribute (sidecar .metadata.json), so retrieval is scoped the same way as the local index."""
    runtime = client("bedrock-agent-runtime", read_timeout=10)
    scopes = SHARED_SCOPES.get((hotel_code or "").upper())
    vcfg = {"numberOfResults": top_k}
    if scopes:
        vcfg["filter"] = {"orAll": [{"equals": {"key": "hotel_code", "value": c}} for c in scopes]}
    try:
        resp = runtime.retrieve(knowledgeBaseId=BEDROCK_KB_ID, retrievalQuery={"text": query},
                                retrievalConfiguration={"vectorSearchConfiguration": vcfg})
    except runtime.exceptions.ValidationException:
        vcfg.pop("filter", None)  # e.g. metadata not ingested yet - fall back to unscoped search
        resp = runtime.retrieve(knowledgeBaseId=BEDROCK_KB_ID, retrievalQuery={"text": query},
                                retrievalConfiguration={"vectorSearchConfiguration": vcfg})
    out = []
    for r in resp.get("retrievalResults", []):
        meta = r.get("metadata") or {}
        uri = (r.get("location") or {}).get("s3Location", {}).get("uri", "bedrock-kb")
        page = meta.get("x-amz-bedrock-kb-document-page-number")
        page = int(page) if isinstance(page, (int, float)) else page
        source = meta.get("source") or uri.split("/kb/", 1)[-1]
        out.append({"chunk_id": f"{meta.get('hotel_code', 'KB')}:{source}" + (f":p{page}" if page else ""),
                    "hotel_code": meta.get("hotel_code", hotel_code), "source": source, "page": page,
                    "score": round(r.get("score", 0), 3), "text": r.get("content", {}).get("text", "")[:max_chars]})
    return out
