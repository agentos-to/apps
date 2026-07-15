"""Local Network — a device-realm transport volume served live.

The discovered LAN IS a volume: this app `@provides("volume_transport")`, so the
engine files it under the **Local Network** container (kind `device` → that
container, per `sources/containers.rs`) and dispatches every browse here live —
no `.db`, nothing to go stale. Each Wi-Fi network is a volume; its devices are
the entries, typed `device` so they wear per-kind faces and the agent reads the
same nodes over `data`.

Discovery is unprivileged + Apple-native: provoke the ARP/NDP neighbor table,
resolve DHCP hostnames via the system resolver, and map each MAC to its maker
from the offline IEEE OUI database. Results cache briefly so browsing is instant.
"""

import asyncio
import datetime
import os
import re

from agentos import shell, client, returns, provides, test, timeout

# Offline IEEE OUI prefix DB shipped with nmap (Homebrew on Apple Silicon / Intel).
_OUI_PATHS = [
    "/opt/homebrew/share/nmap/nmap-mac-prefixes",
    "/usr/local/share/nmap/nmap-mac-prefixes",
    "/usr/share/nmap/nmap-mac-prefixes",
]
_OUI: dict[str, str] | None = None

# Module-level cache (workers are long-lived) keyed by network id (gateway MAC).
_CACHE: dict[str, dict] = {"nets": {}, "devices": {}, "ts": {}}
_FRESH_SECONDS = 45


def _load_oui() -> dict[str, str]:
    global _OUI
    if _OUI is not None:
        return _OUI
    _OUI = {}
    for path in _OUI_PATHS:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2 and len(parts[0]) == 6:
                    _OUI[parts[0].upper()] = parts[1]
        break
    return _OUI


def _normmac(mac: str) -> str:
    try:
        return ":".join(f"{int(o, 16):02x}" for o in mac.split(":"))
    except Exception:
        return mac.lower()


