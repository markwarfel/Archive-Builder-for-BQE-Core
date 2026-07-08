from __future__ import annotations
import json, time, urllib.error, urllib.parse, urllib.request
import auth


def _get(path: str, params: dict) -> list:
    """Fetch one page from the BQE API, with one retry on 401 and one on 429."""
    endpoint = auth.tokens.get("endpoint", "").rstrip("/")
    url = f"{endpoint}/{path}?" + urllib.parse.urlencode(params)

    def _req():
        r = urllib.request.Request(url)
        r.add_header("Authorization", f"Bearer {auth.tokens['access_token']}")
        r.add_header("Accept", "application/json")
        with urllib.request.urlopen(r) as resp:
            data = resp.read()
            return json.loads(data) if data.strip() else []

    try:
        return _req()
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and auth.refresh_access_token():
            return _req()
        if exc.code == 429:
            retry_after = (exc.headers or {}).get("Retry-After", "60")
            wait = float(retry_after) if str(retry_after).isdigit() else 60.0
            time.sleep(wait)
            return _req()
        raise


def fetch_all(path: str,
              extra_params: dict | None = None,
              on_page: callable | None = None,
              page_size: int = 1000,
              max_pages: int | None = None) -> list:
    """Paginate through a BQE endpoint, returning all items across all pages.

    BQE uses 1-based page numbers (page=1 is the first page). Starting at
    page=0 causes the first chunk to be returned twice, so ID deduplication
    is applied to drop the phantom duplicate before it reaches the aggregation.

    Max pageSize: 1000 without expand, 100 with expand=*.
    """
    page_num = 0
    all_items: list = []
    seen_ids: set = set()
    while True:
        if max_pages is not None and page_num >= max_pages:
            break
        params = {"page": f"{page_num},{page_size}"}
        if extra_params:
            params.update(extra_params)
        items = _get(path, params)
        if not isinstance(items, list) or not items:
            break
        new_items = [it for it in items if it.get("id") not in seen_ids]
        seen_ids.update(it["id"] for it in items if it.get("id"))
        all_items.extend(new_items)
        if on_page:
            on_page(len(all_items))
        page_num += 1
        time.sleep(0.25)  # 4 req/s — well under BQE's per-minute rate limit
    return all_items
