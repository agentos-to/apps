"""Reddit — live Reddit through a reddit.com tab in the engine's HEADLESS
background profile.

Every read is a **same-origin `fetch()`** of Reddit's own `.json` endpoints,
evaluated inside a reddit.com tab via the `browser_session` service — the Exa
pattern. The browser profile IS the session; requests originate from the real
browser, so Reddit's bot-detection / JS-challenge (`js_challenge=1`, the
Cloudflare-style interstitial) clears invisibly and the fetch is never 403'd —
the failure mode of the old plain-HTTP connector. No window opens for a read;
the posts/communities surface in whatever app renders the payload (rule 19).

Reddit exposes a full JSON API by appending `.json` to any path:
  /search.json?q=…                     sitewide post search
  /r/<sub>/search.json?restrict_sr=1   in-subreddit post search
  /r/<sub>/<sort>.json                 subreddit listing (hot/new/top/rising)
  /comments/<id>.json                  a post + its full comment tree
  /subreddits/search.json?q=…          subreddit search
  /r/<sub>/about.json                  subreddit metadata
  /api/me.json                         who am I (the honest auth signal)

Logged-out reads work; sign-in (`login` → `login_window`) is the one headed
moment, so reads reflect *your* Reddit (subscriptions, personalized ranking).
Mapping to `post` / `community` shapes happens in Python (`_map_post`,
`_map_community`, `_flatten` / `_nested` for comment trees).

Reference: `exa.py` (same-origin `fetch()` in the bg profile) for the transport;
`outlook.py` (★) for the `login_window` sign-in flip.
"""

import json
from datetime import datetime, timezone

from agentos import account, app_error, browser_session, connection, provides, returns, test, timeout

# The signed-in landing page; `_TARGET` is the tab-match hostname the engine
# opens (https://<target>/) when no reddit.com tab is live in the bg profile.
_HOME = "https://www.reddit.com/"
_TARGET = "www.reddit.com"

_REDDIT = {"shape": "product", "url": "https://reddit.com", "name": "Reddit"}


