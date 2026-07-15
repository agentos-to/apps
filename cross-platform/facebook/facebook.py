"""Facebook — Stories (24h rings) via the page Relay store on facebook.com.

DMs live in `messenger` (MSYS worker). Stories are a different surface: the
home-tray GraphQL lands in the **page** RelayModernEnvironment. We read
normalized records only — same philosophy as WhatsApp Status / IG Relay
reads. Durable map: `operations.md`.
"""

from __future__ import annotations

import base64
import json

from agentos import (
    account, app_error, blobs, browser_session, client,
    provides, returns, timeout,
)

# Pin the real host Facebook redirects to — engine also treats www↔apex as
# one surface, but opening www avoids a mint→redirect→orphan cycle.
_PAGE = "www.facebook.com"
_HOME_URL = "https://www.facebook.com/"
_LOGIN_URL = "https://www.facebook.com/login/"
_SESSION_COOKIE = "xs"
_VIEWER_COOKIE = "c_user"

# Stories payload surfaces in the Social app — headless bg profile (rule 19).
# login_window is the one headed flip of that same profile.

_FIND_ENV = r"""
const __findRelayEnv = () => {
  if (globalThis.__re && typeof __re.relayEnv === 'function') {
    try { const e = __re.relayEnv(); if (e) return e; } catch (e) {}
  }
  const cands = [document.documentElement, document.body,
    ...document.querySelectorAll('body > div')].filter(Boolean);
  for (const c of cands) {
    const key = Object.keys(c).find(k =>
      k.startsWith('__reactContainer$') || k.startsWith('__reactFiber$'));
    if (!key) continue;
    const q = [c[key]];
    const seen = new Set();
    while (q.length) {
      const f = q.shift();
      if (!f || seen.has(f)) continue;
      seen.add(f);
      if (seen.size > 8000) break;
      const env = (f.memoizedProps && f.memoizedProps.environment)
        || (f.pendingProps && f.pendingProps.environment);
      if (env && typeof env.getStore === 'function') return env;
      if (f.child) q.push(f.child);
      if (f.sibling) q.push(f.sibling);
    }
  }
  return null;
};
"""

