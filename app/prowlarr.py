import httpx

class Prowlarr:
    def __init__(self, base_url: str, api_key: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Api-Key": api_key}
        self.timeout = timeout

    async def newznab(self, indexer_id: int, params: dict) -> httpx.Response:
        url = f"{self.base_url}/api/v1/indexer/{indexer_id}/newznab"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await client.get(url, params=params, headers=self.headers)

    async def indexers(self):
        url = f"{self.base_url}/api/v1/indexer"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(url, headers=self.headers)
            r.raise_for_status()
            return r.json()
