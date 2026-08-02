from collections import Counter

from .errors import RateLimitExceeded


class RecipientRateLimiter:
    def __init__(self, quota):
        self.quota = quota
        self._usage = Counter()

    def consume(self, recipient):
        if self._usage[recipient] >= self.quota:
            raise RateLimitExceeded(f"quota exhausted for {recipient}")
        self._usage[recipient] += 1

    def usage(self, recipient):
        return self._usage[recipient]
