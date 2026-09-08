import hashlib
import inspect
from urllib.parse import quote

import httpx

BASE_URL = "https://api.srrdb.com/v1"
USER_AGENT = "srrdb-proxy/2.1"


class Srrdb:
    def __init__(self, timeout: float, concurrency: int):
        import asyncio
        self.timeout = timeout
        self.sem = asyncio.Semaphore(concurrency)

    async def exists(self, release: str) -> tuple[bool, str]:
        # Public srrDB API: no API key.
        url = f"{BASE_URL}/search/{quote(release, safe='')}"

        async with self.sem:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    r = await client.get(
                        url,
                        headers={"User-Agent": USER_AGENT}
                    )
                    r.raise_for_status()
                    data = r.json()
            except Exception as exc:
                return False, f"ERROR: {exc}"

        # Require an exact release-name match.
        for item in data.get("results", []):
            if item.get("release") == release:
                return True, "FOUND"

        if data.get("resultsCount", 0):
            return False, "NO_EXACT_MATCH"

        return False, "NOT_FOUND"


# Fingerprint of the matching/validation logic above (+ the endpoint it
# hits). Bundled into every cache row (see cache.Cache). If you edit
# `exists()` — e.g. change exact-match to fuzzy match, add cross-checks,
# hit a different endpoint — this hash changes automatically and every
# previously cached verdict is transparently treated as a cache miss and
# re-checked, with no manual cache flush and no waiting out the TTL.
LOGIC_VERSION = hashlib.sha256(
    (BASE_URL + inspect.getsource(Srrdb.exists)).encode("utf-8")
).hexdigest()[:16]
