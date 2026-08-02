class RolloutEngineError(Exception):
    pass


class ConfigurationError(RolloutEngineError):
    def __init__(self, message: str, *, field: str):
        super().__init__(message)
        self.field = field


class ContextError(RolloutEngineError):
    def __init__(self, message: str, *, field: str):
        super().__init__(message)
        self.field = field


class UnknownFlag(RolloutEngineError):
    def __init__(self, flag_key: str):
        super().__init__(f"unknown flag: {flag_key}")
        self.flag_key = flag_key


class RequestConflict(RolloutEngineError):
    def __init__(self, request_id: str):
        super().__init__(f"request conflict: {request_id}")
        self.request_id = request_id


class EvaluationCycle(RolloutEngineError):
    def __init__(self, flag_key: str):
        super().__init__(f"evaluation cycle at: {flag_key}")
        self.flag_key = flag_key
