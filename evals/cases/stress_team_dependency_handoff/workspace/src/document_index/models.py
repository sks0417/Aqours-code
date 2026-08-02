from dataclasses import dataclass


@dataclass(frozen=True)
class Document:
    document_id: str
    title: str
    body: str
    tags: tuple[str, ...]