def _is_randomized(mac: str) -> bool:
    """Locally-administered bit (2nd-LSB of octet 1) ⇒ randomized/private MAC."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except Exception:
        return False


def _vendor_offline(mac: str) -> str:
    if _is_randomized(mac):
        return ""
    return _load_oui().get("".join(mac.split(":")[:3]).upper(), "")


async def _vendor_online(mac: str) -> str:
    """Keyless fallback — macvendors.com, used only when the offline DB misses."""
    try:
        resp = await client.get(f"https://api.macvendors.com/{mac}", timeout=4)
        body = (resp.get("body") or "").strip()
        if resp.get("status") == 200 and body and len(body) < 120 and "errors" not in body:
            return body
    except Exception:
        pass
    return ""


# ── device-maker brand ────────────────────────────────────────────────────────
#
# `vendor` (the OUI) names whoever made the *network chip*, which for embedded
# devices is a module maker, not the brand: a Tesla's Wi-Fi is "LG Innotek", a
# Brother printer's is "Cloud Network Technology" (Foxconn), a Dreame vacuum's
# is "FN-Link". No MAC API can fix this — they all read the same IEEE OUI
# registry. The real brand is what the device calls *itself* — its DHCP/mDNS
# hostname and model string ("tesla_model_3", "Brother HL-L3270CDW",
# "dreame_vacuum_p2009"). We mine that, and keep `vendor` as the honest silicon
# fact. (canonical org name, homepage domain, [match tokens]).
_BRANDS: list[tuple[str, str, list[str]]] = [
    ("Tesla, Inc.", "tesla.com", ["tesla", "model_3", "model_y", "model_s", "model_x"]),
    ("Brother Industries", "brother.com", ["brother", "brw", "hl-l", "mfc-", "dcp-"]),
    ("Dreame Technology", "dreametech.com", ["dreame"]),
    ("Roborock", "roborock.com", ["roborock"]),
    ("iRobot", "irobot.com", ["roomba", "irobot"]),
    ("Google LLC", "google.com", ["google", "googlehome", "nest", "chromecast"]),
    ("Roku, Inc.", "roku.com", ["roku"]),
    ("Nintendo", "nintendo.com", ["nintendo", "switch"]),
    ("Apple Inc.", "apple.com", ["apple", "iphone", "ipad", "macbook", "imac", "appletv", "homepod", "airpods"]),
    ("Amazon", "amazon.com", ["amazon", "echo", "alexa", "firetv", "fire-tv", "kindle", "eero"]),
    ("Sonos", "sonos.com", ["sonos"]),
    ("Samsung", "samsung.com", ["samsung", "galaxy"]),
    ("Sony", "sony.com", ["playstation", "ps5", "ps4", "bravia"]),
    ("Microsoft", "microsoft.com", ["xbox", "surface"]),
    ("HP Inc.", "hp.com", ["officejet", "laserjet", "deskjet", "envy", "hp-print"]),
    ("Canon", "canon.com", ["canon"]),
    ("Epson", "epson.com", ["epson"]),
    ("Ring", "ring.com", ["ring-", "ring_"]),
    ("Wyze", "wyze.com", ["wyze"]),
    ("Signify (Philips Hue)", "philips-hue.com", ["philips", "hue"]),
    ("LIFX", "lifx.com", ["lifx"]),
    ("ASUS", "asus.com", ["asus", "rt-ax", "rt-ac", "gs-ax"]),
    ("NETGEAR", "netgear.com", ["netgear", "orbi"]),
    ("TP-Link", "tp-link.com", ["tp-link", "tplink", "kasa"]),
    ("Ubiquiti", "ui.com", ["ubiquiti", "unifi"]),
    ("Raspberry Pi", "raspberrypi.com", ["raspberry", "raspberrypi"]),
]


def _brand(*signals: str) -> tuple[str, str]:
    """Device-maker brand from the device's self-advertisement (hostname, mDNS
    name, model, and the OUI vendor as a weak hint) → (name, domain), else ("","")."""
    blob = " ".join(s for s in signals if s).lower()
    for name, domain, tokens in _BRANDS:
        if any(t in blob for t in tokens):
            return name, domain
    return "", ""


# ── network identity ─────────────────────────────────────────────────────────

def _cidr(myip: str, hexmask: str) -> str:
    try:
        mask = int(hexmask, 16)
        prefix = bin(mask).count("1")
        octets = [int(x) for x in myip.split(".")]
        mbytes = [(mask >> (24 - 8 * i)) & 0xFF for i in range(4)]
        base = ".".join(str(octets[i] & mbytes[i]) for i in range(4))
        return f"{base}/{prefix}"
    except Exception:
        return ""


async def _identity() -> dict:
    g = (await shell.run("route", args=["-n", "get", "default"], timeout=5))["stdout"]
    gw = re.search(r"gateway:\s*(\S+)", g)
    iface = re.search(r"interface:\s*(\S+)", g)
    iface_name = iface.group(1) if iface else "en0"
    ic = (await shell.run("ifconfig", args=[iface_name], timeout=5))["stdout"]
    myip = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", ic)
    mask = re.search(r"netmask (0x[0-9a-fA-F]+)", ic)
    ether = re.search(r"\bether ([0-9a-f:]{17})", ic)
    ip = myip.group(1) if myip else None
    return {
        "gateway": gw.group(1) if gw else None,
        "iface": iface_name,
        "myip": ip,
        "mac": ether.group(1) if ether else None,   # this Mac's own L2 address (arp never lists self)
        "subnet": _cidr(ip, mask.group(1)) if (ip and mask) else "",
    }


async def _computer_name() -> str:
    """This Mac's user-facing name (System Settings → Sharing), else its hostname."""
    for binary, args in (("scutil", ["--get", "ComputerName"]), ("hostname", [])):
        try:
            out = (await shell.run(binary, args=args, timeout=3))["stdout"].strip()
            if out:
                return out.removesuffix(".local")
        except Exception:
            pass
    return ""


def _clean_ssid(s: str | None) -> str:
    # macOS Sequoia returns the literal "<redacted>" for the SSID unless the
    # process holds Location Services permission — treat that as "no SSID".
    s = (s or "").strip()
    return "" if s.lower() in ("", "<redacted>", "redacted", "<unknown>") else s


async def _ssid() -> str:
    out = (await shell.run("ipconfig", args=["getsummary", "en0"], timeout=4))["stdout"]
    m = re.search(r"\bSSID\s*:\s*(.+)", out)
    s = _clean_ssid(m.group(1) if m else "")
    if s:
        return s
    sp = (await shell.run("system_profiler", args=["SPAirPortDataType"], timeout=12))["stdout"]
    m = re.search(r"Current Network Information:\s*\n\s*(.+?):", sp)
    return _clean_ssid(m.group(1) if m else "")


