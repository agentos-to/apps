"""macOS Contacts app — read the system address book as `person` records.

Reads contacts through the **Contacts framework** (`CNContactStore`) via an
embedded Swift helper run with `shell.run("swift", ...)`. The durable, sync-
surviving identity is the vCard **UID** — obtained by serializing each contact
with `CNContactVCardSerialization` and reading its `UID:` line.

Why not the easy reads: `CNContact.identifier` and the AddressBook sqlite
`ZUNIQUEID` are both **device-local** — they change on iCloud sync, so a re-sync
would mint duplicates. The vCard UID is the only key that survives sync *and*
the engine's person-merge, so it's what we emit as the `icloud` identity.

Each tool returns `person`-shaped dicts carrying an `identities` array — the
uniform identity set the engine dedups on. We emit each `id` already canonical
(email lowercased, phone → E.164) and never dedup ourselves: the agent upserts
what we return via `data.create(person, ...)` and the engine merges on any
`(platform, id)` collision. Social profiles from the address book give only a
mutable username (no stable platform id), so they ride as **handle-only**
entries — the engine never indexes those, which is correct: a bare @handle is
not a reliable identity.

Requires Contacts access (TCC) granted to the AgentOS engine — see readme.md.
"""

import json

from agentos import returns, shell, test, timeout, app_error
from agentos.identity import normalize_email, normalize_phone


# The Swift helper. Reads {query?, limit?} as JSON on stdin; emits a JSON array
# of contact dicts on stdout. Exits 2 with "PERMISSION_DENIED" on stderr when
# Contacts access isn't authorized. UID comes from the serialized vCard, never
# from the device-local CNContact.identifier.
_SWIFT = r"""
import Contacts
import Foundation

var query: String? = nil
var limit = 200
if let inData = try? FileHandle.standardInput.readToEnd(),
   let obj = (try? JSONSerialization.jsonObject(with: inData)) as? [String: Any] {
    query = obj["query"] as? String
    if let l = obj["limit"] as? Int { limit = l }
    else if let ls = obj["limit"] as? String, let l = Int(ls) { limit = l }
}
if let q = query, q.trimmingCharacters(in: .whitespaces).isEmpty { query = nil }

func statusName(_ s: CNAuthorizationStatus) -> String {
    switch s {
    case .notDetermined: return "notDetermined"
    case .restricted: return "restricted"
    case .denied: return "denied"
    case .authorized: return "authorized"
    @unknown default: return "unknown"
    }
}

let store = CNContactStore()
let before = statusName(CNContactStore.authorizationStatus(for: .contacts))
let sem = DispatchSemaphore(value: 0)
var granted = false
var authErr: Error? = nil
store.requestAccess(for: .contacts) { ok, err in granted = ok; authErr = err; sem.signal() }
sem.wait()
guard granted else {
    let detail = authErr?.localizedDescription ?? "Contacts access not authorized"
    FileHandle.standardError.write(
        ("PERMISSION_DENIED: status=" + before + " — " + detail + "\n").data(using: .utf8)!)
    exit(2)
}

let keys: [CNKeyDescriptor] = ([
    CNContactGivenNameKey, CNContactFamilyNameKey, CNContactMiddleNameKey,
    CNContactNamePrefixKey, CNContactNameSuffixKey, CNContactNicknameKey,
    CNContactOrganizationNameKey, CNContactEmailAddressesKey,
    CNContactPhoneNumbersKey, CNContactUrlAddressesKey,
    CNContactSocialProfilesKey, CNContactInstantMessageAddressesKey,
] as [String]).map { $0 as CNKeyDescriptor } + [CNContactVCardSerialization.descriptorForRequiredKeys()]

func uid(of c: CNContact) -> String {
    guard let data = try? CNContactVCardSerialization.data(with: [c]),
          let vcard = String(data: data, encoding: .utf8) else { return c.identifier }
    for raw in vcard.split(whereSeparator: { $0 == "\n" || $0 == "\r" }) {
        let line = String(raw)
        if line.uppercased().hasPrefix("UID"),
           let colon = line.firstIndex(of: ":") {
            var v = String(line[line.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
            let urn = "urn:uuid:"
            if v.lowercased().hasPrefix(urn) { v = String(v.dropFirst(urn.count)) }
            if !v.isEmpty { return v }
        }
    }
    return c.identifier
}

func dict(_ c: CNContact) -> [String: Any] {
    var d: [String: Any] = ["uid": uid(of: c)]
    d["givenName"] = c.givenName
    d["familyName"] = c.familyName
    d["middleName"] = c.middleName
    d["namePrefix"] = c.namePrefix
    d["nameSuffix"] = c.nameSuffix
    d["nickname"] = c.nickname
    d["organizationName"] = c.organizationName
    d["emails"] = c.emailAddresses.map { String($0.value) }
    d["phones"] = c.phoneNumbers.map { $0.value.stringValue }
    d["urls"] = c.urlAddresses.map { String($0.value) }
    d["social"] = c.socialProfiles.map { p in
        ["service": p.value.service, "username": p.value.username, "url": p.value.urlString]
    }
    d["im"] = c.instantMessageAddresses.map { p in
        ["service": p.value.service, "username": p.value.username]
    }
    return d
}

var out: [[String: Any]] = []
if let q = query {
    let pred = CNContact.predicateForContacts(matchingName: q)
    if let matches = try? store.unifiedContacts(matching: pred, keysToFetch: keys) {
        for c in matches.prefix(limit) { out.append(dict(c)) }
    }
} else {
    let req = CNContactFetchRequest(keysToFetch: keys)
    var n = 0
    try store.enumerateContacts(with: req) { c, stop in
        out.append(dict(c)); n += 1
        if n >= limit { stop.pointee = true }
    }
}
let data = try JSONSerialization.data(withJSONObject: out, options: [])
FileHandle.standardOutput.write(data)
"""


