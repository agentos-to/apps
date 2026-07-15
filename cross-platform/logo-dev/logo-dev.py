from agentos import connection, returns, timeout, url


connection(
    'api',
    auth={'type': 'api_key', 'header': {'Authorization': '"Bearer " + .auth.key'}},
    label='Publishable Key',
    help_url='https://www.logo.dev/dashboard')


CDN = "https://img.logo.dev"


def _base_url(path: str, token: str, size: int, format: str) -> str:
    return f"{CDN}/{path}?token={token}&size={size}&format={format}"


@returns({"url": "string"})
@connection("api")
@timeout(5)
async def logo_url(*, domain: str, size: int = 128, format: str = "png",
             theme: str = "auto", fallback: str = "404", **params) -> dict:
    """Return CDN URL for a company logo by domain.

    `domain` may be a bare domain (`github.com`), a host with subdomains
    (`mail.google.com`), or a full URL (`https://mail.google.com/inbox`) — it is
    normalized to the registrable domain (`google.com`) so the caller can pass
    an app's website verbatim and never hand-strip it.

    `fallback` decides what a *missing* logo returns. logo.dev's own default is a
    generated monogram tile — a 200 that's indistinguishable from a real logo, so
    "this brand has no logo" is silently undetectable. We default to `"404"`:
    logo.dev returns HTTP 404 when it has no logo, so a consumer (e.g. an icon
    setter) can honestly skip instead of storing a fake face. Pass
    `fallback="monogram"` to opt back into the letter-tile.

        Args:
            domain: Domain, host, or full URL (e.g. shopify.com).
            size: Size in pixels (16-800).
            format: Image format (jpg, png, webp).
            theme: Theme (auto, light, dark).
            fallback: "404" (missing → HTTP 404, detectable) or "monogram".
        """
    token = params.get("auth", {}).get("key", "")
    # Accept a bare domain, a subdomain host, or a full URL → registrable apex.
    p = url.parse(domain)
    host = (p.host or p.path or domain).split("/")[0]
    apex = url.registrable(host)
    u = _base_url(apex, token, size, format) + f"&theme={theme}&fallback={fallback}"
    return {"url": u}


@returns({"url": "string"})
@connection("api")
@timeout(5)
async def ticker_url(*, ticker: str, size: int = 128, format: str = "png",
               **params) -> dict:
    """Return CDN URL for a company logo by stock ticker

        Args:
            ticker: Stock ticker (e.g., AAPL)
            size: Size in pixels
            format: Image format
        """
    token = params.get("auth", {}).get("key", "")
    return {"url": _base_url(f"ticker:{ticker}", token, size, format)}


@returns({"url": "string"})
@connection("api")
@timeout(5)
async def name_url(*, name: str, size: int = 128, format: str = "png",
             **params) -> dict:
    """Return CDN URL for a company logo by name

        Args:
            name: Company name (e.g., Shopify)
            size: Size in pixels
            format: Image format
        """
    token = params.get("auth", {}).get("key", "")
    return {"url": _base_url(f"name:{url.encode(name)}", token, size, format)}


@returns({"url": "string"})
@connection("api")
@timeout(5)
async def crypto_url(*, symbol: str, size: int = 128, format: str = "png",
               **params) -> dict:
    """Return CDN URL for a cryptocurrency logo

        Args:
            symbol: Crypto symbol (e.g., BTC, ETH)
            size: Size in pixels
            format: Image format
        """
    token = params.get("auth", {}).get("key", "")
    return {"url": _base_url(f"crypto:{symbol}", token, size, format)}