_HELPERS = r"""
const str = (v) => (typeof v === 'string' ? v : (v == null ? '' : String(v)));
const getSrc = (env) => env.getStore().getSource();
const get = (src, id) => { try { return id ? src.get(id) : null; } catch (e) { return null; } };
const deref = (src, v) => {
  if (!v) return null;
  if (typeof v === 'string') return get(src, v);
  if (v.__ref) return get(src, v.__ref);
  return v;
};
const meCookie = () => str((document.cookie.match(/(?:^|; )c_user=([^;]+)/) || [])[1] || '');
const picUri = (src, user) => {
  if (!user) return null;
  for (const k of Object.keys(user)) {
    if (!/profile_picture|profilePic/i.test(k)) continue;
    const img = deref(src, user[k]);
    if (img && img.uri) return img.uri;
  }
  return null;
};
const normMedia = (t) => {
  const s = str(t).toLowerCase();
  if (s === 'photo' || s === 'image') return 'image';
  if (s === 'video') return 'video';
  if (s.includes('video')) return 'video';
  if (s.includes('photo') || s.includes('image') || s === 'story') return 'image';
  return s || 'image';
};
const mediaFromCard = (src, card, info) => {
  let mediaType = (info && info.story_card_type) || 'STORY';
  let mediaUrl = null;
  let thumbUrl = null;
  let playableUrl = null;
  let posterUrl = null;
  if (info) {
    for (const k of Object.keys(info)) {
      if (!/story_thumbnail|story_video_thumbnail/i.test(k)) continue;
      const thumb = deref(src, info[k]);
      if (thumb && thumb.uri) { thumbUrl = thumb.uri; break; }
    }
  }
  for (const aid of (card.attachments && card.attachments.__refs) || []) {
    const att = get(src, aid);
    const media = att && deref(src, att.media);
    if (!media) continue;
    mediaType = media.__typename || mediaType;
    for (const k of Object.keys(media)) {
      if (/^image(\(|$)/.test(k)) {
        const img = deref(src, media[k]);
        // Prefer larger posters over tiny tray thumbs when Video has image().
        if (img && img.uri) {
          if (!posterUrl || /s960|width:1080|height:1920/i.test(k)) posterUrl = img.uri;
          if (normMedia(mediaType) !== 'video') mediaUrl = img.uri;
        }
      }
      if (/playable_url|browser_native_hd_url|browser_native_sd_url/i.test(k)
          && typeof media[k] === 'string' && media[k]) {
        playableUrl = media[k];
        mediaType = 'Video';
      }
    }
  }
  const type = normMedia(mediaType);
  // Never promote a tray JPEG thumb to mediaUrl for videos — that paints a
  // black <video> in the Social viewer. Prefer playable → poster → (photos) thumb.
  if (type === 'video') {
    mediaUrl = playableUrl || posterUrl || null;
  } else {
    mediaUrl = playableUrl || mediaUrl || posterUrl || thumbUrl;
  }
  return {
    mediaType: type,
    mediaUrl,
    thumbUrl: thumbUrl || posterUrl || mediaUrl,
    playableUrl,
  };
};
const mapStory = (src, card, bucket, owner, ringTotal, ringUnread, me) => {
  if (!card || !card.id) return null;
  const info = deref(src, card.story_card_info)
    || get(src, 'client:' + card.id + ':story_card_info') || {};
  const seen = deref(src, card.story_card_seen_state)
    || get(src, 'client:' + card.id + ':story_card_seen_state');
  const { mediaType, mediaUrl, thumbUrl } = mediaFromCard(src, card, info);
  const creation = card.creation_time;
  const exp = card.expiration_time;
  const authorId = str((owner && owner.id) || (bucket && bucket.id) || '');
  const authorName = (owner && owner.name) || null;
  const isOutgoing = authorId === me;
  const viewed = !!(seen && seen.is_seen_by_viewer);
  const out = {
    id: card.id,
    postType: 'story',
    content: null,
    published: Number.isFinite(creation) ? new Date(creation * 1000).toISOString() : null,
    expiresAt: Number.isFinite(exp) ? new Date(exp * 1000).toISOString() : null,
    isOutgoing,
    mediaType,
    viewed,
    authorId,
    author: isOutgoing ? 'Me' : authorName,
    name: isOutgoing ? 'My story' : (authorName || 'Story'),
    ringTotal,
    ringUnread,
    accountEmail: me || null,
  };
  const face = picUri(src, owner);
  if (face) out.image = face;
  if (thumbUrl) out.thumb = thumbUrl;
  if (authorId) {
    out.from = { platform: 'facebook', id: authorId };
    if (authorName) out.from.display_name = authorName;
  }
  if (mediaUrl) out.mediaUrl = mediaUrl;
  if (bucket && bucket.id) out.bucketId = bucket.id;
  return out;
};
const listStoryPosts = (src, limit, includeViewed) => {
  const me = meCookie();
  const ids = src.getRecordIDs();
  // Prefer Facebook's rectangular tray connection — its edge order IS the
  // product ranking (self first, then friends). Union other bucket pages
  // after, without scrambling that order via published-desc.
  const connIds = ids.filter((id) =>
    /unified_stories_buckets/.test(id) && !/:edges:|:page_info$/.test(id));
  connIds.sort((a, b) => {
    const score = (id) => {
      if (/StoriesTrayRectangular/.test(id)) return 0;
      if (/unified_stories_buckets\(first:/.test(id)) return 1;
      if (/unified_stories_buckets\(after:/.test(id)) return 2;
      return 3;
    };
    return score(a) - score(b) || a.localeCompare(b);
  });
  const bucketOrder = [];
  const bucketById = new Map();
  for (const cid of connIds) {
    const conn = get(src, cid);
    for (const er of (conn && conn.edges && conn.edges.__refs) || []) {
      const edge = get(src, er);
      const bucket = deref(src, edge && edge.node);
      if (!bucket || !bucket.id || bucketById.has(bucket.id)) continue;
      bucketById.set(bucket.id, bucket);
      bucketOrder.push(bucket);
    }
  }
  const posts = [];
  for (const bucket of bucketOrder) {
    if (posts.length >= limit) break;
    const owner = deref(src, bucket.story_bucket_owner) || get(src, bucket.id);
    let storiesConn = get(src, 'client:' + bucket.id + ':unified_stories(include_fb_note:true)');
    if (!storiesConn) {
      const k = Object.keys(bucket).find(x => x.startsWith('unified_stories'));
      if (k) storiesConn = deref(src, bucket[k]);
    }
    const nodes = [];
    const first = deref(src, bucket.first_story_to_show);
    if (first) nodes.push(first);
    for (const nid of (storiesConn && storiesConn.nodes && storiesConn.nodes.__refs) || []) {
      const n = get(src, nid);
      if (n && !nodes.find(x => x.id === n.id)) nodes.push(n);
    }
    for (const er of (storiesConn && storiesConn.edges && storiesConn.edges.__refs) || []) {
      const n = deref(src, get(src, er) && get(src, er).node);
      if (n && !nodes.find(x => x.id === n.id)) nodes.push(n);
    }
    // Within a ring: oldest → newest (viewer plays forward).
    nodes.sort((a, b) => (Number(a.creation_time) || 0) - (Number(b.creation_time) || 0));
    const ringTotal = nodes.length;
    const ringUnread = nodes.filter(n => {
      const s = deref(src, n.story_card_seen_state)
        || get(src, 'client:' + n.id + ':story_card_seen_state');
      return s && s.is_seen_by_viewer === false;
    }).length;
    for (const card of nodes) {
      if (posts.length >= limit) break;
      const p = mapStory(src, card, bucket, owner, ringTotal, ringUnread, me);
      if (!p) continue;
      if (!includeViewed && p.viewed) continue;
      posts.push(p);
    }
  }
  return posts;
};
const findStory = (src, id) => {
  const direct = get(src, id);
  if (direct && direct.__typename === 'Story') return direct;
  // Scan buckets if the card isn't a top-level record id match
  for (const p of listStoryPosts(src, 500, true)) {
    if (p.id === id) return get(src, id) || direct;
  }
  return direct;
};
"""