def _ts(epoch: int | float | None) -> str | None:
    """Unix seconds → ISO 8601, or None."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()


def _map_post(d: dict) -> dict:
    """A Reddit post/comment `data` dict → shape-native `post` fields."""
    author = d.get("author", "")
    subreddit = d.get("subreddit", "")
    return {
        "id": d.get("id"),
        "at": _REDDIT,
        "name": d.get("title"),
        "content": d.get("selftext") or d.get("body"),
        "url": f"https://reddit.com{d['permalink']}" if d.get("permalink") else None,
        "author": author,
        "published": _ts(d.get("created_utc")),
        "score": d.get("score"),
        "commentCount": d.get("num_comments"),
        "posted_by": {
            "shape": "account",
            "id": author,
            "name": author,
            "url": f"https://reddit.com/u/{author}",
        } if author else None,
        "published_in": {
            "shape": "community",
            "id": subreddit,
            "name": subreddit,
            "url": f"https://reddit.com/r/{subreddit}",
        } if subreddit else None,
    }


def _map_community(d: dict) -> dict:
    """A subreddit `data` dict → shape-native `community` fields."""
    display = d.get("display_name", "")
    return {
        "id": d.get("name"),
        "at": _REDDIT,
        "name": display,
        "content": d.get("public_description"),
        "url": f"https://reddit.com/r/{display}" if display else None,
        "image": d.get("community_icon") or d.get("icon_img"),
        "subscriberCount": d.get("subscribers"),
        "privacy": "OPEN",
    }


# ──────────────────────────────────────────────────────────────────────
# Transport — same-origin fetch inside the reddit.com tab
# ──────────────────────────────────────────────────────────────────────

# A freshly opened tab is at about:blank when our JS first runs — a relative
# fetch has no origin, and a cross-origin one wouldn't carry the reddit cookie.
# So we wait until the document settles on a reddit.com origin (the tab may sit
# briefly on the `js_challenge` interstitial — same origin, so the fetch still
# works). Exa's `_PRELUDE` analogue.
_PRELUDE = """
const __deadline = Date.now() + 20000;
while (true) {
  const onReddit = location.hostname.indexOf('reddit.com') !== -1;
  const ready = document.readyState === 'complete';
  const challenged = /(?:^|[?&])js_challenge=/.test(location.search);
  if (onReddit && ready && !challenged) break;
  if (Date.now() > __deadline) {
    return {
      __error: challenged ? 'js_challenge' : 'tab_not_ready',
      href: location.href,
    };
  }
  await new Promise(r => setTimeout(r, 250));
}
"""


async def _eval(body: str, *, timeout_s: int = 45):
    """Run an op body inside the reddit.com tab of the engine's HEADLESS
    background profile (`browser_session` pins it — no window opens)."""
    return await browser_session.eval(
        _TARGET, "(async () => {" + _PRELUDE + body + "})()", timeout=timeout_s
    )


def _auth_result(data: dict) -> bool:
    """True when `_get_json` returned a NeedsAuth `__result__` payload."""
    return isinstance(data, dict) and "__result__" in data


async def _get_json(path: str, params: dict | None = None):
    """Same-origin `fetch()` of a Reddit `.json` path; returns the parsed value
    (a dict for listings, an array for `/comments/<id>.json`). On bot-detection
    returns a ``NeedsAuth`` ``__result__`` (call ``reddit.login`` — never
    attach / headed relaunch). Raises on a non-JSON body."""
    body = (
        f"const __path = {json.dumps(path)};"
        f"const __p = {json.dumps({k: v for k, v in (params or {}).items() if v is not None})};"
        "const qs = new URLSearchParams(__p).toString();"
        "const url = qs ? __path + '?' + qs : __path;"
        "const r = await fetch(url, { cache: 'no-store' });"
        "const t = await r.text();"
        "if (r.status === 403) return { __error: 'blocked_403' };"
        "try { return JSON.parse(t); } catch (e) { return { __error: 'non_json', status: r.status, preview: t.slice(0, 200) }; }"
    )
    data = await _eval(body)
    if isinstance(data, dict) and data.get("__error"):
        code = data["__error"]
        if code == "tab_not_ready":
            raise RuntimeError("The reddit.com tab never became ready (page still loading?).")
        if code == "js_challenge":
            return browser_session.needs_auth(
                "Reddit blocked the headless background profile with a JS "
                "challenge (bot-detection).",
                login_op="reddit.login",
            )
        if code == "blocked_403":
            return browser_session.needs_auth(
                f"Reddit 403'd {path} (bot-detection).",
                login_op="reddit.login",
            )
        raise RuntimeError(
            f"Reddit returned non-JSON ({data.get('status')}) for {path}: {data.get('preview')}"
        )
    return data


# ──────────────────────────────────────────────────────────────────────
# The account trio — check / login / logout
# ──────────────────────────────────────────────────────────────────────
#
# The session is the browser profile, not a credential → all `@connection("none")`.
# Reddit's own sign-in has bot-detection on the POST, so `login` hands off to the
# headed `login_window` flip (the ★ outlook.py pattern) rather than driving the
# form headless. `check_session` reads the honest live signal — `/api/me.json`
# through Reddit's authed pipeline; logged out it returns a bare `loid` with no
# `data.name`, so a present username means the session will work.


@account.check
@returns("account")
@connection("none")
@timeout(45)
async def check_session(**params) -> dict:
    """Verify the Reddit session and identify the account.

    Fetches `/api/me.json` live (no-store) inside the reddit.com tab — a real
    call through Reddit's authed pipeline, not a page-store guess. A logged-out
    profile returns a bare logged-out id (`loid`) with no `data.name`, so the
    presence of a username is the honest "writes will work" signal.
    """
    body = (
        "const r = await fetch('/api/me.json', { cache: 'no-store' });"
        "if (!r.ok) return { __error: 'http_' + r.status };"
        "const j = await r.json().catch(() => null);"
        "const d = j && j.data;"
        "if (!d || !d.name) return { authenticated: false };"
        "return { authenticated: true, name: d.name, uid: d.id || null,"
        "         karma: (d.total_karma != null ? d.total_karma : null) };"
    )
    value = await _eval(body, timeout_s=30)
    if not isinstance(value, dict) or not value.get("authenticated"):
        return {"authenticated": False}
    name = value["name"]
    return {
        "authenticated": True,
        "at": _REDDIT,
        "platform": "reddit",
        "identifier": name,
        "handle": name,
        "displayName": name,
        "accountType": "user",
        "userId": value.get("uid"),
    }


@account.login
@returns("account | auth_challenge")
@connection("none")
@timeout(60)
async def login(**params) -> dict:
    """Sign in to Reddit — or report the already-live session.

    Returns the `account` when the background profile already holds a live
    Reddit session. Otherwise Reddit's sign-in has bot-detection that can't be
    driven headless, so this opens a headed sign-in window on the engine's
    background profile (a chromeless `--app` flip) and returns a `login_window`
    `auth_challenge`: the human signs in once, the agent polls `check_session`,
    and the session persists in the profile every headless read uses.
    """
    session = await check_session(**params)
    if isinstance(session, dict) and session.get("authenticated"):
        return session
    return await browser_session.login_window(
        "https://www.reddit.com/login/", label="Reddit"
    )


@account.logout
@returns({"status": "string", "hint": "string"})
@connection("none")
@timeout(45)
async def logout(**params) -> dict:
    """Sign out of Reddit — navigate the tab to Reddit's logout endpoint, which
    clears the session from the engine's background profile."""
    await browser_session.navigate(_TARGET, "https://www.reddit.com/logout/")
    return {
        "status": "logged_out",
        "hint": "Navigated to Reddit's sign-out; the session is cleared from "
                "the engine's background profile. Re-run login to sign back in.",
    }


