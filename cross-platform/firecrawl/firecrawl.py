"""Firecrawl — browser-rendered web scraping via API."""

from agentos import connection, provides, returns, client


connection(
    'api',
    base_url='https://api.firecrawl.dev/v2',
    auth={'type': 'api_key', 'header': {'Authorization': '"Bearer " + .auth.key'}},
    label='API Key',
    help_url='https://www.firecrawl.dev/app/api-keys')


API_BASE = "https://api.firecrawl.dev/v2"


@returns("document")
@provides("web_fetch")
@connection("api")
async def read_webpage(*, url: str, wait_for_js: int = 0, timeout: int = 30000, **params) -> dict:
    """Read a URL with browser rendering (handles JS-heavy sites)."""
    api_key = params.get("auth", {}).get("key", "")
    resp = await client.post(
        f"{API_BASE}/scrape",
        json={
            "url": url,
            # branding carries the page's own favicon URL — the source
            # tells us, we never derive it.
            "formats": ["markdown", "branding"],
            "onlyMainContent": True,
            "waitFor": wait_for_js,
            "timeout": timeout,
        }, headers={"Authorization": f"Bearer {api_key}"},
    )
    data = (resp["json"] or {}).get("data") or {}
    meta = data.get("metadata") or {}
    return {
        "id": meta.get("sourceURL") or meta.get("url") or url,
        "name": meta.get("title") or meta.get("ogTitle"),
        "content": data.get("markdown") or meta.get("description"),
        "url": meta.get("sourceURL") or meta.get("url") or url,
        "image": meta.get("ogImage") or meta.get("image") or meta.get("og:image"),
        "favicon": ((data.get("branding") or {}).get("images") or {}).get("favicon"),
        "author": meta.get("author") or meta.get("article:author"),
        "published": (
            meta.get("publishedTime")
            or meta.get("publishedDate")
            or meta.get("article:published_time")
        ),
        "contentType": "text/markdown",
    }
