"""LinkedIn — home feed (SDUI) + Messaging (Voyager Messaging GraphQL).

Feed: flagship SDUI MainFeed fiber + DOM cards. Messaging: same-origin
`voyagerMessagingGraphQL` (conversations / messages) plus a CDP
`browser_session.subscribe` fetch-wrap for live inbound. Durable map:
`operations.md`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time

from agentos import (
    account, app_error, blobs, browser_session, client,
    provides, returns, services, timeout, url,
)

_PAGE = "www.linkedin.com"
_FEED_URL = "https://www.linkedin.com/feed/"
_MESSAGING_URL = "https://www.linkedin.com/messaging/"
_LOGIN_URL = "https://www.linkedin.com/login"
_SESSION_COOKIE = "li_at"
_CSRF_COOKIE = "JSESSIONID"
# Proven queryIds from live /messaging/ (2026-07-18).
_Q_CONVERSATIONS = "messengerConversations.0d5e6781bbee71c3e51c8843c6519f48"
_Q_MESSAGES = "messengerMessages.5846eeb71c981f11e0134cb6626cc314"
_WATCH_MARKER = "__agentos_entity__"
_MERGE_URLS = [
    "https://www.linkedin.com/",
    "https://www.linkedin.com/feed/",
    "https://www.linkedin.com/messaging/",
    "https://www.linkedin.com/login",
]
# Headed login window: poll until session lands, then merge+close.
_LOGIN_WAIT_S = 240
_LOGIN_POLL_S = 2.5

# Feed payload surfaces in the Feeds app — headless bg profile (rule 19).
# login_window is the one headed moment on the dedicated login profile.

_LOGIN_DONE_JS = r"""
(() => {
  const href = location.href || '';
  const path = location.pathname || '';
  // Still on an auth / challenge surface → not done.
  if (/\/(login|uas\/login|checkpoint|challenge)/i.test(path + href)) {
    return { ok: false, why: 'auth_path', href };
  }
  // Signed-in chrome / feed / profile.
  if (document.querySelector(
    '[data-testid="mainFeed"], [data-sdui-screen*="MainFeed"], '
    + 'header.global-nav, nav.global-nav, [data-test-global-nav]'
  )) {
    return { ok: true, why: 'signed_in_chrome', href };
  }
  if (/\/(feed|mynetwork|messaging|notifications|in\/)/i.test(path)) {
    return { ok: true, why: 'app_path', href };
  }
  // liap is set on logged-in chrome (not httpOnly).
  if (/(?:^|; )liap=true(?:;|$)/.test(document.cookie || '')) {
    return { ok: true, why: 'liap', href };
  }
  return { ok: false, why: 'unknown', href, path };
})()
"""

_LIST_FEED_JS = r"""
(async () => {
  const limit = __LIMIT__;
  const pages = __PAGES__;

  function findMainFeed(max = 20000) {
    const root = document.getElementById('root') || document.body;
    const key = Object.keys(root).find(
      (k) => k.startsWith('__reactFiber') || k.startsWith('__reactContainer')
    );
    if (!key) return null;
    const q = [root[key]];
    const seen = new Set();
    let best = null;
    while (q.length && seen.size < max) {
      const f = q.shift();
      if (!f || seen.has(f)) continue;
      seen.add(f);
      const p = f.memoizedProps || f.pendingProps;
      if (p && p.collectionId === 'mainFeed' && Array.isArray(p.items)) {
        if (!best || ((p.items || []).length > (best.items || []).length)) best = p;
      }
      if (f.child) q.push(f.child);
      if (f.sibling) q.push(f.sibling);
    }
    return best;
  }

  const parseRelative = (s) => {
    const now = Date.now();
    const m = String(s || '').trim().match(/^(\d+)\s*([smhdw])\b/i);
    if (!m) return null;
    const n = Number(m[1]);
    const u = m[2].toLowerCase();
    const mult = { s: 1e3, m: 6e4, h: 36e5, d: 864e5, w: 6048e5 }[u];
    return new Date(now - n * mult).toISOString();
  };

  const urnFromHref = (href) => {
    if (!href) return null;
    const m = String(href).match(/urn:li:(share|activity|ugcPost):[0-9]+/);
    return m ? m[0] : null;
  };

  const isFaceUrl = (u) => /profile-displayphoto|company-logo/i.test(u || '');
  const isMediaUrl = (u) =>
    /feedshare|videocover|image-shrink/i.test(u || '')
    || (/dms\/image\/v2\//i.test(u || '') && !isFaceUrl(u));
  /** LinkedIn CDN size token — larger wins. Tokens are signed; do not rewrite. */
  const mediaScore = (u) => {
    const m = String(u || '').match(
      /(?:feedshare-shrink_|image-shrink_|videocover-shrink_|shrink_|scale_)(\d+)/i
    );
    return m ? Number(m[1]) : 0;
  };
  /** Every licdn URL on the card (img/srcset + HTML) — painted src is often a tiny shrink. */
  const collectLicdnUrls = (el) => {
    const urls = [];
    const seen = new Set();
    const add = (u) => {
      if (!u || !/media\.licdn\.com/i.test(u) || seen.has(u)) return;
      seen.add(u);
      urls.push(u);
    };
    for (const i of el.querySelectorAll('img')) {
      add(i.currentSrc || i.src);
      const ss = i.getAttribute('srcset') || '';
      for (const part of ss.split(',')) {
        const u = part.trim().split(/\s+/)[0];
        if (u) add(u);
      }
    }
    const html = el.innerHTML || '';
    for (const m of html.matchAll(/https:\/\/media\.licdn\.com\/[^"'\\\s>]+/g)) {
      add(m[0].replace(/&amp;/g, '&'));
    }
    return urls;
  };
  const pickFace = (el, urls) => {
    // Prefer the avatar inside the author profile/company link — never post media.
    for (const a of el.querySelectorAll('a[href*="/in/"] img, a[href*="/company/"] img')) {
      const u = a.currentSrc || a.src || '';
      if (isFaceUrl(u)) return u;
    }
    const faces = urls.filter(isFaceUrl).sort((a, b) => mediaScore(b) - mediaScore(a));
    return faces[0] || null;
  };
  const pickMedia = (urls) => {
    const media = urls.filter((u) => isMediaUrl(u) && !isFaceUrl(u))
      .sort((a, b) => mediaScore(b) - mediaScore(a));
    return media[0] || null;
  };

  const fiberKey = (el) => Object.keys(el || {}).find(
    (k) => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance')
  );

  /** Activity URN lives on the reaction-button SDUI props (share URN alone is wrong for social APIs). */
  const activityFromCard = (el) => {
    const btn = [...el.querySelectorAll('button,[role="button"]')].find((b) =>
      /Reaction button state/i.test(b.getAttribute('aria-label') || '')
    );
    if (!btn) return null;
    let f = btn[fiberKey(btn)];
    for (let i = 0; i < 50 && f; i++, f = f.return) {
      const p = f.memoizedProps || f.pendingProps || {};
      try {
        const seen = new WeakSet();
        const raw = JSON.stringify(p, (k, v) => {
          if (typeof v === 'function') return undefined;
          if (v && typeof v === 'object') {
            if (seen.has(v)) return undefined;
            seen.add(v);
          }
          return v;
        });
        const m = raw && raw.match(/"activityId"\s*:\s*"(\d{6,})"/);
        if (m) return 'urn:li:activity:' + m[1];
      } catch (e) {}
    }
    const htmlAct = (el.innerHTML || '').match(/urn:li:activity:\d+/);
    return htmlAct ? htmlAct[0] : null;
  };

  const viewerReactionFromCard = (el) => {
    const btn = [...el.querySelectorAll('button,[role="button"]')].find((b) =>
      /Reaction button state/i.test(b.getAttribute('aria-label') || '')
    );
    const aria = (btn && btn.getAttribute('aria-label')) || '';
    const m = aria.match(/Reaction button state:\s*(.+)$/i);
    if (!m) return null;
    const v = m[1].trim();
    if (!v || /^no reaction$/i.test(v)) return null;
    return v.toUpperCase().replace(/\s+/g, '_');
  };

  /** Trailing social counts: reactions [, comments [, reposts]]. */
  const socialCountsFromCard = (el) => {
    const lines = String(el.innerText || '').split('\n').map((s) => s.trim()).filter(Boolean);
    let commentCount = null;
    for (const l of lines) {
      const see = l.match(/see\s+(\d[\d,]*)\s+more\s+comments?/i);
      if (see) commentCount = Number(see[1].replace(/,/g, '')) + 1;
      const cr = l.match(/^(\d[\d,]*)\s+comments?$/i);
      if (cr) commentCount = Number(cr[1].replace(/,/g, ''));
    }
    const nums = [];
    for (let i = lines.length - 1; i >= 0; i--) {
      const l = lines[i];
      if (/^[\u200b\s]*$/.test(l)) continue;
      if (/^\d[\d,]*$/.test(l)) {
        nums.unshift(Number(l.replace(/,/g, '')));
        if (nums.length >= 3) break;
        continue;
      }
      break;
    }
    let score = null;
    let shareCount = null;
    if (nums.length === 1) score = nums[0];
    else if (nums.length === 2) {
      score = nums[0];
      if (commentCount == null) commentCount = nums[1];
    } else if (nums.length >= 3) {
      score = nums[0];
      if (commentCount == null) commentCount = nums[1];
      shareCount = nums[2];
    }
    return { score, commentCount, shareCount };
  };

  /** Author identity from profile/company links + optional member URN. */
  const authorIdentityFromCard = (el, authorName, isCompany) => {
    const hrefs = [...el.querySelectorAll('a[href]')].map((a) => a.getAttribute('href') || '');
    let handle = null;
    let companySlug = null;
    for (const h of hrefs) {
      const pm = h.match(/\/in\/([^/?#]+)\/?/i);
      if (pm && !handle) handle = decodeURIComponent(pm[1]);
      const cm = h.match(/\/company\/([^/?#]+)\/?/i);
      if (cm && !companySlug) companySlug = decodeURIComponent(cm[1]);
    }
    const html = el.innerHTML || '';
    const memberM = html.match(/urn:li:member:(\d+)/);
    const memberId = memberM ? memberM[1] : null;
    if (isCompany && companySlug) {
      return {
        kind: 'organization',
        authorId: companySlug,
        handle: companySlug,
        url: 'https://www.linkedin.com/company/' + companySlug + '/',
        memberId: null,
      };
    }
    const authorId = memberId || handle || null;
    return {
      kind: 'person',
      authorId,
      handle: handle || null,
      url: handle ? ('https://www.linkedin.com/in/' + handle + '/') : null,
      memberId,
    };
  };

  const splitPersonName = (full) => {
    const parts = String(full || '').trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return {};
    if (parts.length === 1) return { givenName: parts[0] };
    return {
      givenName: parts[0],
      familyName: parts[parts.length - 1],
      additionalName: parts.length > 2 ? parts.slice(1, -1).join(' ') : undefined,
    };
  };

  const parseCard = (el) => {
    const text = String(el.innerText || '').replace(/\s+/g, ' ').trim();
    if (!text.startsWith('Feed post')) return null;
    if (/\bPromoted\b/.test(text)) return null;

    const hrefs = [...el.querySelectorAll('a[href]')].map((a) => a.getAttribute('href') || '');
    let id = null;
    for (const h of hrefs) {
      id = urnFromHref(h);
      if (id) break;
    }
    if (!id) {
      const htmlUrn = (el.innerHTML || '').match(/urn:li:(share|activity|ugcPost):\d+/);
      if (htmlUrn) id = htmlUrn[0];
    }

    let t = text.replace(/^Feed post\s+/, '').trim();
    t = t.replace(/\s*…\s*more.*$/u, '').replace(/\s+\d+(?:\s+\d+){0,3}\s*[\u200b\s]*$/u, '').trim();

    let author = null;
    let degree = null;
    let jobTitle = null;
    let published = null;
    let content = t;
    let isCompany = false;
    const dm = t.match(/^(.+?)\s+[•·]\s+(1st|2nd|3rd)\b\s*(.*)$/s);
    if (dm) {
      author = dm[1].trim();
      degree = dm[2];
      const rest = dm[3].trim();
      const last = rest.match(/^(.*?)(\d+\s*[smhdw])\s*[•·]\s*(Edited\s*[•·]\s*)?(.*)$/s);
      if (last) {
        jobTitle = (last[1] || '').trim() || null;
        published = parseRelative(last[2]);
        content = (last[4] || '').trim();
      } else content = rest;
    } else {
      const cm = t.match(/^(.+?)\s+(\d[\d,]*\s+followers)\b\s*(.*)$/s);
      if (cm) {
        author = cm[1].trim();
        content = cm[3].trim();
        isCompany = true;
      }
    }

    // Face = author avatar only; media = largest post attachment URL on the card.
    const urls = collectLicdnUrls(el);
    const face = pickFace(el, urls);
    const media = pickMedia(urls);
    const social = socialCountsFromCard(el);
    const activityUrn = activityFromCard(el);
    const viewerReaction = viewerReactionFromCard(el);
    const ident = authorIdentityFromCard(el, author, isCompany);
    return {
      id,
      activityUrn,
      author,
      authorId: ident.authorId,
      handle: ident.handle,
      authorUrl: ident.url,
      authorKind: ident.kind,
      memberId: ident.memberId,
      degree,
      jobTitle,
      published,
      content: content.slice(0, 4000),
      // FB/WA convention: `image` = author face; never post media.
      image: face,
      thumb: media,
      mediaUrl: media,
      postType: media ? (/videocover/i.test(media) ? 'video' : 'image') : 'text',
      score: social.score,
      commentCount: social.commentCount,
      shareCount: social.shareCount,
      viewerReaction,
    };
  };

  const collect = () => {
    const col = findMainFeed();
    const cards = [...document.querySelectorAll('[data-testid="mainFeed"] [data-lazy-mount-id]')]
      .map(parseCard)
      .filter(Boolean);
    const fiberIds = [];
    if (col) {
      for (const it of col.items || []) {
        const id = it && it.dedupeId != null ? String(it.dedupeId) : '';
        if (/^urn:li:(share|activity|ugcPost):/.test(id)) {
          fiberIds.push({ id, key: it.key || it.semanticId || null });
        }
      }
    }
    let fi = 0;
    for (const c of cards) {
      if (!c.id && fiberIds[fi]) {
        c.id = fiberIds[fi].id;
        c.key = fiberIds[fi].key;
        fi++;
      } else if (c.id) {
        const idx = fiberIds.findIndex((f) => f.id === c.id);
        if (idx >= 0) fi = idx + 1;
      }
    }
    const posts = cards.filter((c) => c.author && c.content).map((c) => {
      const id = c.id || c.activityUrn || ('li-dom:' + c.author + ':' + (c.published || ''));
      const authorId = c.authorId || null;
      const out = {
        id,
        postType: c.postType,
        content: c.content,
        published: c.published,
        author: c.author,
        authorId,
        // Header avatar (FB/WA): face on `image`. Attachment on mediaUrl/thumb.
        image: c.image,
        thumb: c.thumb,
        mediaUrl: c.mediaUrl,
        mediaType: c.postType === 'video' ? 'video' : (c.mediaUrl ? 'image' : null),
        externalUrl: (c.id || c.activityUrn)
          ? 'https://www.linkedin.com/feed/update/'
            + encodeURIComponent(c.id || c.activityUrn) + '/'
          : null,
        degree: c.degree || null,
      };
      if (c.jobTitle) out.title = c.jobTitle;
      if (c.activityUrn) {
        out.activityUrn = c.activityUrn;
        out.feedbackId = c.activityUrn; // Feeds react/comments key (FB-shaped)
      }
      if (c.score != null) out.score = c.score;
      if (c.commentCount != null) out.commentCount = c.commentCount;
      if (c.shareCount != null) out.shareCount = c.shareCount;
      if (c.viewerReaction) out.viewerReaction = c.viewerReaction;
      if (c.score != null) out.reactions = [{ type: 'LIKE', count: c.score }];

      // Attribution: JSON `from` (FB parity) + shaped `posted_by` for graph people.
      if (authorId) {
        out.from = {
          platform: 'linkedin',
          id: authorId,
          display_name: c.author,
        };
        if (c.handle) out.from.handle = c.handle;
      }
      if (c.authorKind === 'organization' && c.author) {
        out.posted_by = {
          shape: 'organization',
          name: c.author,
          image: c.image || null,
          url: c.authorUrl || null,
        };
      } else if (c.author && authorId) {
        const names = splitPersonName(c.author);
        const ident = {
          platform: 'linkedin',
          id: c.memberId || authorId,
        };
        if (c.handle) ident.handle = c.handle;
        out.posted_by = {
          shape: 'person',
          name: c.author,
          givenName: names.givenName || null,
          familyName: names.familyName || null,
          additionalName: names.additionalName || null,
          jobTitle: c.jobTitle || null,
          image: c.image || null,
          url: c.authorUrl || null,
          about: c.jobTitle || null,
          identities: [ident],
        };
      }
      return out;
    });
    const next = col && col.nextPageRequest && col.nextPageRequest.requestedArguments
      && col.nextPageRequest.requestedArguments.payload;
    const canMore = !!(col && col.paginationResult
      && typeof col.paginationResult.fetchMoreItems === 'function');
    return { posts, next, canMore, col };
  };

  // Warm pagination until we have enough cards or pages exhausted.
  for (let i = 0; i < pages; i++) {
    const cur = collect();
    if (cur.posts.length >= limit || !cur.canMore) break;
    try {
      cur.col.paginationResult.fetchMoreItems();
      await new Promise((r) => setTimeout(r, 2200));
    } catch (e) {
      break;
    }
  }

  const out = collect();
  return {
    posts: out.posts.slice(0, limit),
    pageInfo: {
      endCursor: out.next ? JSON.stringify(out.next) : null,
      hasNext: !!out.canMore,
    },
  };
})()
"""

_ME_JS = r"""
(async () => {
  const csrf = (document.cookie.match(/JSESSIONID="?([^";]+)/) || [])[1] || '';
  if (!csrf) return { __error: 'no_csrf' };
  const res = await fetch('/voyager/api/me', {
    headers: {
      'csrf-token': csrf,
      accept: 'application/vnd.linkedin.normalized+json+2.1',
      'x-restli-protocol-version': '2.0.0',
    },
    credentials: 'include',
  });
  if (!res.ok) return { __error: 'me_http', what: String(res.status) };
  const j = await res.json();
  const mini = (j.included || []).find((x) => x && /MiniProfile/i.test(x.$type || '')) || {};
  const profileUrn = (j.data && j.data['*miniProfile']) || mini.entityUrn || null;
  // Messaging mailbox = fsd_profile URN (not fs_miniProfile).
  let mailboxUrn = null;
  if (profileUrn) {
    const m = String(profileUrn).match(/urn:li:(?:fs_miniProfile|fsd_profile):([A-Za-z0-9_-]+)/);
    if (m) mailboxUrn = 'urn:li:fsd_profile:' + m[1];
  }
  return {
    plainId: String((j.data && j.data.plainId) || ''),
    profileUrn,
    mailboxUrn,
    name: [mini.firstName, mini.lastName].filter(Boolean).join(' '),
    handle: mini.publicIdentifier || null,
    occupation: mini.occupation || null,
  };
})()
"""


# ── Messaging GraphQL helpers (in-page) ────────────────────────────────
_MSG_HELPERS_JS = r"""
const __LI_Q_CONVERSATIONS = __Q_CONVERSATIONS__;
const __LI_Q_MESSAGES = __Q_MESSAGES__;

const liCsrf = () => (document.cookie.match(/JSESSIONID="?([^";]+)/) || [])[1] || '';
const liHeaders = () => ({
  'csrf-token': liCsrf(),
  accept: 'application/vnd.linkedin.normalized+json+2.1',
  'x-restli-protocol-version': '2.0.0',
});
/** LinkedIn GraphQL urn encoding — encodeURIComponent leaves () unescaped (400). */
const encLiUrn = (u) => String(u || '')
  .replace(/:/g, '%3A').replace(/\(/g, '%28').replace(/\)/g, '%29').replace(/,/g, '%2C');
const attributedText = (v) => {
  if (!v) return '';
  if (typeof v === 'string') return v;
  if (typeof v.text === 'string') return v.text;
  return '';
};
const vectorFace = (pic) => {
  if (!pic || !pic.rootUrl) return null;
  const arts = pic.artifacts || [];
  // Prefer ~200px for conversation rows.
  let best = arts[0];
  for (const a of arts) {
    if ((a.width || 0) >= 150 && (a.width || 0) <= 220) { best = a; break; }
    if ((a.width || 0) > (best && best.width || 0)) best = a;
  }
  if (!best || !best.fileIdentifyingUrlPathSegment) return pic.rootUrl || null;
  return pic.rootUrl + best.fileIdentifyingUrlPathSegment;
};
const threadIdFromConv = (c) => {
  if (!c) return null;
  if (c.conversationUrl) {
    const m = String(c.conversationUrl).match(/\/messaging\/thread\/([^/?#]+)/);
    if (m) return decodeURIComponent(m[1]);
  }
  if (c.backendUrn) {
    const m = String(c.backendUrn).match(/messagingThread:(.+)$/);
    if (m) return m[1];
  }
  if (c.entityUrn) {
    const m = String(c.entityUrn).match(/msg_conversation:\([^,]+,([^)]+)\)/);
    if (m) return m[1];
  }
  return null;
};
const participantInfo = (p) => {
  if (!p) return null;
  const mem = (p.participantType && p.participantType.member) || null;
  const org = (p.participantType && p.participantType.organization) || null;
  if (mem) {
    const first = attributedText(mem.firstName);
    const last = attributedText(mem.lastName);
    const name = [first, last].filter(Boolean).join(' ').trim();
    const host = p.hostIdentityUrn || '';
    const id = (host.match(/fsd_profile:(.+)$/) || [])[1] || null;
    const handle = mem.publicIdentifier || null;
    const image = vectorFace(mem.profilePicture);
    const out = {
      platform: 'linkedin',
      id: id || handle,
      display_name: name || handle || id || 'Someone',
    };
    if (handle) out.handle = handle;
    if (image) out.image = image;
    return out;
  }
  if (org) {
    const name = attributedText(org.name) || 'Company';
    const host = p.hostIdentityUrn || '';
    const id = (host.match(/fsd_company:(.+)$/) || [])[1] || null;
    return { platform: 'linkedin', id, display_name: name };
  }
  return null;
};
const indexIncluded = (included) => {
  const byUrn = {};
  for (const x of included || []) {
    if (x && x.entityUrn) byUrn[x.entityUrn] = x;
  }
  return byUrn;
};
const mapConv = (c, byUrn, mailboxUrn) => {
  if (!c || c.$type !== 'com.linkedin.messenger.Conversation') return null;
  const id = threadIdFromConv(c);
  if (!id) return null;
  const partUrns = c['*conversationParticipants'] || [];
  const parts = [];
  let face = null;
  let title = attributedText(c.title) || null;
  for (const urn of partUrns) {
    const p = byUrn[urn];
    const info = participantInfo(p);
    if (!info) continue;
    // Skip self in the participant list / title.
    const selfHost = mailboxUrn && ('urn:li:msg_messagingParticipant:' + mailboxUrn);
    if (p && p.entityUrn === selfHost) continue;
    if (p && p.hostIdentityUrn === mailboxUrn) continue;
    parts.push(info);
    if (!face && info.image) face = info.image;
  }
  if (!title) title = parts.map((p) => p.display_name).filter(Boolean).join(', ') || 'Conversation';
  // Manual "Mark as unread" sets read:false with unreadCount often still 0 —
  // Messaging treats !== 0 as unread; use -1 sentinel (WhatsApp parity).
  let unread = typeof c.unreadCount === 'number' ? c.unreadCount : 0;
  if (c.read === false && unread === 0) unread = -1;
  const out = {
    id,
    name: title,
    isGroup: c.groupChat === true,
    unreadCount: unread,
    published: c.lastActivityAt ? new Date(c.lastActivityAt).toISOString() : null,
    externalUrl: c.conversationUrl || ('https://www.linkedin.com/messaging/thread/' + encodeURIComponent(id) + '/'),
  };
  if (parts.length) out.participant = parts;
  if (face) out.image = face;
  if (c.categories && c.categories.indexOf('ARCHIVE') >= 0) out.isArchived = true;
  // Preview from first embedded message element when present.
  const msgEls = (c.messages && c.messages['*elements']) || [];
  if (msgEls[0] && byUrn[msgEls[0]]) {
    const m = byUrn[msgEls[0]];
    const snip = attributedText(m.body);
    if (snip) out.snippet = snip.slice(0, 160);
  }
  return out;
};
const mapMsg = (m, byUrn, mailboxUrn) => {
  if (!m || m.$type !== 'com.linkedin.messenger.Message') return null;
  const convUrn = m['*conversation'] || m.backendConversationUrn || '';
  let conversationId = null;
  const tm = String(convUrn).match(/msg_conversation:\([^,]+,([^)]+)\)/);
  if (tm) conversationId = tm[1];
  else if (m.backendConversationUrn) {
    const bm = String(m.backendConversationUrn).match(/messagingThread:(.+)$/);
    if (bm) conversationId = bm[1];
  }
  const senderUrn = m['*sender'] || m['*actor'] || '';
  const sender = byUrn[senderUrn];
  const info = participantInfo(sender);
  const selfHost = mailboxUrn && ('urn:li:msg_messagingParticipant:' + mailboxUrn);
  const isOutgoing = !!(
    (sender && sender.hostIdentityUrn === mailboxUrn)
    || senderUrn === selfHost
    || (typeof senderUrn === 'string' && mailboxUrn && senderUrn.indexOf(mailboxUrn) >= 0)
  );
  const content = attributedText(m.body) || m.renderContentFallbackText || '';
  const out = {
    id: m.entityUrn || m.backendUrn,
    content,
    published: m.deliveredAt ? new Date(m.deliveredAt).toISOString() : null,
    conversationId,
    type: 'text',
    isOutgoing,
    author: isOutgoing ? 'Me' : ((info && info.display_name) || 'Someone'),
  };
  if (info && !isOutgoing) out.from = info;
  return out;
};

async function liMailboxUrn() {
  const res = await fetch('/voyager/api/me', { headers: liHeaders(), credentials: 'include' });
  if (!res.ok) return null;
  const j = await res.json();
  const mini = (j.included || []).find((x) => x && /MiniProfile/i.test(x.$type || '')) || {};
  const profileUrn = (j.data && j.data['*miniProfile']) || mini.entityUrn || '';
  const m = String(profileUrn).match(/urn:li:(?:fs_miniProfile|fsd_profile):([A-Za-z0-9_-]+)/);
  return m ? ('urn:li:fsd_profile:' + m[1]) : null;
}

const liThreadId = (conversationId) => {
  let threadId = String(conversationId || '');
  const fromUrn = threadId.match(/msg_conversation:\([^,]+,([^)]+)\)/);
  if (fromUrn) threadId = fromUrn[1];
  const fromUrl = threadId.match(/\/messaging\/thread\/([^/?#]+)/);
  if (fromUrl) threadId = decodeURIComponent(fromUrl[1]);
  return threadId;
};
const liConvUrn = (mailbox, conversationId) =>
  'urn:li:msg_conversation:(' + mailbox + ',' + liThreadId(conversationId) + ')';

async function liFetchConversations(limit, archived) {
  const mailbox = await liMailboxUrn();
  if (!mailbox) return { __error: 'no_mailbox' };
  const url = '/voyager/api/voyagerMessagingGraphQL/graphql?queryId='
    + __LI_Q_CONVERSATIONS + '&variables=(mailboxUrn:' + encLiUrn(mailbox) + ')';
  const r = await fetch(url, { headers: liHeaders(), credentials: 'include' });
  if (!r.ok) return { __error: 'conversations_http', what: String(r.status) };
  const j = await r.json();
  const included = j.included || [];
  const byUrn = indexIncluded(included);
  const wantArchived = !!archived;
  const convs = included
    .filter((x) => x && x.$type === 'com.linkedin.messenger.Conversation')
    .map((c) => mapConv(c, byUrn, mailbox))
    .filter(Boolean)
    .filter((c) => !!c.isArchived === wantArchived)
    .sort((a, b) => (Date.parse(b.published) || 0) - (Date.parse(a.published) || 0))
    .slice(0, limit);
  return { mailbox, conversations: convs };
}

/** Patch conversation fields (read true/false) — UI "Mark as read/unread". */
async function liPatchConversation(conversationId, patchSet) {
  const mailbox = await liMailboxUrn();
  if (!mailbox) return { __error: 'no_mailbox' };
  const convUrn = liConvUrn(mailbox, conversationId);
  const url = '/voyager/api/voyagerMessagingDashMessengerConversations?ids=List('
    + encLiUrn(convUrn) + ')';
  const body = { entities: {} };
  body.entities[convUrn] = { patch: { $set: patchSet } };
  const r = await fetch(url, {
    method: 'POST',
    headers: Object.assign({}, liHeaders(), { 'Content-Type': 'application/json; charset=UTF-8' }),
    credentials: 'include',
    body: JSON.stringify(body),
  });
  if (!r.ok && r.status !== 200) {
    const t = await r.text();
    return { __error: 'patch_http', what: String(r.status), head: t.slice(0, 200) };
  }
  // Re-fetch one row for the return shape.
  const pack = await liFetchConversations(80, false);
  const arch = await liFetchConversations(80, true);
  const all = []
    .concat((pack && pack.conversations) || [])
    .concat((arch && arch.conversations) || []);
  const hit = all.find((c) => c && c.id === liThreadId(conversationId));
  return hit || { id: liThreadId(conversationId), ok: true };
}

/** Archive / unarchive — addCategory / removeCategory ARCHIVE. */
async function liSetArchived(conversationId, archived) {
  const mailbox = await liMailboxUrn();
  if (!mailbox) return { __error: 'no_mailbox' };
  const convUrn = liConvUrn(mailbox, conversationId);
  const action = archived ? 'addCategory' : 'removeCategory';
  const url = '/voyager/api/voyagerMessagingDashMessengerConversations?action=' + action;
  const r = await fetch(url, {
    method: 'POST',
    headers: Object.assign({}, liHeaders(), { 'Content-Type': 'application/json; charset=UTF-8' }),
    credentials: 'include',
    body: JSON.stringify({ conversationUrns: [convUrn], category: 'ARCHIVE' }),
  });
  if (!(r.status >= 200 && r.status < 300)) {
    const t = await r.text();
    return { __error: 'archive_http', what: String(r.status), head: t.slice(0, 200) };
  }
  const pack = await liFetchConversations(80, !!archived);
  const hit = ((pack && pack.conversations) || [])
    .find((c) => c && c.id === liThreadId(conversationId));
  return hit || {
    id: liThreadId(conversationId),
    isArchived: !!archived,
    ok: true,
  };
}

/** Delete conversation — HTTP DELETE ids=List(convUrn). Irreversible. */
async function liDeleteConversation(conversationId) {
  const mailbox = await liMailboxUrn();
  if (!mailbox) return { __error: 'no_mailbox' };
  const convUrn = liConvUrn(mailbox, conversationId);
  const url = '/voyager/api/voyagerMessagingDashMessengerConversations?ids=List('
    + encLiUrn(convUrn) + ')';
  const r = await fetch(url, {
    method: 'DELETE',
    headers: liHeaders(),
    credentials: 'include',
  });
  const t = await r.text();
  if (!(r.status >= 200 && r.status < 300)) {
    return { __error: 'delete_http', what: String(r.status), head: t.slice(0, 200) };
  }
  return { id: liThreadId(conversationId), deleted: true, ok: true };
}

async function liFetchMessages(conversationId, limit) {
  const mailbox = await liMailboxUrn();
  if (!mailbox) return { __error: 'no_mailbox' };
  const threadId = liThreadId(conversationId);
  const convUrn = liConvUrn(mailbox, threadId);
  const url = '/voyager/api/voyagerMessagingGraphQL/graphql?queryId='
    + __LI_Q_MESSAGES + '&variables=(conversationUrn:' + encLiUrn(convUrn) + ')';
  const r = await fetch(url, { headers: liHeaders(), credentials: 'include' });
  if (!r.ok) return { __error: 'messages_http', what: String(r.status), threadId };
  const j = await r.json();
  const included = j.included || [];
  const byUrn = indexIncluded(included);
  const messages = included
    .filter((x) => x && x.$type === 'com.linkedin.messenger.Message')
    .map((m) => mapMsg(m, byUrn, mailbox))
    .filter(Boolean)
    .sort((a, b) => (Date.parse(b.published) || 0) - (Date.parse(a.published) || 0))
    .slice(0, limit);
  return { mailbox, conversationId: threadId, messages };
}
"""


def _msg_helpers() -> str:
    return (
        _MSG_HELPERS_JS
        .replace("__Q_CONVERSATIONS__", json.dumps(_Q_CONVERSATIONS))
        .replace("__Q_MESSAGES__", json.dumps(_Q_MESSAGES))
    )


# People search — Voyager dash PEOPLE SRP (Facebook `search_people` parity).
_SEARCH_PEOPLE_JS = r"""
const liSearchCsrf = () => (document.cookie.match(/JSESSIONID="?([^";]+)/) || [])[1] || '';
const liSearchHeaders = () => ({
  'csrf-token': liSearchCsrf(),
  accept: 'application/vnd.linkedin.normalized+json+2.1',
  'x-restli-protocol-version': '2.0.0',
});
const liSplitName = (full) => {
  const parts = String(full || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return {};
  if (parts.length === 1) return { givenName: parts[0] };
  return {
    givenName: parts[0],
    familyName: parts[parts.length - 1],
    additionalName: parts.length > 2 ? parts.slice(1, -1).join(' ') : undefined,
  };
};
const liSearchFace = (entity) => {
  try {
    const attrs = (entity.image && entity.image.attributes) || [];
    for (const a of attrs) {
      const pic = a.detailDataUnion && a.detailDataUnion.nonEntityProfilePicture;
      const arts = pic && pic.vectorImage && pic.vectorImage.artifacts;
      if (arts && arts[0] && arts[0].fileIdentifyingUrlPathSegment) {
        const seg = arts[0].fileIdentifyingUrlPathSegment;
        if (/^https?:/i.test(seg)) return seg;
        const root = (pic.vectorImage.rootUrl) || '';
        return root + seg;
      }
    }
  } catch (e) {}
  return null;
};

const liProfileFace = (prof) => {
  try {
    const pic = prof && (prof.profilePicture || prof.picture);
    const vi = pic && pic.displayImageReference && pic.displayImageReference.vectorImage;
    if (!vi) return null;
    const arts = vi.artifacts || [];
    let best = arts[0];
    for (const a of arts) {
      if ((a.width || 0) >= 150 && (a.width || 0) <= 220) { best = a; break; }
    }
    if (!best || !best.fileIdentifyingUrlPathSegment) return null;
    const seg = best.fileIdentifyingUrlPathSegment;
    if (/^https?:/i.test(seg)) return seg;
    return (vi.rootUrl || '') + seg;
  } catch (e) { return null; }
};
const liPersonFromProfile = (prof) => {
  if (!prof || !prof.entityUrn) return null;
  const m = String(prof.entityUrn).match(/fsd_profile:([A-Za-z0-9_-]+)/);
  if (!m) return null;
  const id = m[1];
  const handle = prof.publicIdentifier || null;
  const name = [prof.firstName, prof.lastName].filter(Boolean).join(' ').trim() || handle || id;
  const names = liSplitName(name);
  const ident = { platform: 'linkedin', id };
  if (handle) ident.handle = handle;
  const out = {
    shape: 'person',
    id,
    name,
    givenName: names.givenName || prof.firstName || null,
    familyName: names.familyName || prof.lastName || null,
    jobTitle: (typeof prof.headline === 'string' ? prof.headline : null) || null,
    url: handle ? ('https://www.linkedin.com/in/' + handle + '/') : null,
    image: liProfileFace(prof),
    identities: [ident],
  };
  // Nested account — Messaging / graph parity with WA/IG list_persons.
  out.accounts = [ident];
  return out;
};

/** Connections address book (+ optional DM participants). `id` = fsd_profile ACo… for compose send. */
async function liListPersons(limit) {
  const n = Math.max(1, Math.min(400, Number(limit) || 200));
  const byId = new Map();
  const push = (p) => {
    if (!p || !p.id || byId.has(p.id)) return;
    byId.set(p.id, p);
  };
  // 1) My Network connections (paged).
  let start = 0;
  const pageSize = Math.min(50, n);
  while (byId.size < n && start < 500) {
    const path = '/voyager/api/relationships/dash/connections'
      + '?decorationId=com.linkedin.voyager.dash.deco.web.mynetwork.ConnectionList-1'
      + '&q=search&sortType=RECENTLY_ADDED&count=' + pageSize + '&start=' + start;
    const r = await fetch(path, { headers: liSearchHeaders(), credentials: 'include' });
    if (!r.ok) break;
    const j = await r.json();
    const included = j.included || [];
    const profiles = {};
    for (const x of included) {
      if (x && /identity\.profile\.Profile$/i.test(x.$type || '') && x.entityUrn) {
        profiles[x.entityUrn] = x;
      }
    }
    let got = 0;
    for (const x of included) {
      if (!x || x.$type !== 'com.linkedin.voyager.dash.relationships.Connection') continue;
      const urn = x.connectedMember || x['*connectedMemberResolutionResult'];
      const prof = urn && profiles[urn];
      const person = liPersonFromProfile(prof);
      if (person) { push(person); got++; }
    }
    if (got === 0) break;
    start += pageSize;
    const total = j.data && j.data.paging && typeof j.data.paging.total === 'number'
      ? j.data.paging.total : null;
    if (total != null && start >= total) break;
    if (got < pageSize) break;
  }
  // 2) People from warm messaging inbox (threads you've already opened).
  try {
    const pack = await liFetchConversations(Math.min(80, n), false);
    for (const c of (pack && pack.conversations) || []) {
      for (const part of c.participant || []) {
        if (!part || !part.id) continue;
        if (byId.has(part.id)) continue;
        const ident = { platform: 'linkedin', id: part.id };
        if (part.handle) ident.handle = part.handle;
        push({
          shape: 'person',
          id: part.id,
          name: part.display_name || part.handle || part.id,
          url: part.handle ? ('https://www.linkedin.com/in/' + part.handle + '/') : null,
          image: part.image || null,
          identities: [ident],
          accounts: [ident],
        });
      }
    }
  } catch (e) {}
  return [...byId.values()].slice(0, n);
}

async function liSearchPeople(query, limit) {
  const q = String(query || '').trim();
  if (!q) return [];
  const n = Math.max(1, Math.min(25, Number(limit) || 8));
  const path = '/voyager/api/search/dash/clusters'
    + '?decorationId=com.linkedin.voyager.dash.deco.search.SearchClusterCollection-1'
    + '&origin=SWITCH_SEARCH_VERTICAL&q=all'
    + '&query=(keywords:' + encodeURIComponent(q)
    + ',flagshipSearchIntent:SEARCH_SRP,queryParameters:(resultType:List(PEOPLE)),'
    + 'includeFiltersInResponse:false)&start=0&count=' + n;
  const r = await fetch(path, { headers: liSearchHeaders(), credentials: 'include' });
  if (!r.ok) {
    const t = await r.text();
    return { __error: 'search_http', what: String(r.status), head: t.slice(0, 200) };
  }
  const j = await r.json();
  const included = j.included || [];
  const needle = q.replace(/^@/, '').toLowerCase();
  const people = [];
  const seen = new Set();
  for (const x of included) {
    if (!x || x.$type !== 'com.linkedin.voyager.dash.search.EntityResultViewModel') continue;
    const m = String(x.entityUrn || '').match(/fsd_profile:([^,)]+)/);
    if (!m) continue;
    const id = m[1];
    if (seen.has(id)) continue;
    seen.add(id);
    const name = (x.title && x.title.text) || null;
    const nav = x.navigationUrl || '';
    let handle = null;
    const hm = nav.match(/\/in\/([^/?#]+)/);
    if (hm) handle = decodeURIComponent(hm[1]);
    const names = liSplitName(name);
    const ident = { platform: 'linkedin', id };
    if (handle) ident.handle = handle;
    const out = {
      shape: 'person',
      id,
      name,
      givenName: names.givenName || null,
      familyName: names.familyName || null,
      jobTitle: (x.primarySubtitle && x.primarySubtitle.text) || null,
      about: (x.secondarySubtitle && x.secondarySubtitle.text) || null,
      url: handle ? ('https://www.linkedin.com/in/' + handle + '/') : null,
      image: liSearchFace(x),
      identities: [ident],
    };
    if (names.additionalName) out.additionalName = names.additionalName;
    people.push(out);
  }
  // Prefer exact handle / id hits when the query looks like one.
  people.sort((a, b) => {
    const ah = (a.identities && a.identities[0] && a.identities[0].handle || '').toLowerCase();
    const bh = (b.identities && b.identities[0] && b.identities[0].handle || '').toLowerCase();
    const as = (ah === needle || a.id === needle) ? 0 : 1;
    const bs = (bh === needle || b.id === needle) ? 0 : 1;
    return as - bs;
  });
  return people.slice(0, n);
}
"""


def _is_linkedin_face_url(url: str) -> bool:
    """True for profile/company avatar CDN URLs — never post media."""
    u = url or ""
    return "profile-displayphoto" in u or "company-logo" in u


async def _read_cookies():
    return await browser_session.read_cookies(_PAGE)


async def _ensure_feed(*, force: bool = False):
    on_feed = await browser_session.eval(
        _PAGE,
        "location.hostname.indexOf('linkedin.com') !== -1"
        " && location.pathname.indexOf('/feed') === 0"
        " && !!document.querySelector('[data-testid=\"mainFeed\"]')"
        " && document.readyState !== 'loading'",
        timeout=15,
    )
    if force or on_feed is not True:
        await browser_session.navigate(_PAGE, _FEED_URL, timeout=60)
        await browser_session.eval(
            _PAGE,
            "(async () => { await new Promise(r => setTimeout(r, 2800)); return true; })()",
            timeout=20,
        )


async def _ensure_messaging(*, force: bool = False):
    """Land `/messaging/` so Voyager Messaging GraphQL + composer are warm."""
    on_msg = await browser_session.eval(
        _PAGE,
        "location.hostname.indexOf('linkedin.com') !== -1"
        " && location.pathname.indexOf('/messaging') === 0"
        " && document.readyState !== 'loading'",
        timeout=15,
    )
    if force or on_msg is not True:
        await browser_session.navigate(_PAGE, _MESSAGING_URL, timeout=60)
        await browser_session.eval(
            _PAGE,
            "(async () => { await new Promise(r => setTimeout(r, 2800)); return true; })()",
            timeout=20,
        )


async def _eval(js: str, *, timeout_s: int = 60):
    return await browser_session.eval(_PAGE, js, timeout=timeout_s)


async def _require_session():
    jar = await _read_cookies()
    if _SESSION_COOKIE not in jar:
        return browser_session.needs_auth(
            "LinkedIn session missing (no li_at cookie).",
            login_op="linkedin.login",
        )
    return None


async def _viewer_identity():
    await _ensure_feed(force=False)
    me = await _eval(_ME_JS, timeout_s=45)
    if isinstance(me, dict) and not me.get("__error"):
        return me
    return None


# ──────────────────────────────────────────────────────────────────────
# Account trio
# ──────────────────────────────────────────────────────────────────────


async def _account_from_bg() -> dict | None:
    """Live bg-profile account, or None if `li_at` is missing."""
    jar = await _read_cookies()
    if _SESSION_COOKIE not in jar:
        return None
    acct = {
        "authenticated": True,
        "platform": "linkedin",
        "at": {
            "shape": "product",
            "name": "LinkedIn",
            "url": "https://www.linkedin.com/",
        },
    }
    try:
        me = await _viewer_identity()
        if isinstance(me, dict) and me.get("plainId"):
            acct["identifier"] = me["plainId"]
            if me.get("handle"):
                acct["handle"] = me["handle"]
            if me.get("name"):
                acct["name"] = me["name"]
    except Exception:
        pass
    # Self-register needs an identifier; fall back to cookie presence marker.
    if not acct.get("identifier"):
        acct["identifier"] = "linkedin"
    return acct


async def _login_profile_signed_in() -> bool:
    """True when the headed login profile has a real LinkedIn session.

    Prefer httpOnly `li_at` (cookie read, mode=login). Fall back to in-page
    signals — left /login, feed/nav chrome, or `liap=true` — so we close as
    soon as the human finishes even if cookie propagation lags one tick.
    """
    try:
        if await browser_session.session_cookie_present(
            _PAGE, _SESSION_COOKIE, mode="login"
        ):
            return True
    except Exception:
        # Login profile not open / cookies verb refused → not signed in here.
        return False
    try:
        sig = await browser_session.eval(
            _PAGE, _LOGIN_DONE_JS, mode="login", timeout=15
        )
        return isinstance(sig, dict) and sig.get("ok") is True
    except Exception:
        return False


async def _finish_login_window() -> dict | None:
    """Merge login-profile session → bg, close the window, return bg account."""
    try:
        await browser_session.merge_login_session(urls=_MERGE_URLS)
    except Exception:
        # Still try close; check_session will say if merge failed.
        pass
    try:
        await browser_session.close_login_window(strategy="profile")
    except Exception:
        pass
    # Give bg cookie jar a beat to settle.
    await asyncio.sleep(0.8)
    return await _account_from_bg()


@account.check
@returns("account")
@timeout(45)
async def check_session(**params):
    """Verify LinkedIn login — httpOnly `li_at` means the session is real."""
    acct = await _account_from_bg()
    if acct:
        return acct
    return {"authenticated": False}


@account.login
@returns("account | auth_challenge")
@timeout(300)
async def login(**params):
    """Sign in to LinkedIn; auto-close the login window when done.

    Opens a headed login-profile window, polls until `li_at` / signed-in
    chrome appears on that page, merges cookies into the headless bg
    profile, closes the window, and returns the account. If the human
    hasn't finished within ~4 minutes, leaves the window open and returns
    an ``auth_challenge`` (agent can keep polling ``check_session``).
    """
    existing = await _account_from_bg()
    if existing:
        return existing

    # Login window already open + signed in (e.g. human finished before we
    # polled) → merge/close without opening a second chrome.
    if await _login_profile_signed_in():
        acct = await _finish_login_window()
        if acct:
            return acct

    challenge = await browser_session.login_window(
        _LOGIN_URL,
        label="LinkedIn",
        instructions=(
            "Sign in to LinkedIn in the window that opened. Complete password "
            "/ 2FA / challenge — the plugin detects the signed-in feed/nav "
            "(or `li_at`), merges into the background profile, and closes "
            "the window automatically."
        ),
        retrieval={
            "via": "email",
            "look_for": "a LinkedIn login code or sign-in confirmation",
        },
    )

    deadline = time.monotonic() + _LOGIN_WAIT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(_LOGIN_POLL_S)
        if not await _login_profile_signed_in():
            continue
        acct = await _finish_login_window()
        if acct:
            return acct
        # Signed in on login profile but merge didn't land — keep trying.
        break

    # Timed out still on the form, or merge failed after detect.
    if await _login_profile_signed_in():
        acct = await _finish_login_window()
        if acct:
            return acct

    if isinstance(challenge, dict):
        challenge["instructions"] = (
            "LinkedIn sign-in window is still open. Finish signing in "
            "(password / 2FA / challenge). The plugin will detect the "
            "signed-in page, merge into background, and close the window — "
            "or call linkedin.login again / check_session after you finish."
        )
    return challenge


@account.logout
@returns({"ok": "boolean", "message": "string"})
@timeout(45)
async def logout(**params):
    """Log out of LinkedIn in the engine browser."""
    return {"ok": False, "message": "logout not yet implemented — see readme"}


# ──────────────────────────────────────────────────────────────────────
# Feed → social_feeds (Feeds app)
# ──────────────────────────────────────────────────────────────────────


async def _list_feed_page(*, limit: int, cursor: str | None = None):
    """List durable MainFeed posts. `cursor` is reserved (token payload JSON)."""
    await _ensure_feed(force=False)
    # Warm a few SDUI pages so `limit` is reachable without the agent scrolling.
    pages = max(1, min(6, (int(limit) + 4) // 5))
    js = (
        _LIST_FEED_JS
        .replace("__LIMIT__", str(int(limit)))
        .replace("__PAGES__", str(pages))
    )
    # cursor currently unused for fetchMoreItems (in-page token advances itself);
    # kept for Feeds pager symmetry / future SDUI replay.
    _ = cursor
    out = await _eval(js, timeout_s=120)
    if isinstance(out, dict) and out.get("__error"):
        return app_error(
            f"LinkedIn feed unavailable: {out.get('what') or out['__error']}",
            code="NotReady",
        )
    if not isinstance(out, dict):
        return {"_items": [], "pageInfo": {"endCursor": None, "hasNext": False}}
    posts = out.get("posts") if isinstance(out.get("posts"), list) else []
    page_info = out.get("pageInfo") if isinstance(out.get("pageInfo"), dict) else {}
    # Strip heavy mediaUrl from list video rows (hydrate via get_post); keep images.
    for p in posts:
        if isinstance(p, dict) and p.get("mediaType") == "video":
            p.pop("mediaUrl", None)
    return {
        "_items": posts,
        "pageInfo": {
            "endCursor": page_info.get("endCursor"),
            "hasNext": bool(page_info.get("hasNext")),
        },
    }


@returns("post[]")
@provides("social_feeds", account_param="account")
@timeout(180)
async def list_posts(
    *,
    limit=20,
    surface="feed",
    cursor=None,
    **params,
):
    """List LinkedIn home-feed posts for the Feeds app.

    `surface`:
      - `feed` / `all` — MainFeed durable posts (paged)
      - `stories` — empty (LinkedIn has no Stories tray here)

    Ads (`Promoted`) dropped in-plugin. Brokered as `social_feeds`.
    """
    jar = await _read_cookies()
    if _SESSION_COOKIE not in jar:
        return browser_session.needs_auth(
            "LinkedIn session missing (no li_at cookie).",
            login_op="linkedin.login",
        )
    surf = str(surface or "feed").lower().strip()
    if surf == "stories":
        return []
    lim = int(limit)
    cur = str(cursor) if cursor else None
    pack = await _list_feed_page(limit=lim, cursor=cur)
    if surf == "all" and isinstance(pack, dict) and isinstance(pack.get("_items"), list):
        return pack["_items"]
    return pack


@returns("post")
@provides("get_post", account_param="account")
@timeout(120)
async def get_post(*, id, **params):
    """Get one feed post; hydrate media into the blob store when possible."""
    jar = await _read_cookies()
    if _SESSION_COOKIE not in jar:
        return browser_session.needs_auth(
            "LinkedIn session missing (no li_at cookie).",
            login_op="linkedin.login",
        )
    sid = str(id)
    await _ensure_feed(force=False)
    # Reuse list collector body inside get (warm a couple pages first).
    list_body = (
        _LIST_FEED_JS
        .replace("__LIMIT__", "40")
        .replace("__PAGES__", "3")
        .strip()
    )
    # Strip outer async wrapper for nesting — _LIST_FEED_JS is already (async ()=>{…})()
    # so splice as expression.
    js = (
        "(async () => {\n"
        f"  const want = {json.dumps(sid)};\n"
        f"  const list = await {list_body};\n"
        "  const posts = (list && list.posts) || [];\n"
        "  let hit = posts.find((p) => p && p.id === want);\n"
        "  if (!hit) hit = posts.find((p) => p && p.from && p.from.id === want);\n"
        "  if (!hit) return { __error: 'not_found', what: 'post not in current MainFeed window' };\n"
        "  return hit;\n"
        "})()"
    )
    entity = await _eval(js, timeout_s=120)
    if isinstance(entity, dict) and entity.get("__error"):
        return app_error(
            f"LinkedIn post unavailable: {entity.get('what') or entity['__error']}",
            code="NotFound",
        )
    if not isinstance(entity, dict):
        return app_error("LinkedIn post unavailable", code="NotFound")

    # Never fall back to `image` (author face) — that polluted avatars/attaches.
    media_url = entity.get("mediaUrl") or entity.get("thumb")
    if media_url and not _is_linkedin_face_url(str(media_url)):
        blob = await _fetch_media(str(media_url))
        if blob and blob.get("data"):
            try:
                path = await blobs.put(
                    base64.b64decode(blob["data"]),
                    mime=blob.get("mime") or "application/octet-stream",
                )
                entity["attaches"] = [{
                    "path": path,
                    "mime": blob.get("mime"),
                    "size": blob.get("size"),
                    "role": "media",
                }]
            except Exception:
                pass
    return entity


# ──────────────────────────────────────────────────────────────────────
# Interactions — SDUI reactions (+ counts on list); comments still open
# ──────────────────────────────────────────────────────────────────────

_REACT_JS = r"""
(async () => {
  const postId = __POST_ID__;
  const emoji = __EMOJI__;

  const map = {
    '👍': 'LIKE', like: 'LIKE', LIKE: 'LIKE',
    '👏': 'PRAISE', celebrate: 'PRAISE', PRAISE: 'PRAISE',
    '❤️': 'EMPATHY', love: 'EMPATHY', EMPATHY: 'EMPATHY',
    '💡': 'INTEREST', insightful: 'INTEREST', INTEREST: 'INTEREST',
    '🤔': 'APPRECIATION', support: 'APPRECIATION', APPRECIATION: 'APPRECIATION',
    '😂': 'ENTERTAINMENT', funny: 'ENTERTAINMENT', ENTERTAINMENT: 'ENTERTAINMENT',
  };
  const clear = emoji == null || emoji === '' || emoji === false;
  const reactionType = clear ? null : (map[emoji] || map[String(emoji).toUpperCase()] || 'LIKE');

  const fiberKey = (el) => Object.keys(el || {}).find(
    (k) => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance')
  );

  const cards = [...document.querySelectorAll('[data-testid="mainFeed"] [data-lazy-mount-id]')]
    .filter((c) => /^Feed post/i.test((c.innerText || '').trim()) && !/\bPromoted\b/.test(c.innerText || ''));

  const matchCard = () => {
    for (const el of cards) {
      const html = el.innerHTML || '';
      const textIds = [
        ...(html.match(/urn:li:(?:share|activity|ugcPost):\d+/g) || []),
      ];
      const btn = [...el.querySelectorAll('button,[role="button"]')].find((b) =>
        /Reaction button state/i.test(b.getAttribute('aria-label') || '')
      );
      let activityUrn = null;
      if (btn) {
        let f = btn[fiberKey(btn)];
        for (let i = 0; i < 50 && f; i++, f = f.return) {
          const p = f.memoizedProps || f.pendingProps || {};
          try {
            const seen = new WeakSet();
            const raw = JSON.stringify(p, (k, v) => {
              if (typeof v === 'function') return undefined;
              if (v && typeof v === 'object') {
                if (seen.has(v)) return undefined;
                seen.add(v);
              }
              return v;
            });
            const m = raw && raw.match(/"activityId"\s*:\s*"(\d{6,})"/);
            if (m) { activityUrn = 'urn:li:activity:' + m[1]; break; }
          } catch (e) {}
        }
      }
      const ids = new Set([...textIds, activityUrn].filter(Boolean));
      if (ids.has(postId) || (activityUrn && activityUrn === postId)) {
        return { el, activityUrn, shareUrn: textIds.find((u) => /urn:li:share:/.test(u)) || null };
      }
    }
    return null;
  };

  let hit = matchCard();
  if (!hit || !hit.activityUrn) {
    // Warm a page of feed then retry
    return { __error: 'not_in_feed', what: 'post not in current MainFeed — open feed first' };
  }
  const activityId = hit.activityUrn.replace(/^urn:li:activity:/, '');
  const requestId = clear
    ? 'com.linkedin.sdui.reactions.delete'
    : 'com.linkedin.sdui.reactions.create';
  const payload = {
    threadUrn: {
      threadUrnActivityThreadUrn: {
        __typename: 'proto_com_linkedin_common_ActivityUrn',
        activityUrn: { activityId },
      },
    },
    reactionSource: 'Update',
  };
  if (!clear) payload.reactionType = 'ReactionType_' + reactionType;

  const body = {
    requestId,
    serverRequest: {
      requestId,
      requestedArguments: {
        $type: 'proto.sdui.actions.requests.RequestedArguments',
        requestedStateKeys: [],
        payload,
        requestMetadata: {
          $type: 'proto.sdui.common.RequestMetadata',
          currentActor: {
            $type: 'proto.sdui.bindings.core.Bindable',
            key: { key: { value: { $case: 'id', id: 'identitySwitcherActorContext-' + hit.activityUrn } } },
            content: { $case: 'stringBinding', stringBinding: {} },
          },
        },
      },
      isApfcEnabled: false,
      isStreaming: false,
      rumPageKey: '',
    },
    states: [],
    requestedArguments: {
      $type: 'proto.sdui.actions.requests.RequestedArguments',
      requestedStateKeys: [],
      payload,
      requestMetadata: {
        $type: 'proto.sdui.common.RequestMetadata',
        currentActor: {
          $type: 'proto.sdui.bindings.core.Bindable',
          key: { key: { value: { $case: 'id', id: 'identitySwitcherActorContext-' + hit.activityUrn } } },
          content: { $case: 'stringBinding', stringBinding: {} },
        },
      },
    },
    isApfcEnabled: false,
    isStreaming: false,
    rumPageKey: '',
    screenId: 'com.linkedin.sdui.flagshipnav.feed.MainFeed',
  };

  const csrf = (document.cookie.match(/JSESSIONID="?([^";]+)/) || [])[1] || '';
  if (!csrf) return { __error: 'no_csrf' };
  const url = '/flagship-web/rsc-action/actions/server-request?sduiid=' + encodeURIComponent(requestId);
  const res = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'content-type': 'application/json',
      'csrf-token': csrf,
      'x-li-rsc-stream': 'true',
      accept: 'text/x-component',
    },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) {
    return { __error: 'react_http', what: String(res.status), head: text.slice(0, 200) };
  }
  if (/errors\":\[[^\]]+\]/.test(text) && !/\"errors\":\[\]/.test(text.slice(0, 500))) {
    return { __error: 'react_failed', what: text.slice(0, 300) };
  }
  return {
    ok: true,
    activityUrn: hit.activityUrn,
    reactionType: clear ? null : reactionType,
    emoji: emoji || null,
    via: requestId,
  };
})()
"""


@returns({"ok": "boolean"})
@provides("post_react", account_param="account")
@timeout(90)
async def react_to_post(*, id, emoji=None, **params):
    """React to (or clear reaction on) a LinkedIn feed post.

    Uses flagship SDUI `com.linkedin.sdui.reactions.create|delete` against the
    card's activity URN (not the share URN). `emoji`: 👍/👏/❤️/💡/🤔/😂 or
    like/celebrate/…; omit/null/'' to clear. Brokered as `post_react`.
    """
    jar = await _read_cookies()
    if _SESSION_COOKIE not in jar:
        return browser_session.needs_auth(
            "LinkedIn session missing (no li_at cookie).",
            login_op="linkedin.login",
        )
    await _ensure_feed(force=False)
    js = (
        _REACT_JS
        .replace("__POST_ID__", json.dumps(str(id)))
        .replace("__EMOJI__", json.dumps(emoji) if emoji is not None else "null")
    )
    out = await _eval(js, timeout_s=60)
    if isinstance(out, dict) and out.get("__error"):
        return app_error(
            f"LinkedIn react failed: {out.get('what') or out['__error']}",
            code="NotReady",
        )
    return out if isinstance(out, dict) else {"ok": True}


_LIST_COMMENTS_JS = r"""
(async () => {
  const postId = __POST_ID__;
  const limit = __LIMIT__;
  const cursor = __CURSOR__;

  const csrf = (document.cookie.match(/JSESSIONID="?([^";]+)/) || [])[1] || '';
  if (!csrf) return { __error: 'no_csrf' };

  const fiberKey = (el) => Object.keys(el || {}).find(
    (k) => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance')
  );

  const activityFromEl = (el) => {
    const btn = [...el.querySelectorAll('button,[role="button"]')].find((b) =>
      /Reaction button state/i.test(b.getAttribute('aria-label') || '')
    );
    if (!btn) return null;
    let f = btn[fiberKey(btn)];
    for (let i = 0; i < 50 && f; i++, f = f.return) {
      try {
        const seen = new WeakSet();
        const raw = JSON.stringify(f.memoizedProps || f.pendingProps || {}, (k, v) => {
          if (typeof v === 'function') return undefined;
          if (v && typeof v === 'object') {
            if (seen.has(v)) return undefined;
            seen.add(v);
          }
          return v;
        });
        const m = raw && raw.match(/"activityId"\s*:\s*"(\d{6,})"/);
        if (m) return 'urn:li:activity:' + m[1];
      } catch (e) {}
    }
    return null;
  };

  const resolveThreadUrn = () => {
    if (/^urn:li:(activity|ugcPost):\d+$/.test(postId)) return postId;
    const cards = [...document.querySelectorAll('[data-testid="mainFeed"] [data-lazy-mount-id]')]
      .filter((c) => /^Feed post/i.test((c.innerText || '').trim()) && !/\bPromoted\b/.test(c.innerText || ''));
    for (const el of cards) {
      const html = el.innerHTML || '';
      const urns = [...new Set(html.match(/urn:li:(?:share|activity|ugcPost):\d+/g) || [])];
      const activityUrn = activityFromEl(el);
      const ids = new Set([...urns, activityUrn].filter(Boolean));
      if (!ids.has(postId) && !(activityUrn && activityUrn === postId)) continue;
      if (activityUrn) return activityUrn;
      const ugc = urns.find((u) => /ugcPost/.test(u));
      if (ugc) return ugc;
    }
    // Last resort: treat bare numeric as activity id
    if (/^\d{6,}$/.test(postId)) return 'urn:li:activity:' + postId;
    return null;
  };

  const faceUrl = (mini) => {
    if (!mini || !mini.picture) return null;
    const pic = mini.picture;
    if (typeof pic === 'string') return pic;
    const root = pic.rootUrl || '';
    const arts = Array.isArray(pic.artifacts) ? pic.artifacts.slice() : [];
    arts.sort((a, b) => (a.width || 0) - (b.width || 0));
    const art = arts.find((a) => (a.width || 0) >= 100) || arts[0];
    if (root && art && art.fileIdentifyingUrlPathSegment) {
      return root + art.fileIdentifyingUrlPathSegment;
    }
    return null;
  };

  let threadUrn = resolveThreadUrn();
  if (!threadUrn) {
    return { __error: 'no_thread', what: 'need activity/ugcPost URN (share alone fails comments)' };
  }

  let start = 0;
  if (cursor != null && cursor !== '') {
    const n = Number(cursor);
    if (Number.isFinite(n)) start = Math.max(0, Math.floor(n));
    else {
      try {
        const j = JSON.parse(cursor);
        if (j && Number.isFinite(Number(j.start))) start = Math.max(0, Math.floor(Number(j.start)));
      } catch (e) {}
    }
  }
  const count = Math.max(1, Math.min(30, limit || 15));
  const url = '/voyager/api/feed/comments?q=comments&updateId='
    + encodeURIComponent(threadUrn)
    + '&count=' + count
    + '&start=' + start;

  const res = await fetch(url, {
    credentials: 'include',
    headers: {
      'csrf-token': csrf,
      accept: 'application/vnd.linkedin.normalized+json+2.1',
      'x-restli-protocol-version': '2.0.0',
    },
  });
  if (!res.ok) {
    const t = await res.text();
    return { __error: 'comments_http', what: String(res.status), head: t.slice(0, 200), threadUrn };
  }
  const j = await res.json();
  const profiles = {};
  for (const x of j.included || []) {
    if (x && /MiniProfile/i.test(x.$type || '') && x.entityUrn) profiles[x.entityUrn] = x;
  }
  const items = [];
  for (const c of j.included || []) {
    if (!c || !/voyager\.feed\.Comment/.test(c.$type || '')) continue;
    const commenter = c.commenter || {};
    const miniUrn = commenter['*miniProfile'] || null;
    const mini = (miniUrn && profiles[miniUrn]) || null;
    const author = mini
      ? [mini.firstName, mini.lastName].filter(Boolean).join(' ')
      : (commenter.commenterProfileId || 'Someone');
    const content = (c.commentary && c.commentary.text)
      || (c.commentV2 && c.commentV2.text)
      || '';
    const id = c.urn || c.entityUrn || c.dashEntityUrn;
    if (!id) continue;
    const handle = (mini && mini.publicIdentifier) || null;
    const memberUrn = commenter.urn || null; // urn:li:member:N
    const memberId = memberUrn && String(memberUrn).match(/urn:li:member:(\d+)/)
      ? String(memberUrn).match(/urn:li:member:(\d+)/)[1]
      : null;
    const authorId = memberId || handle || commenter.commenterProfileId || null;
    const face = faceUrl(mini);
    const row = {
      id: String(id),
      author: author || 'Someone',
      authorId,
      content: String(content || ''),
      published: typeof c.createdTime === 'number'
        ? new Date(c.createdTime).toISOString()
        : null,
      image: face,
    };
    if (c.parentCommentUrn) row.replyTo = String(c.parentCommentUrn);
    if (c.permalink) row.externalUrl = c.permalink;
    if (authorId) {
      row.from = {
        platform: 'linkedin',
        id: authorId,
        display_name: author || undefined,
      };
      if (handle) row.from.handle = handle;
    }
    // Graph person — extraction only links children that self-declare shape.
    if (authorId && author) {
      const parts = String(author).trim().split(/\s+/).filter(Boolean);
      const ident = { platform: 'linkedin', id: memberId || authorId };
      if (handle) ident.handle = handle;
      row.posted_by = {
        shape: 'person',
        name: author,
        givenName: parts[0] || null,
        familyName: parts.length > 1 ? parts[parts.length - 1] : null,
        additionalName: parts.length > 2 ? parts.slice(1, -1).join(' ') : null,
        jobTitle: (mini && mini.occupation) || null,
        about: (mini && mini.occupation) || null,
        image: face,
        url: handle ? ('https://www.linkedin.com/in/' + handle + '/') : null,
        identities: [ident],
      };
    }
    items.push(row);
  }

  const paging = (j.data && j.data.paging) || {};
  const total = typeof paging.total === 'number' ? paging.total : null;
  const metaCount = j.data && j.data.metadata && j.data.metadata.updatedCommentCount;
  // Voyager often embeds replies in `included` beyond `count` — return the
  // whole page; advance cursor by the requested `count` / paging window.
  const pageCount = typeof paging.count === 'number' ? paging.count : count;
  const pageStart = typeof paging.start === 'number' ? paging.start : start;
  const nextStart = pageStart + pageCount;
  const hasNext = total != null ? nextStart < total : items.length > 0;
  return {
    _items: items,
    pageInfo: {
      endCursor: hasNext ? String(nextStart) : null,
      hasNext: !!hasNext,
    },
    threadUrn,
    total: total != null ? total : (metaCount || null),
  };
})()
"""


@returns({"_items": "comment[]", "pageInfo": "object"})
@provides("post_comments", account_param="account")
@timeout(120)
async def list_comments(*, id, limit=20, cursor=None, **params):
    """List comments on a LinkedIn feed post.

    Voyager `GET /voyager/api/feed/comments?q=comments&updateId=<activity|ugcPost>`.
    Share URNs alone return empty — resolve to the card's activity URN first.
    Brokered as `post_comments`. Read-only.
    """
    jar = await _read_cookies()
    if _SESSION_COOKIE not in jar:
        return browser_session.needs_auth(
            "LinkedIn session missing (no li_at cookie).",
            login_op="linkedin.login",
        )
    await _ensure_feed(force=False)
    cur_js = json.dumps(str(cursor)) if cursor not in (None, "") else "null"
    js = (
        _LIST_COMMENTS_JS
        .replace("__POST_ID__", json.dumps(str(id)))
        .replace("__LIMIT__", str(int(limit)))
        .replace("__CURSOR__", cur_js)
    )
    out = await _eval(js, timeout_s=90)
    if isinstance(out, dict) and out.get("__error"):
        return app_error(
            f"LinkedIn list_comments failed: {out.get('what') or out['__error']}",
            code="NotReady",
        )
    if not isinstance(out, dict):
        return {"_items": [], "pageInfo": {"endCursor": None, "hasNext": False}}
    items = out.get("_items") if isinstance(out.get("_items"), list) else []
    page_info = out.get("pageInfo") if isinstance(out.get("pageInfo"), dict) else {}
    return {
        "_items": items,
        "pageInfo": {
            "endCursor": page_info.get("endCursor"),
            "hasNext": bool(page_info.get("hasNext")),
        },
    }


async def _fetch_media(url: str):
    """CDN bytes via `browser_session.response_body` (CDP Network.getResponseBody)."""
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
    """Last-resort CDN fetch via engine http.request."""
    try:
        resp = await client.get(
            url,
            client="browser",
            headers={
                "Referer": "https://www.linkedin.com/",
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
    return {
        "data": base64.b64encode(raw).decode("ascii"),
        "mime": (resp.get("headers") or {}).get("content-type", "image/jpeg").split(";")[0],
        "size": len(raw),
    }


# ──────────────────────────────────────────────────────────────────────
# People search → search_people (Facebook parity)
# ──────────────────────────────────────────────────────────────────────


@returns("person[]")
@provides("search_people", account_param="account")
@timeout(60)
async def search_people(*, query: str, limit: int = 8, **params):
    """People search — LinkedIn PEOPLE search SRP (Voyager dash clusters).

    Brokered as `search_people` (same role as Facebook's typeahead). Returns
    light `person` stubs: id (fsd_profile ACo…), name, face, url, identities.
    """
    guard = await _require_session()
    if guard:
        return guard
    q = str(query or "").strip()
    if not q:
        return []
    # Any logged-in LinkedIn surface is fine — search is same-origin fetch.
    await _ensure_feed(force=False)
    out = await _eval(
        "(async () => {\n"
        + _SEARCH_PEOPLE_JS
        + f"\n  return await liSearchPeople({json.dumps(q)}, {int(limit)});\n"
        + "})()",
        timeout_s=45,
    )
    if isinstance(out, dict) and out.get("__error"):
        return app_error(
            f"LinkedIn people search unavailable: {out.get('what') or out['__error']}",
            code="NotReady",
        )
    return out if isinstance(out, list) else []


@returns("person[]")
@timeout(120)
async def list_persons(*, limit=200, **params):
    """List LinkedIn connections (+ recent DM participants) for New Chat.

    Messaging reads this as `chats` verb `list_persons` (no separate
    `@provides`). Each person's `id` is the fsd_profile `ACo…` token —
    what `send_message`'s `to` accepts for compose-to-profile.
    """
    guard = await _require_session()
    if guard:
        return guard
    await _ensure_messaging(force=False)
    out = await _eval(
        "(async () => {\n"
        + _msg_helpers()
        + "\n"
        + _SEARCH_PEOPLE_JS
        + f"\n  return await liListPersons({int(limit)});\n"
        + "})()",
        timeout_s=100,
    )
    if isinstance(out, dict) and out.get("__error"):
        return app_error(
            f"LinkedIn connections unavailable: {out.get('what') or out['__error']}",
            code="NotReady",
        )
    return out if isinstance(out, list) else []


# ──────────────────────────────────────────────────────────────────────
# Messaging → chats / message_send / message_watch
# ──────────────────────────────────────────────────────────────────────


@returns("conversation[]")
@provides("chats", account_param="account")
@timeout(90)
async def list_conversations(*, limit=50, archived=False, **params):
    """List LinkedIn Messaging inbox threads (most recent first).

    Same-origin `voyagerMessagingGraphQL` `messengerConversations` — mailbox
    URN from Voyager `/me`. Ids are `/messaging/thread/{id}` slugs.

    Args:
        archived: When true, list ARCHIVE-category threads (Messaging shelf).
    """
    guard = await _require_session()
    if guard:
        return guard
    await _ensure_messaging(force=False)
    out = await _eval(
        "(async () => {\n"
        + _msg_helpers()
        + f"\n  return await liFetchConversations({int(limit)}, {json.dumps(bool(archived))});\n"
        + "})()",
        timeout_s=75,
    )
    if isinstance(out, dict) and out.get("__error"):
        return app_error(
            f"LinkedIn conversations unavailable: {out.get('what') or out['__error']}",
            code="NotReady",
        )
    if not isinstance(out, dict):
        return []
    rows = out.get("conversations") if isinstance(out.get("conversations"), list) else []
    return rows


def _norm_thread_id(conversation_id: str) -> str:
    s = str(conversation_id or "").strip()
    m = re.search(r"msg_conversation:\([^,]+,([^)]+)\)", s)
    if m:
        return m.group(1)
    m = re.search(r"/messaging/thread/([^/?#]+)", s)
    if m:
        return m.group(1)
    return s


async def _msg_action(js_call: str, *, timeout_s: int = 60):
    """Run an in-page messaging mutation; map `__error` → app_error."""
    guard = await _require_session()
    if guard:
        return guard
    await _ensure_messaging(force=False)
    out = await _eval(
        "(async () => {\n" + _msg_helpers() + "\n  return await " + js_call + ";\n})()",
        timeout_s=timeout_s,
    )
    if isinstance(out, dict) and out.get("__error"):
        return app_error(
            f"LinkedIn messaging action failed: {out.get('what') or out['__error']}",
            code="NotReady",
        )
    return out


@returns("conversation")
@provides("message_mark_read", account_param="account")
@timeout(60)
async def mark_read(*, conversation_id, **params):
    """Mark a LinkedIn conversation read (clears unread badge).

    Voyager dash patch: `$set: { read: true }` on
    `voyagerMessagingDashMessengerConversations?ids=List(…)`.
    """
    tid = _norm_thread_id(conversation_id)
    if not tid:
        return app_error("conversation_id required", code="BadRequest")
    return await _msg_action(
        f"liPatchConversation({json.dumps(tid)}, {{ read: true }})"
    )


@returns("conversation")
@provides("message_mark_unread", account_param="account")
@timeout(60)
async def mark_unread(*, conversation_id, **params):
    """Mark a LinkedIn conversation unread (UI \"Mark as unread\").

    Same dash patch as mark_read with `read: false`.
    """
    tid = _norm_thread_id(conversation_id)
    if not tid:
        return app_error("conversation_id required", code="BadRequest")
    return await _msg_action(
        f"liPatchConversation({json.dumps(tid)}, {{ read: false }})"
    )


@returns("conversation")
@provides("message_archive", account_param="account")
@timeout(60)
async def set_archived(*, conversation_id, archived=True, **params):
    """Archive or unarchive a LinkedIn conversation.

    `action=addCategory` / `removeCategory` with `category: "ARCHIVE"` —
    same endpoints the inbox ⋯ menu fires.
    """
    tid = _norm_thread_id(conversation_id)
    if not tid:
        return app_error("conversation_id required", code="BadRequest")
    return await _msg_action(
        f"liSetArchived({json.dumps(tid)}, {json.dumps(bool(archived))})"
    )


@returns({"ok": "boolean", "id": "string", "deleted": "boolean"})
@provides("message_delete", account_param="account")
@timeout(60)
async def delete_conversation(*, conversation_id, **params):
    """Delete a LinkedIn conversation (irreversible).

    HTTP DELETE `voyagerMessagingDashMessengerConversations?ids=List(…)`.
    Wired from the UI ⋯ menu path — do not live-test against real threads
    without an explicit ask.
    """
    tid = _norm_thread_id(conversation_id)
    if not tid:
        return app_error("conversation_id required", code="BadRequest")
    return await _msg_action(f"liDeleteConversation({json.dumps(tid)})")


@returns("message[]")
@provides("chats", account_param="account")
@timeout(90)
async def list_messages(*, conversation_id=None, limit=100, **params):
    """List messages in one LinkedIn conversation (newest first).

    `conversation_id` = thread slug from `list_conversations` (or a full
    `urn:li:msg_conversation:(…)` / messaging URL).
    """
    guard = await _require_session()
    if guard:
        return guard
    if not conversation_id:
        return app_error("conversation_id required", code="BadRequest")
    await _ensure_messaging(force=False)
    out = await _eval(
        "(async () => {\n"
        + _msg_helpers()
        + f"\n  return await liFetchMessages({json.dumps(str(conversation_id))}, {int(limit)});\n"
        + "})()",
        timeout_s=75,
    )
    if isinstance(out, dict) and out.get("__error"):
        return app_error(
            f"LinkedIn messages unavailable: {out.get('what') or out['__error']}",
            code="NotReady",
        )
    if not isinstance(out, dict):
        return []
    rows = out.get("messages") if isinstance(out.get("messages"), list) else []
    return rows


_WATCH_HOOK = r"""
(function () {
  if (window.__agentos_li_watch__) return;
  window.__agentos_li_watch__ = true;
  const MARKER = __MARKER__;
  const Q_CONVERSATIONS = __Q_CONVERSATIONS__;
  const Q_MESSAGES = __Q_MESSAGES__;
  const __seen = new Set();
  const __armedAt = Date.now();

  __HELPERS__

  const emitFromJson = (j) => {
    try {
      const included = (j && j.included) || [];
      if (!included.length) return;
      const byUrn = indexIncluded(included);
      // Resolve mailbox lazily from any conversation/message urn in the payload.
      let mailbox = null;
      for (const x of included) {
        const urn = (x && x.entityUrn) || '';
        const mm = String(urn).match(/msg_(?:conversation|message):\((urn:li:fsd_profile:[^,]+),/);
        if (mm) { mailbox = mm[1]; break; }
      }
      for (const m of included) {
        if (!m || m.$type !== 'com.linkedin.messenger.Message') continue;
        const mid = m.entityUrn || m.backendUrn;
        if (!mid || __seen.has(mid)) continue;
        const ts = Number(m.deliveredAt);
        if (Number.isFinite(ts) && ts < __armedAt - 30000) { __seen.add(mid); continue; }
        __seen.add(mid);
        const entity = mapMsg(m, byUrn, mailbox);
        if (!entity) continue;
        entity.__shape__ = 'message';
        console.log(MARKER + JSON.stringify(entity));
      }
    } catch (e) {}
  };

  const wrapFetch = () => {
    if (window.fetch.__agentosLiWatch) return;
    const orig = window.fetch.bind(window);
    window.fetch = async function () {
      const res = await orig.apply(this, arguments);
      try {
        const req = arguments[0];
        const url = typeof req === 'string' ? req : (req && req.url) || '';
        if (/voyagerMessagingGraphQL|voyagerMessagingDash|realtimeFrontend/i.test(String(url))) {
          res.clone().json().then(emitFromJson).catch(function () {});
        }
      } catch (e) {}
      return res;
    };
    window.fetch.__agentosLiWatch = true;
  };

  (async () => {
    wrapFetch();
    // Prime seen-set from current inbox so we don't replay warm history.
    try {
      const pack = await liFetchConversations(40, false);
      // liFetchConversations returns mapped rows — re-fetch raw for urns
      const mailbox = pack && pack.mailbox;
      if (mailbox) {
        const url = '/voyager/api/voyagerMessagingGraphQL/graphql?queryId='
          + Q_CONVERSATIONS + '&variables=(mailboxUrn:' + encLiUrn(mailbox) + ')';
        const r = await fetch(url, { headers: liHeaders(), credentials: 'include' });
        const j = await r.json();
        for (const x of (j.included || [])) {
          if (x && x.$type === 'com.linkedin.messenger.Message' && x.entityUrn) {
            __seen.add(x.entityUrn);
          }
        }
      }
    } catch (e) {}
  })();
})()
"""


@returns({"watching": "boolean", "stream": "string"})
@provides("message_watch", account_param="account")
@timeout(60)
async def watch(**params):
    """Stream new LinkedIn messages into the graph in real time.

    Installs a durable `fetch` wrap on the messaging tab; each new
    `com.linkedin.messenger.Message` in a Voyager Messaging GraphQL (or
    realtime) response lands as a `message` entity via the CDP console
    marker — same pipeline as WhatsApp / Instagram.
    """
    guard = await _require_session()
    if guard:
        return guard
    await _ensure_messaging(force=False)
    hook = (
        _WATCH_HOOK
        .replace("__MARKER__", json.dumps(_WATCH_MARKER))
        .replace("__Q_CONVERSATIONS__", json.dumps(_Q_CONVERSATIONS))
        .replace("__Q_MESSAGES__", json.dumps(_Q_MESSAGES))
        .replace("__HELPERS__", _msg_helpers())
    )
    await services.call("browser_session", verb="subscribe", params={
        "target": _PAGE,
        "js": hook,
        "marker": _WATCH_MARKER,
        "subscriber": "linkedin",
        "op": "watch",
    })
    return {"watching": True, "stream": "message"}


def _resolve_send_target(to: str) -> dict:
    """Classify `to` as an existing thread slug or a profile (compose).

    Thread: `2-…` messaging slug / msg_conversation URN / /messaging/thread/…
    Profile: `ACo…`, `urn:li:fsd_profile:…`, `/in/{handle}`, bare handle.
    """
    raw = str(to or "").strip()
    if not raw:
        return {"kind": "empty"}
    m = re.search(r"msg_conversation:\([^,]+,([^)]+)\)", raw)
    if m:
        return {"kind": "thread", "id": m.group(1)}
    m = re.search(r"/messaging/thread/([^/?#]+)", raw)
    if m:
        return {"kind": "thread", "id": m.group(1)}
    # Existing thread slugs are base64-ish and start with `2-`.
    if re.match(r"^2-[A-Za-z0-9_=-]+$", raw):
        return {"kind": "thread", "id": raw}
    m = re.search(r"urn:li:fsd_profile:([A-Za-z0-9_-]+)", raw)
    if m:
        return {"kind": "profile", "id": m.group(1)}
    m = re.search(r"linkedin\.com/in/([^/?#]+)", raw)
    if m:
        return {"kind": "profile", "handle": url.decode(m.group(1))}
    if re.match(r"^ACo[A-Za-z0-9_-]+$", raw):
        return {"kind": "profile", "id": raw}
    # Bare publicIdentifier handle (no spaces).
    if re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,100}$", raw) and " " not in raw:
        return {"kind": "profile", "handle": raw.lstrip("@")}
    return {"kind": "unknown", "raw": raw}


async def _composer_type_and_send(nav: dict | None, body: str) -> dict | None:
    """Drive LinkedIn's message box from a navigate snapshot or DOM fallback.

    Returns None on success, or an `__error` dict.
    """
    tree = nav.get("snapshot", {}).get("tree", []) if isinstance(nav, dict) else []
    box = next(
        (
            el for el in tree
            if el.get("ref") and (
                el.get("role") in ("textbox", "searchbox")
                or "message" in str(el.get("name") or "").lower()
            )
        ),
        None,
    )
    if box:
        await services.call("browser_session", verb="type", params={
            "target": _PAGE, "ref": box["ref"], "text": body, "clear": True,
        })
        send_btn = next(
            (
                el for el in tree
                if el.get("ref") and el.get("role") == "button"
                and "send" in str(el.get("name") or "").lower()
            ),
            None,
        )
        if send_btn:
            await services.call("browser_session", verb="click", params={
                "target": _PAGE, "ref": send_btn["ref"],
            })
        else:
            await services.call("browser_session", verb="key", params={
                "target": _PAGE, "keys": "Enter",
            })
        return None
    typed = await _eval(
        f"""
(async () => {{
  const text = {json.dumps(body)};
  const box = document.querySelector(
    '.msg-form__contenteditable, [contenteditable="true"].msg-form__contenteditable, '
    + '.msg-form__message-texteditor [contenteditable="true"], '
    + '.msg-form [contenteditable="true"]'
  );
  if (!box) return {{ __error: 'no_composer' }};
  box.focus();
  try {{
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, text);
  }} catch (e) {{
    box.textContent = text;
    box.dispatchEvent(new InputEvent('input', {{ bubbles: true, data: text, inputType: 'insertText' }}));
  }}
  await new Promise((r) => setTimeout(r, 300));
  const btn = document.querySelector(
    'button.msg-form__send-button:not([disabled]), .msg-form__send-button:not([disabled]), '
    + 'button.msg-form__send-btn:not([disabled])'
  );
  if (btn) {{ btn.click(); return {{ ok: true, via: 'button' }}; }}
  box.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', code: 'Enter', bubbles: true }}));
  return {{ ok: true, via: 'enter' }};
}})()
""",
        timeout_s=30,
    )
    if isinstance(typed, dict) and typed.get("__error"):
        return typed
    return None


@returns("message")
@provides("message_send", account_param="account")
@timeout(90)
async def send_message(*, to, text, **params):
    """Send a LinkedIn DM via the messaging composer UI.

    `to` accepts:
      - existing thread slug (`2-…`) / messaging URL / msg_conversation URN
      - profile `ACo…` / `urn:li:fsd_profile:…` / `/in/{handle}` / bare handle
        → opens compose-to-profile (same path as LinkedIn's New message)

    Receipt is the newest matching outgoing GraphQL message — not keypress-ok.
    """
    guard = await _require_session()
    if guard:
        return guard
    body = str(text or "")
    if not body.strip():
        return app_error("`text` required", code="BadRequest")
    target = _resolve_send_target(str(to or ""))
    if target.get("kind") == "empty":
        return app_error("`to` (thread id or profile) required", code="BadRequest")
    if target.get("kind") == "unknown":
        return app_error(
            f"Unrecognized `to` {to!r} — pass a thread slug (`2-…`) or "
            "profile id/handle (`ACo…` / ryan-humphries7).",
            code="BadRequest",
        )

    thread: str | None = None
    if target["kind"] == "thread":
        thread = target["id"]
        nav_url = f"https://www.linkedin.com/messaging/thread/{thread}/"
    else:
        # Compose-to-profile. Prefer fsd_profile URN; resolve handle → urn via
        # identity dash when only a publicIdentifier is given.
        profile_id = target.get("id")
        handle = target.get("handle")
        if not profile_id and handle:
            await _ensure_messaging(force=False)
            resolved = await _eval(
                f"""
(async () => {{
  const csrf = (document.cookie.match(/JSESSIONID="?([^";]+)/) || [])[1] || '';
  const headers = {{
    'csrf-token': csrf,
    accept: 'application/vnd.linkedin.normalized+json+2.1',
    'x-restli-protocol-version': '2.0.0',
  }};
  const handle = {json.dumps(handle)};
  const r = await fetch(
    '/voyager/api/identity/dash/profiles?q=memberIdentity&memberIdentity='
      + encodeURIComponent(handle)
      + '&decorationId=com.linkedin.voyager.dash.deco.identity.profile.WebTopCardCore-9',
    {{ headers, credentials: 'include' }}
  );
  if (!r.ok) return {{ __error: 'profile_http', what: String(r.status) }};
  const j = await r.json();
  const el = ((j.data && j.data['*elements']) || [])[0] || null;
  const m = String(el || '').match(/fsd_profile:([A-Za-z0-9_-]+)/);
  if (m) return {{ id: m[1] }};
  const incl = (j.included || []).find((x) => x && x.publicIdentifier === handle);
  const m2 = incl && String(incl.entityUrn || '').match(/fsd_profile:([A-Za-z0-9_-]+)/);
  return m2 ? {{ id: m2[1] }} : {{ __error: 'not_found', what: handle }};
}})()
""",
                timeout_s=45,
            )
            if isinstance(resolved, dict) and resolved.get("id"):
                profile_id = resolved["id"]
            else:
                return app_error(
                    f"LinkedIn profile not found for handle {handle!r}",
                    code="NotFound",
                )
        urn = f"urn:li:fsd_profile:{profile_id}"
        nav_url = (
            "https://www.linkedin.com/messaging/compose/?recipient="
            + url.encode(urn)
        )

    nav = await services.call("browser_session", verb="navigate", params={
        "target": _PAGE,
        "url": nav_url,
        "timeout": 45,
    })
    err = await _composer_type_and_send(nav if isinstance(nav, dict) else None, body)
    if err:
        return app_error(
            f"No LinkedIn composer for {to}",
            code="NotFound",
        )

    # Compose lands on /messaging/thread/{id}/ after send — capture it.
    if not thread:
        landed = await _eval(
            """
(async () => {
  for (let i = 0; i < 25; i++) {
    const m = location.pathname.match(/\\/messaging\\/thread\\/([^/]+)/);
    if (m) return { threadId: decodeURIComponent(m[1]) };
    await new Promise((r) => setTimeout(r, 400));
  }
  return { __error: 'no_thread', what: location.href };
})()
""",
            timeout_s=20,
        )
        if isinstance(landed, dict) and landed.get("threadId"):
            thread = landed["threadId"]
        else:
            return app_error(
                "LinkedIn send may have worked but thread URL never appeared",
                code="NotReady",
            )

    receipt = await _eval(
        "(async () => {\n"
        + _msg_helpers()
        + f"""
  const want = {json.dumps(body)}.trim();
  const tid = {json.dumps(thread)};
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {{
    const pack = await liFetchMessages(tid, 20);
    if (pack && pack.messages) {{
      const hit = pack.messages.find((m) => m && m.isOutgoing && String(m.content || '').trim() === want);
      if (hit) return hit;
    }}
    await new Promise((r) => setTimeout(r, 800));
  }}
  return {{ __error: 'no_receipt', what: 'sent message not visible in GraphQL yet' }};
}})()
""",
        timeout_s=45,
    )
    if isinstance(receipt, dict) and receipt.get("__error"):
        return app_error(
            f"LinkedIn send may have failed: {receipt.get('what') or receipt['__error']}",
            code="NotReady",
        )
    return receipt
