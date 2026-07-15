# gmail_re.js

Ad-hoc Gmail Web RE helpers. Inject with toolkit:

```
js = open(toolkit.js) + open(gmail_re.js) + body
browser_session eval target=mail.google.com mode=background
```

`window.__gre.help()` lists methods.

## Two surfaces

| Surface | When | Where |
|---|---|---|
| **`window.__agmail`** | Shipping / live ops | Injected by every `gmail-cdp` op via `_LIB` in `gmail_cdp.py` |
| **`window.__gre`** | Ad-hoc RE / probes | This file (+ toolkit `__re`) |

Prefer `__agmail` for anything that already ships. Use `__gre` when you need
discovery without waiting on a Python op, or helpers that should not bloat `_LIB`.

## `__agmail` inventory (durable — keep in sync with `_LIB`)

```
wrapJln · openThread · reply · composeSend · resolveOmToken
listFilters · createFilter · deleteFilter
listLabelsSettings · createLabel · deleteLabel
listSendAs · getVacation · setVacation
__filtSet · __filtClickVisible · __filtSleep   (shared Settings UI helpers)
```

Plugin tools that thin-wrap those: `list_filters` / `create_filter` /
`delete_filter` · `create_label` / `delete_label` · `list_send_as` ·
`get_vacation` / `set_vacation` · plus reads/compose/mutations (see
`requirements.md` / `readme.md`).

## Settings UI pattern (filters / labels / send-as / vacation)

Gmail has **no hand-forgeable sync action** for filters yet — `#settings/*` is
the production path. Create also posts `/sync/st/s` (opaque settings token) +
`/sync/i/s` action `2`; delete posts `/sync/i/s` action `1`. Forging `i/s`
alone 200s but does not persist. See `requirements.md` §9 + `__gre.filterSyncCaps()`.

Recipe used everywhere:

1. `_nav` → `#settings/filters` | `labels` | `accounts` | `general`
2. Page JS drives dialogs with **native value setter** (`Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set`) + `input`/`change` — React/Material ignore plain `.value=`
3. Click visible *enabled* buttons by text (`__filtClickVisible(..., {requireEnabled:true})`)
4. Confirm dialogs: filters use **OK**; labels use **Delete** (not OK)
5. TrustedHTML: **never** `innerHTML=` on Gmail contenteditables — use
   `document.execCommand('insertText')` (vacation body)
6. Sensitive reauth ("We need to verify it's you") → `reauth_required` /
   `NeedsAuth` — `browser.login_window` on the bg profile, then retry

### Filters (CRD — no update tool)

| Tool | Notes |
|---|---|
| `list_filters` | Scrape `Matches:` / `Do this:` + delete-link id `#z…*…` |
| `create_filter` | Two-step dialog. Criteria: `from_addr`/`to`/`subject`/`query`/`has_attachment`. Actions: `add_labels` / `remove_labels` (`INBOX`→skip inbox, `UNREAD`→mark read) / `forward_to` |
| `delete_filter` | By id from list |

**Sacred filter on efisio:** `from:(modernist.club)` → Forward to
`joe@contini.co` (`z0000001680819884557*4072220868256049026`). Do **not**
delete/edit without asking Joe. Validate only with throwaway `AOS-FILTER-*`
subjects.

No `update_filter` — delete + recreate.

### Filter sync RE (sealed path — in progress)

| Piece | Status |
|---|---|
| UI CRD via `__agmail.*Filter` | ✅ shipping |
| Wire: `/sync/st/s` (field **522465311**) + `/sync/i/s` | ✅ captured; forge-alone no-ops |
| Message type `_m.snd` | ✅ ctor reachable; schema `gK[522465311]=tnd` (sealed `gK`) |
| Writer **`_m.V1k.prototype.E0b`** | ✅ **found** — builds `new _.snd`, fills fields, `c.call(b, _.jtd, _.nq(a))` |
| Live `V1k` instance → `browser.call` E0b | ⏳ next — pause/`captureBase` → craft `a` → call |

```js
// loaded_3 — the durable settings write for this filter field:
_m.V1k.prototype.E0b = function(a){
  var b=this.ha, c=b.oa, d=((0,_.jv)(), _.jtd);
  var e=new _.snd;
  e=_.wt(e,1, _.OOj(_.eu(a,1)));
  e=_.I(e,2, _.F(a,2));
  e=_.I(e,3, _.F(a,3));
  a=e.Axa(_.F(a,4));
  c.call(b, d, _.nq(a));
};
```

Reachable helpers on `_m`: `snd`, `nq`, `vq`, `OOj`, `eu`, `wt`, `jv`, `jtd` (obj).
Sealed / not on `_m`: `uvm`, `Lqi`/`Tqi`/`Vqi`, `xRb`, `gK`, `tnd` (serializer stack only).
Protocol strings: `_m.GHl="getFiltersList"`, `_m.sEm="Error creating filter"`, `_m.WHa="create-filter/"`.

