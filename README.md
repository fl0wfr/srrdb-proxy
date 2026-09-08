# srrdb-proxy
Proxy between Sonarr/Radarr and Prowlarr's Newznab/Torznab endpoints.
Every release returned by Prowlarr is checked against the public srrDB
API:
    https://api.srrdb.com/v1/search/<release>
No srrDB API key is required.

## Multiple indexers
The path contains the Prowlarr indexer ID:
    http://srrdb-proxy:8080/15/api
    http://srrdb-proxy:8080/18/api
    http://srrdb-proxy:8080/23/api
The proxy forwards each request to:
    /api/v1/indexer/<ID>/newznab
The srrDB cache is shared across all indexers and is keyed by release
name, not by indexer ID.

## srrDB validation
A release is ACCEPTED only if an entry in `results` has:
    results[].release == <title>
No fuzzy matching is used.
Example:
    Prowlarr title:
    Minions.and.Monsters.2026.COMPLETE.BLURAY-UNTOUCHED
    srrDB:
    results[].release =
    Minions.and.Monsters.2026.COMPLETE.BLURAY-UNTOUCHED
    => ACCEPT
A `results: []` response => REJECT.

## DRY_RUN
By default:
    DRY_RUN=true
The proxy performs the srrDB lookups but returns Prowlarr's original XML
without removing a single `<item>`.
The logs still show the results, though:
    SRRDB indexer=15 ... -> FOUND
    SRRDB indexer=15 ... -> MISSING
    DRY-RUN indexer=15 accepted=2 rejected=18 removed=0
Once the behavior is validated:
    DRY_RUN=false
Releases not found on srrDB are then removed from the XML.

## Cache
SQLite:
    /data/cache.sqlite3
Defaults:
- positive result: 7 days
- negative result: 6 hours

## FAIL_CLOSED
With:
    FAIL_CLOSED=true
an srrDB error or timeout results in a rejection.
With:
    FAIL_CLOSED=false
an srrDB error lets the release through.

## Installation
Copy `.env.example` to `.env`, and fill in:
    PROWLARR_API_KEY=...
    PROXY_API_KEY=...
Then:
    docker compose up -d --build
The `arr` Docker network is used by default and must already exist if
you keep `external: true`.

## Testing
    curl 'http://srrdb-proxy:8080/15/api?t=caps&apikey=CHANGE_ME'
Then:
    curl 'http://srrdb-proxy:8080/15/api?t=tvsearch&q=Test&apikey=CHANGE_ME'
Logs:
    docker logs -f srrdb-proxy

## A note on Prowlarr sync
If Prowlarr automatically syncs indexers to Sonarr/Radarr, it can
overwrite the proxy URL with Prowlarr's original URL.
You'll need to disable that sync for these indexers, or manually keep
the proxy URLs set in Sonarr/Radarr.
