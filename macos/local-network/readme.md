---
id: local-network
services:
  - shell
name: Local Network
description: >
  The local network as a live, browsable volume. Each Wi-Fi network is a
  device-realm volume under "Local Network" in the sidebar; its devices —
  routers, TVs, speakers, printers, vacuums — are discovered live with
  Apple-native tools (no root, no nmap scan) and typed so they wear per-kind
  faces. Nothing is persisted: re-browse and it re-discovers.
color: "#2E7D6F"
website: "https://en.wikipedia.org/wiki/Address_Resolution_Protocol"
---

# Local Network

Provides `volume_transport`: the engine files this under the **Local Network**
container (transport realm `device`) and dispatches every browse here live —
there's no `.db`, so nothing goes stale.

**The three verbs:**
- `list_volumes` — announces each Wi-Fi network as a `device` volume (identity =
  the gateway's MAC; SSID isn't unique, subnets collide).
- `list_contents` — a network's devices, discovered live (cached ~45s so repeat
  browses are instant).
- `read_node` — detail for one device or network.

**How discovery works (unprivileged, Apple-native):**
- **ARP-provoke sweep** — a parallel touch of every address on the /24 makes the
  kernel resolve each on-link host at L2, catching devices that ignore ping
  (`nmap -sn` misses these and takes ~2 min).
- **`arp` / `ndp`** — the resolved IPv4 + IPv6 neighbor tables.
- **`dscacheutil`** — the system resolver's DHCP hostname, often the exact model
  (`dreame_vacuum_p2009`).
- **mDNS / Bonjour (`dns-sd`)** — the richest source: a *friendly* name
  (`Living Room`, `rokuultra`) and the device's advertised capabilities
  (airplay, cast, printer, …) land on `roles`. `dns-sd` streams forever, so each
  browse/resolve runs `shell.run(..., stream=True, timeout=N)` — the engine
  kills it at the deadline and returns the partial output.
- **OUI → manufacturer** — the first 3 MAC octets via the offline nmap/IEEE
  prefix DB; randomized (locally-administered) MACs are flagged, not mislabeled.

**This Mac itself** is added from `_identity()` — it's never in its own ARP
table. It shows as a `computer` with its en0 hardware MAC, its user-facing name
(`scutil --get ComputerName`), and the mDNS services *it* broadcasts on `roles`.
The gateway reads as **Router**.

Each device entry is typed `device` (a live instance of `hardware`, a `product`)
so the same node renders in the Navigator and reads over `data`. A device's face
comes from its `formFactor` (router / tv / printer / vacuum …); its listing
columns (IP / vendor / hostname / roles …) are derived by the engine from the
entry fields, like any shape listing.

**Limits:** guest networks isolate clients and suppress mDNS (you'll see the
gateway + this Mac, names sparse). MAC randomization means a phone has a
different MAC per SSID — it can't be unified across networks without credentials.