### Cracking sealed Gmail JS (CDP — do this)

Gmail has **no webpack registry**. Page `eval` cannot name sealed locals.
The engine's Debugger verbs can. **Prefer these over forging `/sync/*` bodies.**

| Goal | Verb / helper |
|---|---|
| Break on a stack `url:line` with no fn handle | `browser.breakpoint` `{urlRegex, line}` (`line` = 1-based V8 stack) · or `gbreak url 'cb=loaded_3' 4579` |
| Logpoint (never pause; count/capture) | same + `condition: '(window.__n=(window.__n\|\|0)+1,false)'` |
| Pause once, dump `this` + scopeChain objectIds, resume | `waitPause:true` + `triggerJs:'(async()=>__agmail.createFilter(...))()'` · or `gbreak wait expr '_m.V1k.prototype.E0b' --trigger '…'` |
| Read whole bundle text | `browser.script_source` `{url:'loaded_3'}` · or `gbreak src loaded_3` |
| Call / inspect a paused handle | `browser.call` / `browser.inspect` / `browser.source` on the returned `objectId` |
| Closure walk | `browser.scopes` — skips Global + huge `_`/`window`; on hang returns a soft error (session stays alive). Prefer `waitPause` local scopes for Gmail. |

**Filter writer recipe (verified):**
1. Seed `__agmail` via any `gmail-cdp` op (`list_filters`).
2. `gbreak wait expr '_m.V1k.prototype.E0b' --trigger '(async()=>{const c=await __agmail.createFilter({subject:"AOS-FILTER-X",removeLabels:["INBOX"]});if(c&&c.id)await __agmail.deleteFilter(c.id)})()'`
3. From `paused.callFrames[0]`: `this` = live V1k instance; `scopeChain` local = args/`snd` builders.
4. `inspect` those objectIds → craft `a` → `browser.call` `E0b` on `this`.
5. Leave **no** `AOS-FILTER-*` leftovers; never touch the sacred modernist filter.

Also useful: `__re.spy(_m.V1k.prototype,'E0b')` / `__re.captureBase(_m,'V1k').take()` (page-JS; `.take()` not an array).
`window._` is **not** in eval — closed over by `_m.*` only.

### Labels

User `id` === display name (nav scrape; no OAuth `Label_<n>`). Throwaway
`AOS-LABEL-*` only.

### Vacation

`get_vacation` first; any `set_vacation` test must **restore** previous state.
Flip OFF **after** editing fields so Save persists off.

## Driver helpers (`probes/`)

| Script | Use |
|---|---|
| `gnav <url>` | Navigate bg mail tab |
| `geval '<js…return>'` | Eval with toolkit prepended (`FRESH=1` reload; `GRE=1` also injects `gmail_re.js`) |
| `greval '…'` | Bare eval (no toolkit) |
| `gprobe` / `gcall` / `ginspect` / `gsource` / `gscopes` / `gbreak` | Toolkit/CDP deep probes (`gbreak` = breakpoint by expr/url + waitPause + script_source) |
| `cleanup-aos.py` | Trash leftover AOS-* test mail |
| `probe-filter-stack.js` / `probe-filter-json.js` / `probe-filter-seal-stack.js` | Filter sync stack / JSON.stringify / line:col captures |

`toolkit=commons/re/toolkit.js` · `gre=gmail-cdp/dev/gmail_re.js` (`__v6`).

## Gotchas (hard-won)

- No `//` in one-line / concatenated eval payloads
- `location.href = …` in eval kills context — use `navigate` / `location.hash`
- Bg Brave wedge → `pkill` browsers-bg/brave
- Never wrap `_m.EQn` twice (`__gre.eqnClean()`)
- Moles open → compose/reply wedges; cross-doc reload clears
- `view=om` needs **message** hex (`list.messageHex` / `msg[55]`), not thread hex on many self-sents; hex `id` is tried as om token first
- Plugin needs `http` service for `unsubscribe_email` one-click POST
- `_eval` maps payload `__error` → `app_error` — callers must `_is_err(value)`, not re-check `__error`
- Mutations/sends are **serial** on the one bg tab
- Filter create may trip Google **verify-it's-you** (covers Create button) —
  `reauth_required` / `NeedsAuth`; `browser.login_window` on bg profile
- `anyAction` must ignore Settings IMAP checkboxes (Starred/Important/Spam) —
  only visible filter-dialog action rows count
- Hand-forged `/sync/i/s` filter delete/create 200s but does not persist —
  durable write is `/sync/st/s` with opaque token

## Orient every session

```
agentos sup (cwd=gmail-cdp) OR plugins.load({app:"gmail-cdp"})
engine up → gmail-cdp.list_accounts → efisio@gmail.com
read this file ("Cracking sealed Gmail JS") + requirements.md §9
```

Sealed JS / filter writer work: start at **Cracking sealed Gmail JS** above —
`gbreak` / `browser.breakpoint` `waitPause`, not forged sync bodies. Sacred
modernist filter: do not touch.