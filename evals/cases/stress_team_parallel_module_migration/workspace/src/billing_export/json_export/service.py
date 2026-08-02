class JSONExportService:
    def __init__(self, encoder):
        self.encoder = encoder

    def export(self, records):
        return self.encoder.encode(records)
