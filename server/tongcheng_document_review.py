"""Explicit operator approval bound to immutable product-library row versions."""
import hashlib
import json
from datetime import datetime, timezone


def approved_documents(documents, reviews, now=None):
    now=now or datetime.now(timezone.utc)
    result=[]
    for doc in documents:
        review=reviews.get(doc['id'])
        if not review or not review.get('enabled') or review.get('source_sha256')!=doc['source_sha256'] or review.get('content_hash')!=doc['content_hash']:
            continue
        try:
            if not datetime.fromisoformat(review['valid_from'])<=now<datetime.fromisoformat(review['expires_at']):continue
        except (ValueError,TypeError,KeyError):continue
        result.append(dict(doc,approval=review))
    return result


def fingerprint(products, evidence, documents):
    by_id={d['id']:d for d in documents}
    values=[]
    for ref in evidence:
        doc=by_id.get(ref['document_id'])
        if not doc or doc['source_sha256']!=ref['source_sha256']:return None
        values.append([doc['id'],doc['content_hash'],doc['approval']])
    return hashlib.sha256(json.dumps([products,values],ensure_ascii=False,sort_keys=True).encode()).hexdigest() if values else None
