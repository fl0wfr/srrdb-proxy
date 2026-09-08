import asyncio
import logging
import os

from lxml import etree as ET
from fastapi import FastAPI, HTTPException, Request, Response

from .cache import Cache
from .prowlarr import Prowlarr
from .srrdb import LOGIC_VERSION as SRRDB_LOGIC_VERSION
from .srrdb import Srrdb

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("srrdb-proxy")

app = FastAPI(title="srrDB Prowlarr Proxy")

PROWLARR_URL = os.getenv("PROWLARR_URL", "http://prowlarr:9696")
PROWLARR_API_KEY = os.getenv("PROWLARR_API_KEY", "")
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
FAIL_CLOSED = os.getenv("FAIL_CLOSED", "true").lower() == "true"
POSITIVE_TTL = int(os.getenv("POSITIVE_TTL", "604800"))
NEGATIVE_TTL = int(os.getenv("NEGATIVE_TTL", "21600"))
TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15"))
CONCURRENCY = int(os.getenv("SRRDB_CONCURRENCY", "4"))
# Upper bound on total time spent checking a single indexer response
# against srrDB, no matter how many unique titles it contains. Without
# this, a feed full of releases that aren't on srrDB (very common — it's
# a scene DB, not exhaustive) means one outbound HTTP round trip per
# title; even in parallel a slow/rate-limited srrDB can drag a request
# out well past Sonarr/Radarr's connection-test timeout, which is what
# gets an indexer flagged unavailable in Prowlarr.
CHECK_BUDGET = float(os.getenv("CHECK_BUDGET_SECONDS", "20"))
DB_PATH = os.getenv("DB_PATH", "/data/cache.sqlite3")

cache = Cache(DB_PATH)
prowlarr = Prowlarr(PROWLARR_URL, PROWLARR_API_KEY, TIMEOUT)
srrdb = Srrdb(TIMEOUT, CONCURRENCY)

def auth(request: Request):
    if PROXY_API_KEY and request.query_params.get("apikey") != PROXY_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

def local_name(tag) -> str:
    # lxml gives Comment/PI nodes a callable (non-str) `.tag`; treat those
    # as "no name" so they never accidentally match "item"/"channel"/"title".
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]

async def check_release(title: str) -> tuple[bool, str]:
    cached = cache.get(title, SRRDB_LOGIC_VERSION, POSITIVE_TTL, NEGATIVE_TTL)

    if cached is not None:
        return cached, "CACHE_FOUND" if cached else "CACHE_NOT_FOUND"

    found, reason = await srrdb.exists(title)

    if reason.startswith("ERROR"):
        log.error("[SRRDB] %s -> %s", title, reason)
        if FAIL_CLOSED:
            return False, "SRRDB_ERROR_FAIL_CLOSED"
        return True, "SRRDB_ERROR_FAIL_OPEN"

    cache.put(title, SRRDB_LOGIC_VERSION, found)
    return found, reason

async def check_titles(titles: list[str], indexer_id: int) -> dict[str, tuple[bool, str]]:
    """
    Check every title concurrently (actual outbound HTTP concurrency is
    capped by the semaphore inside Srrdb, via SRRDB_CONCURRENCY), bounded
    by an overall CHECK_BUDGET so one slow/rate-limited srrDB response
    can't stall the whole proxy request. Titles still unresolved when the
    budget runs out get the same FAIL_CLOSED/FAIL_OPEN treatment as a
    genuine srrDB error — no new, undocumented failure mode.
    """
    if not titles:
        return {}

    tasks = {title: asyncio.create_task(check_release(title)) for title in titles}
    _done, pending = await asyncio.wait(tasks.values(), timeout=CHECK_BUDGET)

    results = {}
    for title, task in tasks.items():
        if task in pending:
            task.cancel()
            found = not FAIL_CLOSED
            reason = "BUDGET_EXCEEDED_FAIL_OPEN" if found else "BUDGET_EXCEEDED_FAIL_CLOSED"
            log.warning(
                "[SRRDB] indexer=%s %s -> budget of %.0fs exceeded (%s)",
                indexer_id, title, CHECK_BUDGET, reason,
            )
        else:
            try:
                found, reason = task.result()
            except Exception as exc:
                found, reason = (not FAIL_CLOSED), "UNEXPECTED_ERROR"
                log.error("[SRRDB] indexer=%s %s -> unexpected error: %s", indexer_id, title, exc)
            log.info(
                "SRRDB indexer=%s %s -> %s (%s)",
                indexer_id, title, "FOUND" if found else "MISSING", reason,
            )
        results[title] = (found, reason)

    if pending:
        # Let cancellation actually complete instead of leaving the tasks
        # dangling (avoids "Task was destroyed but it is pending" noise).
        await asyncio.gather(*pending, return_exceptions=True)

    return results