async def _network_info() -> dict:
    """The current `network` — gateway IP+MAC (its volume identity), SSID, subnet."""
    ident = await _identity()
    gw = ident["gateway"]
    gwmac = ""
    if gw:
        try:
            await shell.run("ping", args=["-c", "1", "-W", "500", gw], timeout=2)
        except Exception:
            pass
        arp = (await shell.run("arp", args=["-an"], timeout=5))["stdout"]
        m = re.search(rf"\({re.escape(gw)}\) at ([0-9a-f:]+)", arp)
        if m:
            gwmac = _normmac(m.group(1))
    ssid = await _ssid()
    # SSID is redacted on modern macOS without a Location grant (see _clean_ssid),
    # so name the network after its router: the gateway's reverse-DNS hostname
    # (e.g. "gs-ax5400-06f8") else its IP. Identity stays the gateway MAC.
    gw_host = await _hostname(gw) if gw else ""
    name = ssid or gw_host or (f"Wi-Fi ({gw})" if gw else "Local Network")
    return {
        "id": gwmac or ident.get("subnet") or gw or "network",
        "name": name,
        "shape": "network",
        "ssid": ssid,
        "gatewayIp": gw,
        "gatewayMac": gwmac,
        "subnet": ident.get("subnet", ""),
    }


# ── L2 discovery ─────────────────────────────────────────────────────────────

async def _provoke(subnet: str) -> None:
    sem = asyncio.Semaphore(64)

    async def ping(i: int) -> None:
        async with sem:
            try:
                await shell.run("ping", args=["-c", "1", "-W", "500", f"{subnet}.{i}"], timeout=2)
            except Exception:
                pass

    await asyncio.gather(*[ping(i) for i in range(1, 255)])


async def _neighbors() -> dict[str, dict]:
    hosts: dict[str, dict] = {}
    arp = (await shell.run("arp", args=["-an"], timeout=5))["stdout"]
    for line in arp.splitlines():
        m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-f:]+)", line)
        if not m or "incomplete" in line:
            continue
        ip, mac = m.group(1), m.group(2)
        if ip.endswith(".255") or ip.endswith(".0"):
            continue
        if mac == "ff:ff:ff:ff:ff:ff" or mac.startswith(("1:0:5e", "01:00:5e")):
            continue
        hosts[ip] = {"ip": ip, "mac": _normmac(mac), "via": ["arp"]}

    v6map: dict[str, list[str]] = {}
    ndp = (await shell.run("ndp", args=["-an"], timeout=5))["stdout"]
    for line in ndp.splitlines():
        parts = line.split()
        if len(parts) >= 2 and ":" in parts[0] and re.fullmatch(r"[0-9a-f:]{11,}", parts[1] or ""):
            if parts[1].count(":") == 5:
                v6map.setdefault(_normmac(parts[1]), []).append(parts[0])
    for h in hosts.values():
        h["ipv6"] = v6map.get(h["mac"], [])
    return hosts


async def _hostname(ip: str) -> str:
    try:
        out = (await shell.run("dscacheutil", args=["-q", "host", "-a", "ip_address", ip], timeout=3))["stdout"]
        m = re.search(r"name:\s*(\S+)", out)
        return m.group(1) if m else ""
    except Exception:
        return ""


# ── mDNS / Bonjour — the richest name + capability source ─────────────────────
#
# Reverse-DNS gives at most a name; mDNS gives a *friendly* name ("Living Room",
# "rokuultra") and the device's advertised capabilities (airplay, cast, …). The
# only Apple-native tool is `dns-sd`, which streams forever — so every browse/
# resolve runs `shell.run(..., stream=True, timeout=N)`: the engine kills it at
# the deadline and hands back what it printed (the streaming-command contract).
# Guest networks suppress mDNS (client isolation) → this quietly yields nothing.

# (Bonjour service type, role token) — role "" means browse it for the name
# only (every device advertises these; they're not user-meaningful capabilities).
# Friendly-name types come first so they win the name over a generic one.
_MDNS_TYPES: list[tuple[str, str]] = [
    ("_airplay._tcp", "airplay"), ("_raop._tcp", "airplay"),
    ("_googlecast._tcp", "cast"), ("_spotify-connect._tcp", "spotify"),
    ("_sonos._tcp", "sonos"), ("_roku-rcp._tcp", "roku"),
    ("_printer._tcp", "printer"), ("_ipp._tcp", "printer"),
    ("_ipps._tcp", "printer"), ("_pdl-datastream._tcp", "printer"),
    ("_hap._tcp", "homekit"), ("_amzn-wplay._tcp", "amazon"),
    ("_miio._udp", "xiaomi"), ("_ssh._tcp", "ssh"),
    ("_smb._tcp", "smb"), ("_afpovertcp._tcp", "afp"),
    ("_device-info._tcp", ""), ("_companion-link._tcp", ""), ("_http._tcp", ""),
]