# CNSocialProfile / CNInstantMessageAddress service → identity platform slug.
# Twitter is canonicalized to "x" to match the platform registry; everything
# else lowercases and drops spaces. The address book hands us a username, never
# a stable platform id, so these always ride as handle-only entries.
def _slug(service: str | None) -> str | None:
    if not service:
        return None
    s = service.strip().lower().replace(" ", "")
    return {"twitter": "x", "googletalk": "googletalk"}.get(s, s) or None


def _dedup(identities: list[dict]) -> list[dict]:
    """Drop exact-duplicate identity entries, keyed on (platform, id|handle)."""
    seen = set()
    out = []
    for e in identities:
        key = (e.get("platform"), e.get("id") or f"@{e.get('handle')}")
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _person(c: dict, account: str, default_region: str) -> dict:
    """Map one Swift contact dict to a `person` record with an `identities` set."""
    uid = c["uid"]
    given = (c.get("givenName") or "").strip()
    family = (c.get("familyName") or "").strip()
    middle = (c.get("middleName") or "").strip()
    nickname = (c.get("nickname") or "").strip()
    org = (c.get("organizationName") or "").strip()
    name = (" ".join(p for p in (given, middle, family) if p).strip()
            or nickname or org or uid)

    # The icloud entry is the durable source FK; email/phone are the join keys.
    identities = [{"platform": "icloud", "id": uid, "account": account}]
    for raw in c.get("emails", []):
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            identities.append({"platform": "email", "id": normalize_email(raw)})
        except ValueError:
            pass  # not a parseable address — skip rather than emit a bad key
    for raw in c.get("phones", []):
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            identities.append({"platform": "phone", "id": normalize_phone(raw, default_region)})
        except ValueError:
            pass  # ambiguous without a country code — skip; a bad key is worse
    for entry in list(c.get("social", [])) + list(c.get("im", [])):
        slug = _slug(entry.get("service"))
        handle = (entry.get("username") or "").strip()
        if slug and handle:
            identities.append({"platform": slug, "handle": handle})

    person = {"id": uid, "name": name, "identities": _dedup(identities)}
    if given:
        person["givenName"] = given
    if family:
        person["familyName"] = family
    if middle:
        person["additionalName"] = middle
    if c.get("namePrefix"):
        person["honorificPrefix"] = c["namePrefix"]
    if c.get("nameSuffix"):
        person["honorificSuffix"] = c["nameSuffix"]
    if nickname:
        person["nickname"] = nickname
    urls = [u for u in (c.get("urls") or []) if u]
    if urls:
        person["url"] = urls[0]
    return person


async def _fetch(req: dict, account: str, default_region: str) -> list[dict]:
    result = await shell.run("swift", args=["-e", _SWIFT], input=json.dumps(req))
    if result["exit_code"] != 0:
        stderr = (result.get("stderr") or "").strip()
        if "PERMISSION_DENIED" in stderr:
            detail = stderr.split("PERMISSION_DENIED:", 1)[-1].strip()
            return app_error(
                "Contacts access not granted to the AgentOS engine "
                f"({detail}). Grant it in System Settings → Privacy & Security "
                "→ Contacts, then retry.",
                code="NeedsPermission",
            )
        return app_error(f"Contacts read failed: {stderr or 'unknown error'}")
    stdout = (result.get("stdout") or "").strip()
    contacts = json.loads(stdout) if stdout else []
    return [_person(c, account, default_region) for c in contacts]


@test(params={"limit": 3})
@returns("person[]")
@timeout(60)
async def list_contacts(*, limit: int = 200, account: str = "icloud",
                        default_region: str = "US", **params) -> list[dict]:
    """List address-book contacts as `person` records with their identity sets.

    Args:
        limit: Max contacts to return (the book can hold thousands).
        account: Which book the icloud identity belongs to — the iCloud account
            email, or "icloud" / "local" when unknown.
        default_region: ISO-3166 region for parsing phone numbers that lack a
            country code (e.g. "US"). Unparseable numbers are skipped.
    """
    return await _fetch({"limit": limit}, account, default_region)


@test(params={"query": "a", "limit": 3})
@returns("person[]")
@timeout(60)
async def search_contacts(*, query: str, limit: int = 50, account: str = "icloud",
                         default_region: str = "US", **params) -> list[dict]:
    """Search the address book by name, returning matching `person` records.

    Matches given / family / nickname / organization (CNContact name predicate).
    Use this for the on-demand flow — "add Conor" → search → upsert one.

    Args:
        query: Name fragment to match.
        limit: Max results to return.
        account: Which book the icloud identity belongs to — the iCloud account
            email, or "icloud" / "local" when unknown.
        default_region: ISO-3166 region for parsing phones without a country code.
    """
    return await _fetch({"query": query, "limit": limit}, account, default_region)