def item_title(item) -> str:
    title_el = next(
        (child for child in item if local_name(child.tag) == "title"),
        None
    )
    return (title_el.text or "").strip() if title_el is not None else ""

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/indexers")
async def get_indexers(request: Request):
    auth(request)
    return await prowlarr.indexers()

@app.get("/{indexer_id}/api")
@app.get("/{indexer_id}/newznab")
async def proxy_newznab(indexer_id: int, request: Request):
    auth(request)

    params = dict(request.query_params)
    params.pop("apikey", None)

    upstream = await prowlarr.newznab(indexer_id, params)

    if upstream.status_code >= 400:
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/xml"),
        )

    # Capabilities are not releases: always pass through unchanged.
    if params.get("t", "").lower() == "caps":
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/xml"),
        )

    parser = ET.XMLParser(strip_cdata=False, recover=False)
    try:
        root = ET.fromstring(upstream.content, parser=parser)
    except ET.XMLSyntaxError:
        log.error("Invalid XML returned by Prowlarr indexer=%s", indexer_id)
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/xml"),
        )

    # "Real" items are only the direct <item> children of <channel> — not
    # any element named "item" anywhere in the document (e.g. inside an
    # unrelated namespace, or nested oddly). Everything outside <channel>,
    # and every non-<item> child of <channel>, is left completely alone.
    channel = next(
        (child for child in root.iter() if local_name(child.tag) == "channel"),
        None
    )

    if channel is None:
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/xml"),
        )

    items = [child for child in channel if local_name(child.tag) == "item"]

    if not items:
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/xml"),
        )

    item_titles = [item_title(item) for item in items]

    # Dedupe *only* for the srrDB lookups (no point checking the same
    # release twice). Counting and removal below always work off
    # `item_titles` / `items` themselves, one entry per real <item>, so a
    # duplicate title can never desync accepted/rejected/removed.
    unique_titles = []
    seen = set()
    for title in item_titles:
        if title and title not in seen:
            seen.add(title)
            unique_titles.append(title)

    title_results = await check_titles(unique_titles, indexer_id)

    # One verdict per real <item>, in item order. An item with no <title>
    # can't be validated, so it's treated as rejected (fail closed) — and,
    # crucially, it's now counted as rejected too, instead of silently
    # skipping the count but still deleting it.
    item_results = [
        title_results[title] if title else (False, "NO_TITLE")
        for title in item_titles
    ]

    accepted = sum(1 for found, _ in item_results if found)
    rejected = len(item_results) - accepted

    # IMPORTANT:
    # Dry-run does not modify the response at all.
    # Sonarr/Radarr receive the exact XML returned by Prowlarr, byte for byte.
    if DRY_RUN:
        log.info(
            "DRY-RUN indexer=%s accepted=%d rejected=%d removed=0",
            indexer_id,
            accepted,
            rejected,
        )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/xml"),
        )

    # In non-dry-run, filtering is the *only* thing done to the document:
    # we remove exactly the rejected <item> elements from <channel> and
    # touch nothing else. Because lxml keeps each element's original
    # namespace/prefix bindings (unlike stdlib ElementTree, which reassigns
    # ns0/ns1/... on serialization), an accepted item's <link>, <enclosure>,
    # ns1:attr, etc. round-trip exactly as Prowlarr sent them.
    removed = 0
    for item, (found, _reason) in zip(items, item_results):
        if not found:
            channel.remove(item)
            removed += 1

    # removed is rejected by construction (one remove() per rejected item,
    # one item per entry in item_results) — this assertion just documents
    # and guards that invariant rather than relying on it silently.
    assert removed == rejected

    source_encoding = root.getroottree().docinfo.encoding or "utf-8"
    xml = ET.tostring(root, encoding=source_encoding, xml_declaration=True)

    log.info(
        "FILTERED indexer=%s accepted=%d rejected=%d removed=%d",
        indexer_id,
        accepted,
        rejected,
        removed,
    )

    return Response(
        content=xml,
        status_code=upstream.status_code,
        media_type="application/xml",
    )