# A `dns-sd -B` row: "<time> Add <flags> <if> <domain> <servicetype> <instance…>"
_MDNS_BROWSE_RE = re.compile(r"^\S+\s+Add\s+\S+\s+\S+\s+\S+\s+\S+\s+(.+?)\s*$")
_MDNS_REACHED_RE = re.compile(r"can be reached at (\S+?):(\d+)")


async def _dns_sd(args: list[str], timeout: float) -> str:
    """One streaming `dns-sd` call → its partial stdout (it never self-exits)."""
    try:
        r = await shell.run("dns-sd", args=args, timeout=timeout, stream=True)
        return r.get("stdout") or ""
    except Exception:
        return ""


def _unescape_mdns(s: str) -> str:
    """Decode dns-sd label escapes (`\\032` → space, `\\.` → `.`) for display."""
    s = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), s)
    return re.sub(r"\\(.)", r"\1", s)


async def _mdns_map() -> dict[str, dict]:
    """Browse Bonjour and bind each responder to its IP → {name, roles, host}.

    Three streamed phases, each fanned out under one semaphore: browse every
    type for instances, resolve each instance to its SRV target host, forward-
    resolve that host to an IPv4. The IP is what lets us fold the result onto an
    ARP-discovered device.
    """
    sem = asyncio.Semaphore(16)

    async def browse(stype: str, role: str) -> list[tuple[str, str]]:
        async with sem:
            out = await _dns_sd(["-B", stype, "local."], timeout=2.5)
        names = []
        for line in out.splitlines():
            m = _MDNS_BROWSE_RE.match(line)
            if m and m.group(1).strip():
                names.append((m.group(1).strip(), role))
        return names

    browsed = await asyncio.gather(*[browse(t, r) for t, r in _MDNS_TYPES])
    # (raw instance, role, service type) — keep the service type for the resolve.
    instances = [
        (inst, role, stype)
        for (stype, _), names in zip(_MDNS_TYPES, browsed)
        for inst, role in names
    ]

    async def resolve(inst: str, role: str, stype: str) -> tuple[str, str, str]:
        async with sem:
            out = await _dns_sd(["-L", inst, stype, "local."], timeout=2.0)
        m = _MDNS_REACHED_RE.search(out)
        return inst, role, (m.group(1).rstrip(".") if m else "")

    resolved = await asyncio.gather(*[resolve(*x) for x in instances])

    hosts_needed = {host for _, _, host in resolved if host}

    async def host_ip(host: str) -> tuple[str, str]:
        async with sem:
            try:
                out = (await shell.run("dscacheutil", args=["-q", "host", "-a", "name", host], timeout=3))["stdout"]
            except Exception:
                return host, ""
        # A host's own name resolves to loopback + link-local *first* (127.0.0.1,
        # 169.254.*) — skip those for the real LAN address the ARP set is keyed by.
        ips = re.findall(r"ip_address:\s*(\d+\.\d+\.\d+\.\d+)", out)
        ip = next((x for x in ips if not x.startswith(("127.", "169.254."))), "")
        return host, ip

    host_to_ip = dict(await asyncio.gather(*[host_ip(h) for h in hosts_needed])) if hosts_needed else {}

    out: dict[str, dict] = {}
    for inst, role, host in resolved:
        ip = host_to_ip.get(host) if host else ""
        if not ip:
            continue
        e = out.setdefault(ip, {"name": "", "roles": set(), "host": host})
        if role:
            e["roles"].add(role)
        if not e["name"]:
            e["name"] = _unescape_mdns(inst)
    for e in out.values():
        e["roles"] = sorted(e["roles"])
    return out


