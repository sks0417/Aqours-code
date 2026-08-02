from dataclasses import dataclass


@dataclass(frozen=True)
class InventoryRow:
    external_id: str
    sku: str
    quantity: int


@dataclass(frozen=True)
class ImportResult:
    batch_id: str
    imported_count: int
    external_ids: tuple[str, ...]