# ──────────────────────────────────────────────────────────────────────
# Reads
# ──────────────────────────────────────────────────────────────────────


@test(params={"query": "agentOS", "limit": 3})
@returns("post[]")
async def search_posts(
    query: str, subreddit: str = None, limit: int = 25,
    sort: str = "relevance", time: str = "all", **params,
) -> list[dict]:
    """Search posts across Reddit (or within one subreddit).

    Args:
        query: Search query.
        subreddit: Restrict to r/<subreddit> (omit for a sitewide search).
        limit: Number of results (max 100).
        sort: relevance · hot · top · new · comments.
        time: Time window for `top`/`relevance` — all · year · month · week · day · hour.
    """
    qp = {"q": query, "sort": sort, "t": time, "limit": limit}
    if subreddit:
        qp["restrict_sr"] = 1
        data = await _get_json(f"/r/{subreddit}/search.json", qp)
    else:
        data = await _get_json("/search.json", qp)
    if _auth_result(data):
        return data
    return [_map_post(c["data"]) for c in data.get("data", {}).get("children", [])]


@test(params={"subreddit": "programming", "limit": 3})
@returns("post[]")
async def list_posts(subreddit: str, sort: str = "hot", limit: int = 25, **params) -> list[dict]:
    """List posts from a subreddit.

    Args:
        subreddit: Subreddit name (without r/).
        sort: hot · new · top · rising.
        limit: Number of posts (max 100).
    """
    data = await _get_json(f"/r/{subreddit}/{sort}.json", {"limit": limit})
    if _auth_result(data):
        return data
    return [_map_post(c["data"]) for c in data.get("data", {}).get("children", [])]


def _extract_post_id(id: str = None, url: str = None) -> str | None:
    """Resolve a post id from an id or a full/permalink Reddit URL."""
    if id:
        return id
    if url:
        import re
        m = re.search(r"comments/([a-z0-9]+)", url)
        if m:
            return m.group(1)
    return None


