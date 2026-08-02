from .models import Document


def to_v2(document: Document):
    return {"version": 2, "id": document.document_id,
            "fields": {"title": document.title, "body": document.body},
            "labels": list(document.tags)}


def serialize_result(document: Document):
    return {"document_id": document.document_id, "title": document.title,
            "snippet": document.body[:40], "tags": list(document.tags)}
