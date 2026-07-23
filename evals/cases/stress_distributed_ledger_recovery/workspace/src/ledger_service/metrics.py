"""Small metrics value object used by future adapters."""


class LedgerMetrics:
    def __init__(self):
        self.ingested_batches = 0

    def record_ingest(self):
        self.ingested_batches += 1
