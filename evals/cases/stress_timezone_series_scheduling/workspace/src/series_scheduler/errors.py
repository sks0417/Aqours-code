class SchedulerError(Exception):
    pass


class ValidationError(SchedulerError):
    def __init__(self, message: str, *, field: str):
        super().__init__(message)
        self.field = field


class UnknownResource(SchedulerError):
    def __init__(self, resource_id: str):
        super().__init__(f"unknown resource: {resource_id}")
        self.resource_id = resource_id


class DuplicateSeries(SchedulerError):
    def __init__(self, series_id: str):
        super().__init__(f"duplicate series: {series_id}")
        self.series_id = series_id


class UnknownSeries(SchedulerError):
    def __init__(self, series_id: str):
        super().__init__(f"unknown series: {series_id}")
        self.series_id = series_id


class UnknownOccurrence(SchedulerError):
    def __init__(self, series_id: str, original_start: str):
        super().__init__(f"unknown occurrence: {series_id} at {original_start}")
        self.series_id, self.original_start = series_id, original_start


class OccurrenceCancelled(SchedulerError):
    def __init__(self, series_id: str, original_start: str):
        super().__init__(f"occurrence cancelled: {series_id} at {original_start}")
        self.series_id, self.original_start = series_id, original_start


class ScheduleConflict(SchedulerError):
    def __init__(self, resource_id: str, start: str):
        super().__init__(f"schedule conflict: {resource_id} at {start}")
        self.resource_id, self.start = resource_id, start


class CapacityExceeded(SchedulerError):
    def __init__(self, series_id, occurrence_start, requested, available):
        super().__init__(f"capacity exceeded: {series_id} at {occurrence_start}")
        self.series_id = series_id
        self.occurrence_start = occurrence_start
        self.requested = requested
        self.available = available


class IdempotencyConflict(SchedulerError):
    def __init__(self, key: str):
        super().__init__(f"idempotency conflict: {key}")
        self.key = key


class RequestConflict(SchedulerError):
    def __init__(self, request_id: str):
        super().__init__(f"request conflict: {request_id}")
        self.request_id = request_id