async def _read_cookies():
    return await browser_session.read_cookies(_PAGE)


async def _ensure_home():
    """Land on facebook.com so the stories-tray Relay query can populate."""
    on_fb = await browser_session.eval(
        _PAGE,
        "location.hostname.indexOf('facebook.com') !== -1"
        " && location.pathname.indexOf('/messages') !== 0"
        " && document.readyState !== 'loading'",
        timeout=15,
    )
    if on_fb is not True:
        await browser_session.navigate(_PAGE, _HOME_URL, timeout=45)
        # Give Comet a beat to commit the tray query into Relay.
        await browser_session.eval(
            _PAGE,
            "(async () => { await new Promise(r => setTimeout(r, 2500)); return true; })()",
            timeout=20,
        )


async def _eval(js: str, *, timeout_s: int = 60):
    return await browser_session.eval(_PAGE, js, timeout=timeout_s)


# ──────────────────────────────────────────────────────────────────────
# Account trio
# ──────────────────────────────────────────────────────────────────────


@account.check
@returns("account")
@timeout(45)
async def check_session(**params):
    """Verify Facebook login — httpOnly `xs` means writes will work."""
    jar = await _read_cookies()
    if _SESSION_COOKIE not in jar:
        return {"authenticated": False}
    acct = {
        "authenticated": True,
        "platform": "facebook",
        "at": {
            "shape": "product",
            "name": "Facebook",
            "url": "https://www.facebook.com/",
        },
    }
    fbid = (jar.get(_VIEWER_COOKIE) or {}).get("value")
    if fbid:
        acct["identifier"] = fbid
    return acct


@account.login
@returns("account | auth_challenge")
@timeout(60)
async def login(**params):
    """Sign in to Facebook (headed bg-profile flip), or return the account."""
    existing = await check_session()
    if isinstance(existing, dict) and existing.get("authenticated"):
        return existing
    return await browser_session.login_window(
        _LOGIN_URL,
        label="Facebook",
        instructions=(
            "Sign in to Facebook in the window that opened on the engine's "
            "background profile. Complete password re-confirm / 2FA, then poll "
            "facebook.check_session. Call login_window close=true when done."
        ),
        retrieval={
            "via": "email",
            "look_for": "a Facebook login code or sign-in confirmation",
        },
    )


@account.logout
@returns({"ok": "boolean", "message": "string"})
@timeout(45)
async def logout(**params):
    """Log out of Facebook in the engine browser."""
    return {"ok": False, "message": "logout not yet implemented — see readme"}


# ──────────────────────────────────────────────────────────────────────
# Stories → feeds (Social app)
# ──────────────────────────────────────────────────────────────────────


