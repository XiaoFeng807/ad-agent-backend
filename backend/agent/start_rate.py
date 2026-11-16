"""Shared request rate for the two StorePilot Envoys in a single app instance."""

from threading import Lock
from time import monotonic


class AccountStartRate:
    def __init__(self, requests_per_second):
        if requests_per_second < 0:
            raise ValueError("STOREPILOT_STARTS_PER_SECOND must be nonnegative")
        self.limit = requests_per_second
        self._lock = Lock()
        self._second = -1
        self._counts = {}

    def allow(self, user_id):
        with self._lock:
            second = int(monotonic())
            if second != self._second:
                self._counts.clear()
                self._second = second
            count = self._counts.get(user_id, 0)
            if count >= self.limit:
                return False
            self._counts[user_id] = count + 1
            return True