@test.skip(reason="needs a live post id")
@returns("post")
@provides("web_fetch", urls=["reddit.com/*/comments/*", "reddit.com/r/*/comments/*"])
async def get_post(id: str = None, url: str = None, comment_limit: int = 200, **params) -> dict:
    """Get a Reddit post with its comment tree (nested). Pass an id or a URL.

    Args:
        id: Post id (e.g. abc123) — optional if `url` is set.
        url: Full Reddit post URL (web_fetch) — optional if `id` is set.
        comment_limit: Max comments to fetch (Reddit's own paging limit).
    """
    pid = _extract_post_id(id, url)
    if not pid:
        return app_error("Either id or url is required.", code="BadParams")

    data = await _get_json(f"/comments/{pid}.json", {"limit": comment_limit, "depth": 20, "sort": "top"})
    if _auth_result(data):
        return data
    post = _map_post(data[0]["data"]["children"][0]["data"])

    def nested(c: dict) -> dict:
        d = c["data"]
        author = d.get("author", "")
        replies_raw = d.get("replies")
        children = []
        if isinstance(replies_raw, dict):
            children = [
                nested(rc)
                for rc in replies_raw.get("data", {}).get("children", [])
                if rc.get("kind") == "t1"
            ]
        return {
            "shape": "post",
            "id": d.get("id"),
            "at": _REDDIT,
            "content": d.get("body"),
            "author": author,
            "published": _ts(d.get("created_utc")),
            "score": d.get("ups"),
            "posted_by": {
                "shape": "account", "id": author, "name": author,
                "url": f"https://reddit.com/u/{author}",
            } if author else None,
            "replies": children,
        }

    post["replies"] = [
        nested(c) for c in data[1]["data"]["children"] if c.get("kind") == "t1"
    ]
    return post


@test.skip(reason="needs a live post id")
@returns("post[]")
async def comments_post(id: str, comment_limit: int = 200, **params) -> list[dict]:
    """Flatten a post's comment tree into a `post[]` with `replies_to` edges —
    the graph-native form (each comment lands as its own node, linked to its
    parent), vs `get_post`'s nested single node.

    Args:
        id: Post id.
        comment_limit: Max comments to fetch.
    """
    data = await _get_json(f"/comments/{id}.json", {"limit": comment_limit, "depth": 20, "sort": "top"})
    if _auth_result(data):
        return data
    post_data = data[0]["data"]["children"][0]["data"]
    result = [_map_post(post_data)]

    def flatten(c: dict, parent_id: str):
        d = c["data"]
        author = d.get("author", "")
        result.append({
            "id": d.get("id"),
            "at": _REDDIT,
            "content": d.get("body"),
            "url": f"https://reddit.com{d['permalink']}" if d.get("permalink") else None,
            "author": author,
            "published": _ts(d.get("created_utc")),
            "score": d.get("ups"),
            "posted_by": {
                "shape": "account", "id": author, "name": author,
                "url": f"https://reddit.com/u/{author}",
            } if author else None,
            "replies_to": {"shape": "post", "id": parent_id},
        })
        replies_raw = d.get("replies")
        if isinstance(replies_raw, dict):
            for rc in replies_raw.get("data", {}).get("children", []):
                if rc.get("kind") == "t1":
                    flatten(rc, d.get("id"))

    for c in data[1]["data"]["children"]:
        if c.get("kind") == "t1":
            flatten(c, post_data.get("id"))
    return result


@test(params={"query": "programming", "limit": 3})
@returns("community[]")
async def search_communities(query: str, limit: int = 25, **params) -> list[dict]:
    """Search for subreddits.

    Args:
        query: Search query.
        limit: Number of results (max 100).
    """
    data = await _get_json("/subreddits/search.json", {"q": query, "limit": limit})
    if _auth_result(data):
        return data
    return [_map_community(c["data"]) for c in data.get("data", {}).get("children", [])]


@test.skip(reason="needs a live subreddit")
@returns("community")
async def get_community(subreddit: str, **params) -> dict:
    """Get subreddit metadata.

    Args:
        subreddit: Subreddit name (without r/).
    """
    data = await _get_json(f"/r/{subreddit}/about.json")
    if _auth_result(data):
        return data
    return _map_community(data.get("data", {}))