@returns("post[]")
@provides("feeds", account_param="account")
@timeout(90)
async def list_posts(*, limit=200, include_viewed=True, **params):
    """List Facebook Stories as `post` rows (`postType: story`).

    Brokered as `feeds` for the Social app. Reads the page Relay store's
    `unified_stories_buckets` connections — see operations.md.
    """
    jar = await _read_cookies()
    if _SESSION_COOKIE not in jar:
        return browser_session.needs_auth(
            "Facebook session missing (no xs cookie).",
            login_op="facebook.login",
        )
    await _ensure_home()
    lim = int(limit)
    include = bool(include_viewed)
    rows = await _eval(f"""
    (async () => {{
      {_FIND_ENV}
      {_HELPERS}
      const env = __findRelayEnv();
      if (!env) return {{ __error: 'no_relay', what: 'RelayModernEnvironment not found' }};
      const src = getSrc(env);
      const posts = listStoryPosts(src, {lim}, {json.dumps(include)});
      for (const p of posts) {{
        delete p.mediaUrl;
        delete p.bucketId;
        delete p.bucketType;
      }}
      return posts;
    }})()
    """, timeout_s=75)
    if isinstance(rows, dict) and rows.get("__error"):
        return app_error(
            f"Facebook stories unavailable: {rows.get('what') or rows['__error']}",
            code="NotReady",
        )
    return rows if isinstance(rows, list) else []


@returns("post")
@provides("get_post", account_param="account")
@timeout(120)
async def get_post(*, id, **params):
    """Get one Story by card id and hydrate media into the blob store."""
    jar = await _read_cookies()
    if _SESSION_COOKIE not in jar:
        return browser_session.needs_auth(
            "Facebook session missing (no xs cookie).",
            login_op="facebook.login",
        )
    await _ensure_home()
    sid = str(id)
    entity = await _eval(f"""
    (async () => {{
      {_FIND_ENV}
      {_HELPERS}
      const env = __findRelayEnv();
      if (!env) return {{ __error: 'no_relay' }};
      const src = getSrc(env);
      const me = meCookie();
      const listed = listStoryPosts(src, 500, true);
      const meta = listed.find(p => p.id === {json.dumps(sid)});
      const card = get(src, {json.dumps(sid)});
      if (!meta || !card) {{
        return {{ __error: 'not_found', what: 'post', ref: {json.dumps(sid)} }};
      }}
      const owner = get(src, meta.authorId);
      const bucket = get(src, meta.bucketId);
      const out = mapStory(src, card, bucket, owner, meta.ringTotal, meta.ringUnread, me)
        || Object.assign({{}}, meta);
      if (!out.mediaUrl && meta.mediaUrl) out.mediaUrl = meta.mediaUrl;
      if (!out.mediaUrl) {{
        // Direct attachment walk — belt and suspenders
        const info = deref(src, card.story_card_info)
          || get(src, 'client:' + card.id + ':story_card_info') || {{}};
        const m = mediaFromCard(src, card, info);
        if (m.mediaUrl) out.mediaUrl = m.mediaUrl;
        if (m.mediaType) out.mediaType = m.mediaType;
      }}
      return out;
    }})()
    """, timeout_s=100)

    if isinstance(entity, dict) and entity.get("__error") == "not_found":
        return app_error(f"No Facebook story {sid!r}.", code="NotFound")
    if isinstance(entity, dict) and entity.get("__error"):
        return app_error(
            f"Facebook get_post failed: {entity.get('what') or entity['__error']}",
            code="PayloadError",
        )
    if not isinstance(entity, dict):
        return entity

    media_url = entity.pop("mediaUrl", None)
    bucket_id = entity.pop("bucketId", None)
    entity.pop("bucketType", None)

    # Tray Relay often omits Video.playable_url until the story viewer query
    # lands — Paisley-class cards only had a 180×320 JPEG thumb, which then
    # rendered as a black <video>. Open the story URL to hydrate, then re-read.
    needs_video_hydrate = (
        entity.get("mediaType") == "video"
        and (not media_url or _looks_like_image_url(media_url))
        and bucket_id
    )
    if needs_video_hydrate:
        story_url = (
            f"https://www.facebook.com/stories/{bucket_id}/{sid}/?source=story_tray"
        )
        try:
            await browser_session.navigate(_PAGE, story_url, timeout=45)
            await _eval(
                "(async () => { await new Promise(r => setTimeout(r, 2000)); return true; })()",
                timeout_s=20,
            )
            hydrated = await _eval(f"""
            (async () => {{
              {_FIND_ENV}
              {_HELPERS}
              const env = __findRelayEnv();
              if (!env) return {{}};
              const src = getSrc(env);
              const card = get(src, {json.dumps(sid)});
              if (!card) return {{}};
              const info = deref(src, card.story_card_info)
                || get(src, 'client:' + card.id + ':story_card_info') || {{}};
              return mediaFromCard(src, card, info);
            }})()
            """, timeout_s=60)
            if isinstance(hydrated, dict) and hydrated.get("mediaUrl"):
                media_url = hydrated["mediaUrl"]
                if hydrated.get("mediaType"):
                    entity["mediaType"] = hydrated["mediaType"]
                if hydrated.get("thumbUrl") and not entity.get("thumb"):
                    entity["thumb"] = hydrated["thumbUrl"]
        except Exception:
            pass

    if media_url:
        got = await _fetch_media(media_url)
        if got and got.get("data"):
            mime = (got.get("mime") or "application/octet-stream").split(";")[0]
            # Trust bytes over tray metadata — a JPEG poster must not be shaped video.
            if mime.startswith("video/"):
                entity["mediaType"] = "video"
                shape = "video"
                ext = "mp4" if "mp4" in mime or mime == "video/mp4" else "mp4"
            elif mime.startswith("image/"):
                entity["mediaType"] = "image"
                shape = "image"
                ext = "png" if "png" in mime else ("webp" if "webp" in mime else "jpg")
            else:
                # Guess from URL / declared type
                if entity.get("mediaType") == "video" and not _looks_like_image_url(media_url):
                    mime = "video/mp4"
                    shape = "video"
                    ext = "mp4"
                else:
                    mime = mime if mime != "application/octet-stream" else "image/jpeg"
                    shape = "image"
                    entity["mediaType"] = "image"
                    ext = "jpg"
            blob = await blobs.put(got["data"], ext=ext)
            entity["attaches"] = [{
                "shape": shape,
                "name": entity.get("name") or "Story",
                "mimeType": mime,
                "size": got["size"],
                "path": blob["path"],
                "sha": blob["sha256"],
            }]
        else:
            entity["externalUrl"] = media_url
    return entity