_KIND_RULES = [
    ("router", ["asus", "gs-ax", "rt-ax", "rt-ac", "netgear", "tp-link", "tplink", "ubiquiti",
                "unifi", "router", "gateway", "eero", "orbi", "linksys"]),
    # Speaker BEFORE tv on purpose: a smart speaker (Google Home / Nest Mini,
    # HomePod, Echo, Sonos) advertises the *same* cast / airplay / raop roles a
    # streamer does, so a bare "cast"/"airplay" can't decide it — the brand or
    # hostname must win first. Otherwise a Google Home Mini (roles: [cast])
    # misreads as a TV. raop (AirPlay *audio*) is speaker-only, so HomePod-vs-
    # AppleTV still resolves.
    ("speaker", ["google-home", "googlehome", "google_home", "ghome", "nest-mini", "nest_mini",
                 "nest-audio", "nest_audio", "homepod", "echo", "alexa", "sonos", "speaker",
                 "raop", "spotify"]),
    # mDNS role tokens fold into the same blob: airplay/cast → a display (TV),
    # checked only after the speaker brands above have had their say.
    ("tv", ["roku", "appletv", "apple-tv", "firetv", "fire-tv", "shield", "chromecast",
            "bravia", "webos", "samsung-tv", "vizio", "-tv", "googletv", "google-tv",
            "airplay", "cast"]),
    ("car", ["tesla", "model_3", "model_y", "model_s", "model_x", "rivian", "lucid",
             "polestar", "ioniq", "mach-e", "taycan"]),
    ("console", ["switch", "nintendo", "playstation", "ps5", "ps4", "xbox"]),
    ("vacuum", ["vacuum", "dreame", "roborock", "roomba", "irobot"]),
    ("printer", ["brw", "brother", "officejet", "laserjet", "canon", "epson", "printer"]),
    ("bulb", ["lifx", "hue", "yeelight", "kasa", "bulb", "lightstrip"]),
    ("camera", ["camera", "wyze", "ring", "arlo"]),
    ("phone", ["iphone", "ipad", "android", "pixel", "galaxy", "phone"]),
    ("computer", ["macbook", "imac", "raspberry", "-personal", "workstation", "laptop", "desktop"]),
]


def _classify(host: dict, gateway: str | None) -> str:
    if host.get("self"):
        return "computer"
    if host.get("ip") == gateway:
        return "router"
    roles = " ".join(host.get("roles") or [])
    blob = f"{(host.get('hostname') or '').lower()} {(host.get('vendor') or '').lower()} {roles}"
    for kind, keys in _KIND_RULES:
        if any(k in blob for k in keys):
            return kind
    # printer is unambiguous from its own service; trust the role last.
    if "printer" in roles:
        return "printer"
    return "iot"


def _device_entry(h: dict, gateway: str | None) -> dict:
    """A transport entry typed `device` — wears a per-kind face, reads over `data`."""
    randomized = _is_randomized(h["mac"])
    is_gateway = h.get("ip") == gateway
    # Name precedence: mDNS friendly name > reverse-DNS/own hostname > vendor.
    # The gateway falls back to "Router" (the network volume is already named
    # after the router's hostname, so the device in the list reads cleanly).
    name = h.get("mdns_name") or h.get("hostname") or h.get("vendor")
    if not name:
        name = "Router" if is_gateway else h["mac"]
    via = list(h["via"])
    if h.get("hostname") and not h.get("self"):
        via.append("reverse-dns")
    if h.get("roles") or h.get("mdns_name"):
        via.append("mdns")
    # The device-maker brand from self-advertisement — distinct from `vendor`
    # (the OUI silicon maker). Empty when unrecognized; we never guess a brand.
    manufacturer, _domain = _brand(
        h.get("hostname", ""), h.get("mdns_name", ""), h.get("vendor", "")
    )
    return {
        "id": h["mac"],
        "name": name,
        "shape": "device",
        "kind": "device",                 # leaf (not "dir") — devices don't browse-in (yet)
        "macAddress": h["mac"],
        "ipAddress": h["ip"],
        "ipv6": h.get("ipv6") or [],
        "hostname": h.get("hostname", ""),
        "manufacturer": manufacturer,     # device-maker brand (Tesla, Brother) — derived, not OUI
        "vendor": h.get("vendor", ""),    # OUI registrant — the network-chip maker (honest, often a module vendor)
        "formFactor": _classify(h, gateway),
        "roles": h.get("roles") or [],    # advertised capabilities (mDNS) — incl. this Mac's own
        "online": True,
        "macRandomized": randomized,
        "discoveredVia": via,
    }


