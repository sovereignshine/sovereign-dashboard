import time
from threading import Lock

# Cache TTLs per preset (seconds)
PRESET_TTL = {
    'today':        5 * 60,        #  5 min  — intraday, refresh aggressively
    'yesterday':    30 * 60,       # 30 min  — done day, backfill can still shift
    'last_7_days':  60 * 60,       #  1 hr
    'last_14_days': 2 * 60 * 60,   #  2 hr
    'last_30_days': 4 * 60 * 60,   #  4 hr
    # legacy
    'live':        5 * 60,
    'hourly':      30 * 60,
    '4hr':         60 * 60,
    'daily':       60 * 60,
    'weekly':      4 * 60 * 60,
    'this_month':  4 * 60 * 60,
    'last_month':  12 * 60 * 60,
    'last_90_days':12 * 60 * 60,
    'last_year':   24 * 60 * 60,
}

class Cache:
    def __init__(self):
        self._store = {}
        self._lock  = Lock()

    def get(self, key):
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            if time.time() > entry['expires']:
                del self._store[key]
                return None
            return entry['data']

    def set(self, key, data, ttl=3600):
        with self._lock:
            self._store[key] = {
                'data':    data,
                'expires': time.time() + ttl,
            }

    def ttl_for(self, preset):
        return PRESET_TTL.get(preset, 3600)

    def invalidate(self, key):
        with self._lock:
            self._store.pop(key, None)

    def clear(self):
        with self._lock:
            self._store.clear()