def _looks_like_image_url(url: str) -> bool:
    u = (url or "").lower()
    if any(x in u for x in (".mp4", "video-dfw", "/v/t2/", "video/mp4")):
        return False
    return any(x in u for x in (".jpg", ".jpeg", ".png", ".webp", "scontent-", "/t51.", "/t39."))


async def _fetch_media(url: str):
    """CDN bytes via `browser_session.response_body` (CDP Network.getResponseBody).

    In-page `fetch(fbcdn)` is CORS-blocked; the site paints with `<img>`/`<video>`.
    The browser-plane verb intercepts that load (or `loadNetworkResource`).
    Engine `http.request` is a last-resort fallback only.
    """
    try:
        resp = await browser_session.response_body(_PAGE, url, timeout=60)
        if isinstance(resp, dict) and resp.get("data"):
            return {
                "data": resp["data"],
                "mime": (resp.get("mime") or "application/octet-stream").split(";")[0],
                "size": resp.get("size") or 0,
            }
    except Exception:
        pass
    return await _engine_fetch_media(url)


async def _engine_fetch_media(url: str):
    """Last-resort CDN fetch via engine http.request (needs plugin `http` grant)."""
    try:
        resp = await client.get(
            url,
            client="browser",
            headers={
                "Referer": "https://www.facebook.com/",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )
    except Exception:
        return None
    if not isinstance(resp, dict):
        return None
    if resp.get("status") not in (200, 206):
        return None
    hexbody = resp.get("body_bytes")
    if not hexbody:
        body = resp.get("body")
        if not body:
            return None
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return {
            "data": base64.b64encode(raw).decode("ascii"),
            "mime": (resp.get("headers") or {}).get("content-type", "").split(";")[0],
            "size": len(raw),
        }
    try:
        raw = bytes.fromhex(hexbody)
    except ValueError:
        return None
    headers = resp.get("headers") or {}
    ct = ""
    if isinstance(headers, dict):
        for k, v in headers.items():
            if str(k).lower() == "content-type":
                ct = str(v).split(";")[0]
                break
    return {
        "data": base64.b64encode(raw).decode("ascii"),
        "mime": ct,
        "size": len(raw),
    }