def _fresh(key: str) -> bool:
    ts = _CACHE["ts"].get(key)
    if not ts:
        return False
    age = (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds()
    return age < _FRESH_SECONDS


async def _discover() -> tuple[dict, list[dict]]:
    """Full sweep of the CURRENT network → (network, device entries). Caches both."""
    net = await _network_info()
    key = net["id"]
    if _fresh(key):
        return net, _CACHE["devices"].get(key, [])
    ident = await _identity()
    devices: list[dict] = []
    if ident["myip"]:
        subnet = ".".join(ident["myip"].split(".")[:3])

        # ARP sweep and mDNS browse are independent — run them concurrently.
        async def arp_scan() -> dict[str, dict]:
            await _provoke(subnet)        # provoke must finish before reading the table
            return await _neighbors()

        hosts, mdns = await asyncio.gather(arp_scan(), _mdns_map())

        # This Mac is never in its own ARP table — add it from _identity(), with
        # its real hardware MAC and user-facing name. Joe wants to see his own
        # device and what it broadcasts (its mDNS roles, folded in below).
        if ident.get("mac"):
            hosts[ident["myip"]] = {
                "ip": ident["myip"], "mac": _normmac(ident["mac"]),
                "via": ["self"], "ipv6": [], "self": True,
                "hostname": await _computer_name(),
            }

        async def enrich(h: dict) -> None:
            if not h.get("self"):         # self keeps its ComputerName, not a reverse-DNS lookup
                h["hostname"] = await _hostname(h["ip"])
            h["vendor"] = _vendor_offline(h["mac"]) or await _vendor_online(h["mac"])

        await asyncio.gather(*[enrich(h) for h in hosts.values()])

        # Fold mDNS onto each host by IP: friendly name + advertised roles.
        for h in hosts.values():
            m = mdns.get(h["ip"])
            if not m:
                continue
            h["roles"] = m["roles"]
            if m.get("name") and not h.get("self"):
                h["mdns_name"] = m["name"]
            if m.get("host") and not h.get("hostname"):
                h["hostname"] = m["host"].removesuffix(".local")

        devices = [_device_entry(h, ident["gateway"])
                   for h in sorted(hosts.values(), key=lambda x: [int(p) for p in x["ip"].split(".")])]
    _CACHE["nets"][key] = net
    _CACHE["devices"][key] = devices
    _CACHE["ts"][key] = datetime.datetime.now(datetime.timezone.utc)
    return net, devices


# ── volume_transport contract ────────────────────────────────────────────────

@test(params={})
@returns({"volumes": "{'type': 'array', 'description': 'Transport announce rows: name, kind, address'}", "count": "integer"})
@provides("volume_transport")
@timeout(20)
async def list_volumes(**_kwargs):
    """Announce each known Wi-Fi network as a `device`-realm volume.

    `kind: "device"` files them under the Local Network container (not Drives).
    The current network is always announced; networks seen earlier this session
    stay announced from cache so "Home" and "Whole Foods" both show.
    """
    net = await _network_info()
    _CACHE["nets"][net["id"]] = net
    vols = [
        {
            "name": n["name"],
            "kind": "device",
            "address": n["id"],
            "ssid": n.get("ssid") or None,
            "readOnly": True,
            "removable": True,
            "icon": "wifi",       # a network wears a Wi-Fi face, not a disk glyph
        }
        for n in _CACHE["nets"].values()
    ]
    return {"volumes": vols, "count": len(vols)}


@test(params={})
@returns({"id": "string", "entries": "{'type': 'array', 'description': 'Typed device nodes on this network'}", "count": "integer", "nextCursor": "string"})
@timeout(90)
async def list_contents(*, id=None, cursor=None, **_kwargs):
    """Serve a network volume's devices — its root listing.

    `id` is the network's address (gateway MAC). The current network discovers
    live (cached ~45s so repeat browses are instant); a different, cached network
    serves its last snapshot.

    Args:
        id: Network address (gateway MAC). Defaults to the current network.
        cursor: Unused — a network's device list fits one page.
    """
    cur = await _network_info()
    if id is None or id == cur["id"]:
        net, devices = await _discover()
        key = net["id"]
    else:
        key, devices = id, _CACHE["devices"].get(id, [])
    return {"id": key, "entries": devices, "count": len(devices), "nextCursor": None}


@test(params={})
@returns({"id": "string", "name": "string", "shape": "string", "kind": "string", "macAddress": "string", "ipAddress": "string", "vendor": "string", "formFactor": "string"})
@timeout(90)
async def read_node(*, id=None, **_kwargs):
    """Detail for one node — a device (by MAC) or a network (by gateway MAC)."""
    if id and id in _CACHE["nets"]:
        return _CACHE["nets"][id]
    # ensure the current network is discovered, then find the device
    _, devices = await _discover()
    for key in _CACHE["devices"]:
        for d in _CACHE["devices"][key]:
            if d["id"] == id:
                return d
    return {"id": id, "name": id, "shape": "device", "kind": "device"}
