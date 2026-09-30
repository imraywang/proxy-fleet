#!/usr/bin/env python3
"""
proxy-fleet: Manage VPS proxy infrastructure.

Usage:
    python3 fleet.py init                              # Interactive setup
    python3 fleet.py status                            # Show all nodes
    python3 fleet.py deploy <host> [host...]           # Deploy to new machines
    python3 fleet.py deploy <host> --nat 10000-10009   # NAT machine with port range
    python3 fleet.py deploy <host> --name "Tokyo" --emoji "🇯🇵"
    python3 fleet.py remove <host>                     # Remove a node
    python3 fleet.py sync                              # Regenerate & upload subscription
"""

import json, subprocess, sys, os, secrets, re, textwrap, time, base64, urllib.parse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "config.json"
EXAMPLE_CONFIG_PATH = SKILL_DIR / "config.example.json"
RULES_DIR = SKILL_DIR / "templates" / "rules"

# Rule sets we mirror onto our own subscription host and reference from the
# generated config. (local_name, behavior, upstream_path, local_filename).
#
# These are MetaCubeX's pre-compiled `.mrs` geosite/geoip sets, NOT Loyalsoldier
# text lists. Why .mrs: the text lists parse into large in-memory domain tries
# (reject.txt alone was 4.7 MB → tens of MB resident) and OOM'd the iOS network
# extension — the full config cold-started the mihomo core fine but the tunnel
# was jetsam-killed on load (秒关). .mrs is a compiled succinct structure: the
# same coverage in ~600 KB total, a fraction of the memory. Behavior is domain
# or ipcidr only (mrs can't express `classical`, so Loyalsoldier's applications
# list is dropped — those apps just get proxied, correct for whitelist mode).
#
# The mirror (see cmd_sync) fetches these on the VPS — which reaches GitHub/
# jsdelivr fine — and drops them next to config.yaml, so clients only ever pull
# rulesets from our own reachable origin (jsdelivr is poisoned in China).
RULE_PROVIDERS = [
    ("ai",         "domain", "geosite/category-ai-!cn.mrs",  "ai.mrs"),
    ("ads",        "domain", "geosite/category-ads-all.mrs", "ads.mrs"),
    ("private",    "domain", "geosite/private.mrs",          "private.mrs"),
    ("apple",      "domain", "geosite/apple.mrs",            "apple.mrs"),
    ("icloud",     "domain", "geosite/icloud.mrs",           "icloud.mrs"),
    ("cn-domain",  "domain", "geosite/cn.mrs",               "cn-domain.mrs"),
    ("telegram-ip","ipcidr", "geoip/telegram.mrs",           "telegram-ip.mrs"),
    ("cn-ip",      "ipcidr", "geoip/cn.mrs",                 "cn-ip.mrs"),
    ("private-ip", "ipcidr", "geoip/private.mrs",            "private-ip.mrs"),
]
META_RULES_BASE = "https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@meta/geo"

# Shadowrocket can't read .mrs (mihomo's binary format), so its config gets
# the same routing semantics from Surge-format text lists, mirrored onto the
# same host as the .mrs sets. (local_name, rule_type, upstream_url,
# local_filename, kind). Domain lists are DOMAIN-SET (`.example.com` lines),
# the cidr lists are RULE-SET (`IP-CIDR,...` lines, no policy).
#
# kind "txt" is mirrored as-is. kind "mihomo-list" is MetaCubeX's text form
# of a geosite set (`+.example.com` / `example.com` lines), rewritten to
# DOMAIN-SET syntax on the host. Ads use it so both clients block the same
# ~900 domains: Loyalsoldier's reject.txt is ~186k lines / 4 MB — far more
# false positives and a real memory risk in the iOS network extension.
SR_RULES_BASE = "https://raw.githubusercontent.com/Loyalsoldier/surge-rules/release"
SR_RULESETS = [
    ("ai",          "DOMAIN-SET", f"{META_RULES_BASE}/geosite/category-ai-!cn.list",
                                  "sr-ai.txt",          "mihomo-list"),
    ("ads",         "DOMAIN-SET", f"{META_RULES_BASE}/geosite/category-ads-all.list",
                                  "sr-ads.txt",         "mihomo-list"),
    ("private",     "DOMAIN-SET", f"{SR_RULES_BASE}/private.txt",      "sr-private.txt",     "txt"),
    ("apple",       "DOMAIN-SET", f"{SR_RULES_BASE}/apple.txt",        "sr-apple.txt",       "txt"),
    ("icloud",      "DOMAIN-SET", f"{SR_RULES_BASE}/icloud.txt",       "sr-icloud.txt",      "txt"),
    ("cn-domain",   "DOMAIN-SET", f"{SR_RULES_BASE}/direct.txt",       "sr-cn-domain.txt",   "txt"),
    ("telegram-ip", "RULE-SET",   f"{SR_RULES_BASE}/telegramcidr.txt", "sr-telegram-ip.txt", "txt"),
    ("cn-ip",       "RULE-SET",   f"{SR_RULES_BASE}/cncidr.txt",       "sr-cn-ip.txt",       "txt"),
]

# Files `sync` publishes under subscription.file_path.
CLASH_FILE = "config.yaml"
SR_CONF_FILE = "shadowrocket.conf"   # rules + groups (Shadowrocket "config")
SR_NODES_FILE = "shadowrocket.txt"   # base64 vless:// list (Shadowrocket "subscription")

# Pin the 3x-ui version the whole fleet runs on. The panel API client below
# (login → session cookie → /panel/api/inbounds) handles both the 2.8.x API
# and the CSRF-token login that 3.4.x added — see REMOTE_INBOUND_SCRIPT. When
# bumping, re-verify that login flow still holds against the new release.
XUI_VERSION = "v3.4.1"

# ── Helpers ──────────────────────────────────────────────────

def load_config():
    if not CONFIG_PATH.exists():
        print("Error: config.json not found. Run 'fleet.py init' first.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    print(f"  Config saved.")

def ssh(host, cmd, timeout=30, check=True):
    """Run a command on remote host via SSH."""
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no", host, cmd],
        capture_output=True, text=True, timeout=timeout
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"[{host}] SSH failed: {r.stderr.strip()}")
    return r.stdout.strip()

def ssh_script(host, script_text, args="", timeout=60):
    """Pipe a Python script to the remote host via SSH stdin."""
    cmd = f"python3 - {args}" if args else "python3 -"
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", host, cmd],
        input=script_text, capture_output=True, text=True, timeout=timeout
    )
    if r.returncode != 0:
        raise RuntimeError(f"[{host}] Remote script failed: {r.stderr.strip()}")
    return r.stdout.strip()

def ssh_host_info(host):
    """Parse SSH config to get HostName for a host alias."""
    r = subprocess.run(["ssh", "-G", host], capture_output=True, text=True)
    info = {"hostname": host, "port": 22}
    for line in r.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            k, v = parts
            if k == "hostname":
                info["hostname"] = v
            elif k == "port":
                info["port"] = int(v)
    return info

def detect_xray_binary(host):
    """Detect the xray binary path on remote host (handles amd64/arm64)."""
    out = ssh(host, "ls /usr/local/x-ui/bin/xray-linux-* 2>/dev/null | head -1", check=False)
    if out:
        return out.strip()
    # Fallback: check common names
    for name in ["xray-linux-amd64", "xray-linux-arm64", "xray"]:
        path = f"/usr/local/x-ui/bin/{name}"
        exists = ssh(host, f"test -f {path} && echo yes || echo no", check=False)
        if "yes" in exists:
            return path
    raise RuntimeError(f"[{host}] Cannot find xray binary in /usr/local/x-ui/bin/")

# ── Port Scanner ─────────────────────────────────────────────

def scan_ports(host):
    """Get set of TCP ports in use on remote host."""
    out = ssh(host, "ss -tlnp 2>/dev/null | awk 'NR>1{print $4}'", check=False)
    ports = set()
    for line in out.splitlines():
        m = re.search(r':(\d+)$', line)
        if m:
            ports.add(int(m.group(1)))
    return ports

def pick_port(used_ports, preferred_ports, nat_range=None):
    """Pick the best available port."""
    if nat_range:
        lo, hi = nat_range
        for p in range(lo, hi + 1):
            if p not in used_ports:
                return p
        raise RuntimeError(f"No available port in NAT range {lo}-{hi}")
    for p in preferred_ports:
        if p not in used_ports:
            return p
    raise RuntimeError(f"All preferred ports are in use: {preferred_ports}")

# ── Firewall ─────────────────────────────────────────────────

def configure_firewall(host, ports):
    """Detect firewall type and open ports."""
    has_ufw = "active" in ssh(host, "ufw status 2>/dev/null || echo inactive", check=False)
    if has_ufw:
        for p in ports:
            ssh(host, f"ufw allow {p}/tcp 2>/dev/null", check=False)
        ssh(host, "ufw reload 2>/dev/null", check=False)
        print(f"  [{host}] UFW: opened ports {ports}")
    else:
        policy = ssh(host, "iptables -L INPUT -n 2>/dev/null | head -1", check=False)
        if "DROP" in policy or "REJECT" in policy:
            for p in ports:
                ssh(host, f"iptables -A INPUT -p tcp --dport {p} -j ACCEPT 2>/dev/null", check=False)
            print(f"  [{host}] iptables: opened ports {ports}")
        else:
            print(f"  [{host}] No restrictive firewall detected, skipping")

# ── 3x-ui Install ───────────────────────────────────────────

def install_3xui(host, creds):
    """Install 3x-ui and set credentials."""
    installed = "x-ui" in ssh(host, "which x-ui 2>/dev/null || echo ''", check=False)
    if installed:
        print(f"  [{host}] 3x-ui already installed, skipping install")
    else:
        print(f"  [{host}] Installing 3x-ui {XUI_VERSION} (this may take 1-2 minutes)...")
        ssh(host,
            f"echo 'y' | bash <(curl -Ls https://raw.githubusercontent.com/MHSanaei/3x-ui/{XUI_VERSION}/install.sh) {XUI_VERSION}",
            timeout=180, check=False)
        print(f"  [{host}] Install complete")

    # Always reset credentials to ensure consistency
    harden_panel(host, creds)


def harden_panel(host, creds):
    """Set credentials and keep the 3x-ui panel off the public internet.

    Everything this script does talks to the panel over SSH at localhost, so
    nothing needs it reachable from outside — and it speaks plain HTTP, so a
    public listener would send the login in cleartext. Bind it to loopback
    (web UI: `ssh -L <port>:127.0.0.1:<port> <host>`) and turn off 3x-ui's own
    subscription server (port 2096): clients get their config from our nginx
    origin, so it's pure attack surface. subEnable has no CLI flag, so it's
    written straight into the settings table.
    """
    ssh(host, (
        f"/usr/local/x-ui/x-ui setting "
        f"-username {creds['username']} "
        f"-password {creds['password']} "
        f"-port {creds['panel_port']} "
        f"-listenIP 127.0.0.1 "
        f"-webBasePath /"
    ))
    ssh_script(host, textwrap.dedent("""
        import sqlite3
        c = sqlite3.connect("/etc/x-ui/x-ui.db")
        c.execute("delete from settings where key = 'subEnable'")
        c.execute("insert into settings (key, value) values ('subEnable', 'false')")
        c.commit()
    """))
    ssh(host, "systemctl restart x-ui")
    print(f"  [{host}] Panel on 127.0.0.1:{creds['panel_port']} only, built-in sub server off")

# ── VLESS+Reality Inbound ────────────────────────────────────

REMOTE_INBOUND_SCRIPT = textwrap.dedent(r'''
import json, subprocess, sys, re, urllib.request, urllib.parse, http.cookiejar, secrets, glob

port = int(sys.argv[1])
remark = sys.argv[2]
panel_port = int(sys.argv[3])
username = sys.argv[4]
password = sys.argv[5]
sni = sys.argv[6]

# Auto-detect xray binary (amd64 or arm64)
candidates = glob.glob("/usr/local/x-ui/bin/xray-linux-*")
XRAY = candidates[0] if candidates else "/usr/local/x-ui/bin/xray-linux-amd64"

# Generate x25519 keys
# Xray v26+:  "PrivateKey: ... / Password: ... / Hash32: ..."
# Xray v26.x: "PrivateKey: ... / Password (PublicKey): ... / Hash32: ..."
# Xray older: "Private key: ... / Public key: ..."
keys_out = subprocess.check_output([XRAY, "x25519"]).decode()
kv = {}
for l in keys_out.strip().splitlines():
    if ": " in l:
        k, v = l.split(": ", 1)
        kv[k.strip()] = v.strip()

priv = kv.get("PrivateKey") or kv.get("Private key", "")
# Public-key field label varies by Xray version: "Password",
# "Password (PublicKey)" (v26+), or "Public key" (older). Match by prefix.
pub = next(
    (v for k, v in kv.items()
     if k.startswith("Password") or k.lower().startswith("public key")),
    "",
)

if not priv or not pub:
    print(json.dumps({"success": False, "error": f"Failed to parse x25519 output: {kv}"}))
    sys.exit(0)

uuid = subprocess.check_output([XRAY, "uuid"]).decode().strip()
sid = secrets.token_hex(4)

# Login. 3x-ui 3.4.x guards POSTs with a CSRF token embedded in the login
# page (<meta name="csrf-token">) and paired with the session cookie; 2.8.x
# has neither. Fetch "/" first, then send the token as X-CSRF-Token on every
# request (omitted when absent, so the same flow works on both versions).
panel = f"http://localhost:{panel_port}"
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

home = opener.open(f"{panel}/").read().decode("utf-8", "replace")
m = re.search(r'name="csrf-token"\s+content="([^"]+)"', home)
csrf = m.group(1) if m else ""

def api(path, data=None, json_body=False):
    headers = {}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    if json_body:
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(f"{panel}{path}", data, headers)

opener.open(api("/login",
    urllib.parse.urlencode({"username": username, "password": password}).encode()))

# Delete existing VLESS inbounds to avoid duplicates. del/ is a POST route
# (3.4.x returns 404 for GET) — pass an empty body to force the POST method.
existing = json.loads(opener.open(api("/panel/api/inbounds/list")).read())
for ib in existing.get("obj", []):
    if ib.get("protocol") == "vless":
        opener.open(api(f"/panel/api/inbounds/del/{ib['id']}", b""))

# 3x-ui 3.4.x stores clients in dedicated tables (clients/client_inbounds) and
# requires a non-empty, unique email — a client with email "" is silently
# dropped from the generated xray config (clients: null → all handshakes fail).
# 2.8.x tolerated empty email, so this stays compatible with both.
settings = json.dumps({
    "clients": [{"id": uuid, "flow": "xtls-rprx-vision",
                 "email": f"{remark}-{secrets.token_hex(3)}",
                 "limitIp": 0, "totalGB": 0, "expiryTime": 0, "enable": True,
                 "tgId": "", "subId": secrets.token_hex(8), "reset": 0}],
    "decryption": "none", "fallbacks": []
})
# NOTE on the Reality dest (= defaults.sni): pick a TLS-1.3 site whose
# Certificate record fits Xray's hardcoded 8192-byte limit. www.microsoft.com
# now returns an ~8273-byte cert and fails with "handshake did not complete
# successfully" on xray-core 26.x (XTLS/Xray-core#6356) — use apple/cloudflare/
# bing/icloud instead. Verify a new dest before switching the fleet to it.
stream = json.dumps({
    "network": "tcp", "security": "reality", "externalProxy": [],
    "realitySettings": {
        "show": False, "xver": 0, "dest": f"{sni}:443",
        "serverNames": [sni], "privateKey": priv,
        "minClient": "", "maxClient": "", "maxTimediff": 0, "shortIds": [sid],
        "settings": {"publicKey": pub, "fingerprint": "chrome", "serverName": "", "spiderX": "/"}
    },
    "tcpSettings": {"acceptProxyProtocol": False, "header": {"type": "none"}}
})
sniffing = json.dumps({"enabled": True, "destOverride": ["http", "tls", "quic", "fakedns"],
                        "metadataOnly": False, "routeOnly": False})

body = json.dumps({
    "up": 0, "down": 0, "total": 0, "remark": remark, "enable": True, "expiryTime": 0,
    "listen": "", "port": port, "protocol": "vless",
    "settings": settings, "streamSettings": stream, "sniffing": sniffing
}).encode()

result = json.loads(opener.open(api("/panel/api/inbounds/add", body, json_body=True)).read())

print(json.dumps({
    "success": result.get("success", False),
    "uuid": uuid, "public_key": pub, "short_id": sid, "port": port
}))
''')

def create_inbound(host, port, remark, cfg):
    """Create VLESS+Reality inbound on remote host. Returns node info dict."""
    creds = cfg["credentials"]
    defaults = cfg["defaults"]
    args = f"{port} {remark} {creds['panel_port']} {creds['username']} {creds['password']} {defaults['sni']}"
    out = ssh_script(host, REMOTE_INBOUND_SCRIPT, args, timeout=30)
    result = json.loads(out)
    if not result.get("success"):
        raise RuntimeError(f"[{host}] Failed to create inbound: {result.get('error', 'unknown')}")
    print(f"  [{host}] VLESS+Reality on port {port} — UUID: {result['uuid'][:8]}...")
    return result

# ── Remote Query ─────────────────────────────────────────────

REMOTE_QUERY_SCRIPT = textwrap.dedent(r'''
import json, urllib.request, urllib.parse, http.cookiejar, subprocess, sys, glob, re

panel_port = int(sys.argv[1])
username = sys.argv[2]
password = sys.argv[3]

panel = f"http://localhost:{panel_port}"
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

inbounds = []
xver = "?"

try:
    # 3.4.x: grab the CSRF token from the login page and send it as a header
    # (paired with the session cookie). 2.8.x has no token, so hdr stays empty.
    home = opener.open(f"{panel}/").read().decode("utf-8", "replace")
    m = re.search(r'name="csrf-token"\s+content="([^"]+)"', home)
    hdr = {"X-CSRF-Token": m.group(1)} if m else {}
    opener.open(urllib.request.Request(f"{panel}/login",
        urllib.parse.urlencode({"username": username, "password": password}).encode(), hdr))
    resp = opener.open(urllib.request.Request(f"{panel}/panel/api/inbounds/list", headers=hdr))
    data = json.loads(resp.read())

    # 2.8.x returns streamSettings/settings as JSON strings; 3.4.x returns
    # them as already-parsed objects. Accept either.
    def _obj(v):
        return v if isinstance(v, dict) else json.loads(v or "{}")

    for ib in data.get("obj", []):
        stream = _obj(ib.get("streamSettings"))
        settings = _obj(ib.get("settings"))
        reality = stream.get("realitySettings", {})
        clients = settings.get("clients", [])
        inbounds.append({
            "id": ib["id"], "protocol": ib["protocol"], "port": ib["port"],
            "remark": ib.get("remark", ""), "enable": ib.get("enable", False),
            "up": ib.get("up", 0), "down": ib.get("down", 0),
            "uuid": clients[0]["id"] if clients else "",
            "public_key": reality.get("settings", {}).get("publicKey", ""),
            "short_id": (reality.get("shortIds", [""]))[0] if reality.get("shortIds") else "",
            "sni": (reality.get("serverNames", [""]))[0] if reality.get("serverNames") else "",
        })

    # Detect xray binary and version
    candidates = glob.glob("/usr/local/x-ui/bin/xray-linux-*")
    if candidates:
        xver = subprocess.check_output(
            [candidates[0], "version"], stderr=subprocess.STDOUT
        ).decode().split()[1]
except Exception as e:
    pass

print(json.dumps({"inbounds": inbounds, "xray_version": xver}))
''')

def query_node(host, cfg):
    """Query a node's 3x-ui API for inbound details."""
    creds = cfg["credentials"]
    args = f"{creds['panel_port']} {creds['username']} {creds['password']}"
    try:
        out = ssh_script(host, REMOTE_QUERY_SCRIPT, args, timeout=15)
        return json.loads(out)
    except Exception as e:
        return {"error": str(e), "inbounds": []}

# ── Verify ───────────────────────────────────────────────────

def verify_port(server, port, timeout=10):
    """Check if a port is reachable from local machine."""
    r = subprocess.run(
        ["curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}",
         "--connect-timeout", str(timeout), f"https://{server}:{port}"],
        capture_output=True, text=True
    )
    # Reality returns 400 to non-VLESS clients — that means it's alive
    return r.stdout.strip() in ("400", "200")

# ── Subscription Generator ───────────────────────────────────

def _load_inline(name):
    """Rule lines from templates/rules/<name>.yaml, as written (`- TYPE,value,target`)."""
    lines = []
    path = RULES_DIR / f"{name}.yaml"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                lines.append(line)
    return lines

def load_rules():
    """Load rule templates and compose whitelist-mode rule list.

    Rule priority (top = highest):
      1. AI services (inline)        → 🤖 AI Services
         AI services (geosite mrs)   → 🤖 AI Services  (category-ai-!cn)
      2. Ads (geosite mrs)           → REJECT
      3. Custom direct (inline)      → DIRECT  (China AI, .cn, etc.)
      4. Telegram IPs (geoip mrs)    → 🚀 Proxy
      5. Private domains (geosite)   → DIRECT
      6. Apple (geosite mrs)         → DIRECT
      7. iCloud (geosite mrs)        → DIRECT
      8. China domains (geosite mrs) → DIRECT
      9. China IPs (geoip mrs)       → DIRECT
     10. Private/LAN IPs (geoip mrs) → DIRECT
     11. QUIC (UDP/443)              → REJECT  (only what would be proxied)
     12. MATCH                       → 🐟 Final (default proxy)

    All rule-sets are MetaCubeX .mrs (see RULE_PROVIDERS). No GEOIP,CN literal
    and no geoip database: China IPs come from the cn-ip set, so nothing has to
    be downloaded on first launch beyond the small .mrs files we self-host.
    Anything unmatched just gets proxied — the correct whitelist-mode default.
    """
    rules = []

    # Inline rules (manually curated, highest priority)
    rules += _load_inline("ai")
    # maintained upstream list; the inline file stays for additions and so AI
    # routing works even before the ruleset has been fetched once.
    rules.append("- RULE-SET,ai,🤖 AI Services")

    # Ad-blocking (compiled geosite set)
    rules.append("- RULE-SET,ads,REJECT")

    # Custom direct rules (China AI services, .cn TLD, etc.)
    rules += _load_inline("direct")

    # Compiled .mrs rule-set references (mirrored to our own host)
    rules.append("- RULE-SET,telegram-ip,🚀 Proxy,no-resolve")
    rules.append("- RULE-SET,private,DIRECT")
    rules.append("- RULE-SET,apple,DIRECT")
    rules.append("- RULE-SET,icloud,DIRECT")
    rules.append("- RULE-SET,cn-domain,DIRECT")
    rules.append("- RULE-SET,cn-ip,DIRECT,no-resolve")
    rules.append("- RULE-SET,private-ip,DIRECT,no-resolve")
    # Vision flow carries QUIC poorly. Everything that reaches this point is
    # headed for the proxy, so dropping UDP/443 here makes HTTP/3 fall back to
    # TCP without touching direct traffic — same as Shadowrocket's
    # block-quic=all-proxy. (AI / Telegram matched above keep their UDP.)
    rules.append("- AND,((NETWORK,UDP),(DST-PORT,443)),REJECT")
    rules.append("- MATCH,🐟 Final")

    return rules

def dns_servers(cfg):
    """(bootstrap plain-IP resolvers, DoH resolvers) shared by both outputs.

    With fake-ip (Clash) / remote resolution (Shadowrocket), every proxied
    domain is resolved at the exit node, so locally we only need to resolve
    DIRECT/China domains — a domestic resolver does that well. No foreign
    fallback / geoip fallback-filter → no geoip database needed on first
    launch, which is what makes this cold-start cleanly on iOS.
    """
    dns_cfg = cfg["defaults"].get("dns", {})
    domestic_ns = dns_cfg.get("domestic", ["223.5.5.5", "119.29.29.29"])
    # IP-literal DoH (not dns.alidns.com/doh.pub hostnames): inside the iOS
    # network extension, bootstrapping a DoH *hostname* via plain UDP can stall,
    # taking DNS — and therefore all browsing — down with it. Connecting to the
    # resolver by IP removes that bootstrap step entirely. Both endpoints serve
    # DoH with a cert valid for the IP (AliDNS 223.5.5.5, DNSPod 1.12.12.12).
    domestic_doh = dns_cfg.get("domestic_doh", ["https://223.5.5.5/dns-query", "https://1.12.12.12/dns-query"])
    return domestic_ns, domestic_doh


def build_proxies(cfg, node_details):
    """Proxy dicts for every node with a live VLESS inbound, plus the names of
    all / US nodes (for group ordering)."""
    defaults = cfg["defaults"]
    proxies = []
    proxy_names = []
    us_names = []

    for node in cfg["nodes"]:
        nd = node_details.get(node["ssh_host"], {})
        inbounds = nd.get("inbounds", [])
        vless_ib = next((ib for ib in inbounds if ib["protocol"] == "vless"), None)
        if not vless_ib:
            continue

        full_name = f"{node['emoji']} {node['name']}"
        proxy_names.append(full_name)
        if "🇺🇸" in node.get("emoji", ""):
            us_names.append(full_name)

        proxies.append({
            "name": full_name,
            "type": "vless",
            "server": node["server"],
            "port": node["port"],
            "uuid": vless_ib["uuid"],
            "network": "tcp",
            "tls": True,
            "udp": True,
            "flow": "xtls-rprx-vision",
            "servername": vless_ib.get("sni") or defaults["sni"],
            "reality-opts": {
                "public-key": vless_ib["public_key"],
                "short-id": vless_ib["short_id"],
            },
            "client-fingerprint": defaults["fingerprint"],
        })

    return proxies, proxy_names, us_names


def generate_subscription(cfg, node_details):
    """Generate complete mihomo YAML config."""
    sub = cfg["subscription"]
    domestic_ns, domestic_doh = dns_servers(cfg)
    proxies, proxy_names, us_names = build_proxies(cfg, node_details)

    if not proxies:
        print("  Warning: no active VLESS inbounds found on any node")
        return ""

    # AI group: US nodes first, then others
    ai_order = us_names + [n for n in proxy_names if n not in us_names]
    # Proxy group: HK/JP first for lower latency
    proxy_order = [n for n in proxy_names if "🇭🇰" in n or "🇯🇵" in n] + \
                  [n for n in proxy_names if "🇭🇰" not in n and "🇯🇵" not in n]

    rules = load_rules()

    lines = [
        "############################################",
        "# Mihomo (Clash Meta) Subscription Config",
        f"# Nodes: {len(proxies)} | Generated by proxy-fleet",
        "############################################",
        "",
        "mixed-port: 7890",
        "allow-lan: false",
        "mode: rule",
        # warning, not info: at info the core logs a line per connection, and
        # inside the iOS network extension (50 MB jetsam cap) that churn is
        # pure overhead. Clash Mi's connections panel works regardless.
        "log-level: warning",
        "unified-delay: true",
        # race multiple resolved IPs for faster, more reliable connects.
        "tcp-concurrent: true",
        # mobile guidance: without keepalive probes, idle sockets die instead
        # of being kept warm — fewer tracked connections in the NE process and
        # fewer radio wakeups. Verified to parse on mihomo 1.19.29.
        "disable-keep-alive: true",
        # process matching is impossible inside the iOS network extension and
        # we ship no PROCESS-NAME rules, so keep it off everywhere. Quoted so
        # YAML 1.1 parsers don't fold bare `off` into the boolean false.
        'find-process-mode: "off"',
        # NOTE: no top-level `global-client-fingerprint`. Newer mihomo cores
        # (e.g. the one Clash Mi ships) removed it and log an error; each proxy
        # already carries its own `client-fingerprint`, so nothing is lost.
        "",
        # store-selected: remember the user's manual group choice across
        # restarts / subscription updates. store-fake-ip: persist the fake-ip
        # pool so restarts don't re-resolve everything (faster mobile resume).
        "profile:",
        "  store-selected: true",
        "  store-fake-ip: true",
        "",
        "dns:",
        "  enable: true",
        "  ipv6: false",
        "  enhanced-mode: fake-ip",
        "  fake-ip-range: 198.18.0.1/16",
        # domains that must see a real IP: LAN names, OS connectivity checks,
        # NTP (clock sync before the tunnel is fully up), STUN (NAT discovery
        # for calls/WebRTC), console networks and QQ's localhost login helper.
        "  fake-ip-filter:",
        '    - "*.lan"',
        '    - "*.local"',
        '    - "*.localhost"',
        '    - "+.home.arpa"',
        '    - "+.msftconnecttest.com"',
        '    - "+.msftncsi.com"',
        '    - "time.*.com"',
        '    - "time.*.gov"',
        '    - "time.*.apple.com"',
        '    - "ntp.*.com"',
        '    - "+.pool.ntp.org"',
        '    - "+.stun.*.*"',
        '    - "+.stun.*.*.*"',
        '    - "stun.l.google.com"',
        '    - "+.srv.nintendo.net"',
        '    - "+.xboxlive.com"',
        '    - "localhost.ptlogin2.qq.com"',
        "  default-nameserver:",
    ]
    for ns in domestic_ns:
        lines.append(f"    - {ns}")
    lines.append("  nameserver:")
    for ns in domestic_doh:
        lines.append(f"    - {ns}")
    # Sniffer: recover the domain from TLS SNI / HTTP Host / QUIC for
    # connections that arrive as a bare IP (hardcoded IPs, in-app DoH), so
    # domain rules still apply to them. override-destination stays off: the
    # sniffed name is used for rule matching only, never to re-dial.
    lines += [
        "",
        "sniffer:",
        "  enable: true",
        "  force-dns-mapping: true",
        "  parse-pure-ip: true",
        "  override-destination: false",
        "  sniff:",
        "    HTTP:",
        "      ports: [80, 8080-8880]",
        "    TLS:",
        "      ports: [443, 8443]",
        "    QUIC:",
        "      ports: [443, 8443]",
        "",
        "proxies:",
    ]

    for p in proxies:
        lines.append(f'  - name: "{p["name"]}"')
        lines.append(f'    type: {p["type"]}')
        lines.append(f'    server: {p["server"]}')
        lines.append(f'    port: {p["port"]}')
        lines.append(f'    uuid: {p["uuid"]}')
        lines.append(f'    network: {p["network"]}')
        lines.append(f'    tls: {str(p["tls"]).lower()}')
        lines.append(f'    udp: {str(p["udp"]).lower()}')
        lines.append(f'    flow: {p["flow"]}')
        lines.append(f'    servername: {p["servername"]}')
        lines.append(f'    reality-opts:')
        lines.append(f'      public-key: {p["reality-opts"]["public-key"]}')
        lines.append(f'      short-id: {p["reality-opts"]["short-id"]}')
        lines.append(f'    client-fingerprint: {p["client-fingerprint"]}')
        lines.append("")

    lines.append("proxy-groups:")
    lines.append('  - name: "🤖 AI Services"')
    lines.append("    type: select")
    lines.append("    proxies:")
    for n in ai_order:
        lines.append(f'      - "{n}"')
    lines.append("")

    lines.append('  - name: "🚀 Proxy"')
    lines.append("    type: select")
    lines.append("    proxies:")
    for n in proxy_order:
        lines.append(f'      - "{n}"')
    lines.append("      - DIRECT")
    lines.append("")

    lines.append('  - name: "🐟 Final"')
    lines.append("    type: select")
    lines.append("    proxies:")
    lines.append('      - "🚀 Proxy"')
    lines.append("      - DIRECT")
    lines.append("")

    # Rule providers. Compiled .mrs sets, mirrored (by `sync`) onto our own
    # subscription host and served from the same origin the client already
    # fetches the config from — NOT cdn.jsdelivr.net, which is routinely
    # DNS-poisoned/blocked in mainland China. Two failure modes are removed at
    # once: (1) a blocked cold-start fetch is fatal on a fresh iOS client, and
    # (2) the old multi-MB text lists OOM'd the iOS network extension. See
    # RULE_PROVIDERS / mirror step.
    lines.append("rule-providers:")
    _base = f"https://{sub['domain']}/{sub['url_path']}/ruleset"
    for idx, (pname, behavior, _upstream, filename) in enumerate(RULE_PROVIDERS):
        lines.append(f"  {pname}:")
        lines.append(f"    type: http")
        lines.append(f"    behavior: {behavior}")
        lines.append(f"    format: mrs")
        lines.append(f'    url: "{_base}/{filename}"')
        lines.append(f"    path: ./ruleset/{filename}")
        # staggered ~daily intervals: with a shared 86400 all eight providers
        # refresh in the same instant, stacking concurrent downloads + matcher
        # rebuilds inside the memory-capped iOS network extension.
        lines.append(f"    interval: {86400 + idx * 1800}")
    lines.append("")

    lines.append("rules:")
    for r in rules:
        lines.append(f"  {r}")

    return "\n".join(lines) + "\n"

# ── Shadowrocket ─────────────────────────────────────────────
#
# Shadowrocket imports Clash YAML only partially (no .mrs, no sniffer, no
# mihomo DNS keys), so it gets its own pair of files:
#   shadowrocket.txt  — node subscription: base64 of standard vless:// links.
#                       Reality can't be expressed in a .conf [Proxy] line
#                       (undocumented keys), but share links import cleanly.
#   shadowrocket.conf — rules + groups. Groups collect nodes by name regex
#                       (policy-regex-filter), so the conf never hardcodes a
#                       node and stays valid as the fleet changes.
# Group names are plain ASCII: they're referenced unquoted in comma-separated
# lines, and an emoji/space name is one parser quirk away from breaking.

SR_TEST_URL = "http://cp.cloudflare.com/generate_204"
SR_US_REGEX = "🇺🇸|US-"
# inline rules target Clash group names; map them onto the Shadowrocket ones
SR_POLICY_MAP = {"🤖 AI Services": "AI", "🚀 Proxy": "Proxy", "🐟 Final": "Final"}


def _vless_uri(p):
    """Standard (Xray-core) VLESS share link, which Shadowrocket imports."""
    q = urllib.parse.urlencode({
        "encryption": "none",
        "flow": p["flow"],
        "security": "reality",
        "sni": p["servername"],
        "fp": p["client-fingerprint"],
        "pbk": p["reality-opts"]["public-key"],
        "sid": p["reality-opts"]["short-id"],
        "type": p["network"],
    })
    return f"vless://{p['uuid']}@{p['server']}:{p['port']}?{q}#{urllib.parse.quote(p['name'])}"


def generate_sr_nodes(proxies):
    """Shadowrocket subscription body: base64 of newline-separated links."""
    links = "\n".join(_vless_uri(p) for p in proxies)
    return base64.b64encode(links.encode()).decode() + "\n"


def _sr_inline(name):
    """templates/rules/<name>.yaml converted to Shadowrocket rule lines."""
    out = []
    for line in _load_inline(name):
        parts = line.lstrip("- ").split(",")
        if len(parts) >= 3:
            parts[2] = SR_POLICY_MAP.get(parts[2], parts[2])
        out.append(",".join(parts))
    return out


def generate_shadowrocket_conf(cfg):
    """Shadowrocket config mirroring load_rules()'s whitelist-mode order."""
    sub = cfg["subscription"]
    domestic_ns, domestic_doh = dns_servers(cfg)
    origin = f"https://{sub['domain']}/{sub['url_path']}"
    rs = {name: (rtype, f"{origin}/ruleset/{fn}") for name, rtype, _, fn, _ in SR_RULESETS}

    def ruleset(name, policy, no_resolve=False):
        rtype, url = rs[name]
        return f"{rtype},{url},{policy}" + (",no-resolve" if no_resolve else "")

    lines = [
        "# Shadowrocket config | Generated by proxy-fleet",
        f"# 节点订阅: {origin}/{SR_NODES_FILE}",
        "# 先在首页添加上面的节点订阅，再导入本配置；策略组按节点名正则自动收纳节点。",
        "",
        "[General]",
        "bypass-system = true",
        "skip-proxy = 192.168.0.0/16, 10.0.0.0/8, 172.16.0.0/12, localhost, *.local, captive.apple.com",
        "tun-excluded-routes = 10.0.0.0/8, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, "
        "192.0.0.0/24, 192.0.2.0/24, 192.88.99.0/24, 192.168.0.0/16, 198.51.100.0/24, "
        "203.0.113.0/24, 224.0.0.0/4, 255.255.255.255/32, 239.255.255.250/32",
        # same resolver policy as the Clash config: domestic IP-literal DoH for
        # DIRECT traffic, proxied domains resolve at the exit. No `system`
        # fallback — that would hand queries to the ISP resolver.
        f"dns-server = {', '.join(domestic_doh)}",
        f"fallback-dns-server = {', '.join(domestic_ns)}",
        "ipv6 = false",
        "prefer-ipv6 = false",
        "dns-direct-system = false",
        "private-ip-answer = true",
        "dns-direct-fallback-proxy = true",
        "icmp-auto-reply = true",
        "hijack-dns = 8.8.8.8:53, 8.8.4.4:53",
        # Vision flow carries QUIC poorly; drop UDP/443 on proxied connections
        # so HTTP/3 falls back to TCP. Direct connections keep QUIC.
        "block-quic = all-proxy",
        "udp-policy-not-supported-behaviour = REJECT",
        f"update-url = {origin}/{SR_CONF_FILE}",
        "",
        "[Proxy Group]",
        # AI defaults to automatic US failover — a dead manually-picked node
        # otherwise takes every AI service down until someone notices.
        "AI = select, US-Auto, US, Proxy, policy-select-name=US-Auto",
        # PROXY is Shadowrocket's built-in "node selected on the home screen".
        "Proxy = select, PROXY, Auto, US, DIRECT",
        "Final = select, Proxy, DIRECT",
        f"US-Auto = fallback, url={SR_TEST_URL}, interval=600, timeout=5, select=0, "
        f"policy-regex-filter={SR_US_REGEX}",
        f"US = select, policy-regex-filter={SR_US_REGEX}",
        f"Auto = url-test, url={SR_TEST_URL}, interval=600, tolerance=50, timeout=5, select=0, "
        "policy-regex-filter=.",
        "",
        "[Rule]",
    ]
    lines += _sr_inline("ai")
    lines.append(ruleset("ai", "AI"))
    lines.append(ruleset("ads", "REJECT"))
    lines += _sr_inline("direct")
    lines += [
        ruleset("telegram-ip", "Proxy", no_resolve=True),
        ruleset("private", "DIRECT"),
        ruleset("apple", "DIRECT"),
        ruleset("icloud", "DIRECT"),
        ruleset("cn-domain", "DIRECT"),
        ruleset("cn-ip", "DIRECT", no_resolve=True),
    ]
    # LAN / reserved ranges (the Clash side gets these from geoip/private.mrs)
    for cidr in ("10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
                 "172.16.0.0/12", "192.168.0.0/16"):
        lines.append(f"IP-CIDR,{cidr},DIRECT,no-resolve")
    lines.append("FINAL,Final")
    return "\n".join(lines) + "\n"

# ── Commands ─────────────────────────────────────────────────

def cmd_init():
    """Interactive setup to create config.json."""
    if CONFIG_PATH.exists():
        ans = input("config.json already exists. Overwrite? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted.")
            return

    print("\n=== proxy-fleet init ===\n")

    # Credentials
    username = input("Panel username [admin]: ").strip() or "admin"
    password = input("Panel password (leave empty to auto-generate): ").strip()
    if not password:
        password = secrets.token_urlsafe(16)
        print(f"  Generated password: {password}")
    panel_port = input("Panel port [9453]: ").strip() or "9453"

    # Subscription hosting
    print("\n--- Subscription hosting ---")
    sub_host = input("SSH host for subscription hosting: ").strip()
    domain = input("Subscription domain (e.g. sub.example.com): ").strip()
    url_path = secrets.token_hex(8)
    print(f"  Generated URL path: {url_path}")
    file_path = f"/var/www/sub/{url_path}"

    # DNS
    print("\n--- DNS config ---")
    dns_preset = input("DNS preset - [1] China, [2] Global [1]: ").strip() or "1"
    if dns_preset == "2":
        # IP-literal DoH only — see dns_servers() for why hostnames stall on iOS.
        dns_cfg = {
            "domestic": ["8.8.8.8", "1.1.1.1"],
            "domestic_doh": ["https://8.8.8.8/dns-query", "https://1.1.1.1/dns-query"],
        }
    else:
        dns_cfg = {
            "domestic": ["223.5.5.5", "119.29.29.29"],
            "domestic_doh": ["https://223.5.5.5/dns-query", "https://1.12.12.12/dns-query"],
        }

    cfg = {
        "credentials": {
            "username": username,
            "password": password,
            "panel_port": int(panel_port)
        },
        "subscription": {
            "ssh_host": sub_host,
            "domain": domain,
            "file_path": file_path,
            "url_path": url_path,
            "cert_path": "/etc/nginx/ssl/cert.crt",
            "key_path": "/etc/nginx/ssl/cert.key"
        },
        "defaults": {
            "protocol": "vless",
            "security": "reality",
            "sni": "www.microsoft.com",
            "fingerprint": "chrome",
            "preferred_ports": [443, 2083, 8443, 2053, 2087, 2096],
            "dns": dns_cfg
        },
        "nodes": []
    }

    save_config(cfg)
    print(f"\n✅ Config created. Next steps:")
    print(f"  1. Deploy nodes:  python3 scripts/fleet.py deploy <ssh-host>")
    print(f"  2. Set up nginx on {sub_host} with your SSL cert")
    print(f"  3. Point DNS: {domain} → your hosting server IP")

def cmd_status():
    cfg = load_config()
    nodes = cfg["nodes"]

    if not nodes:
        print("\nNo nodes configured. Run 'fleet.py deploy <host>' to add one.")
        return

    print(f"\n{'Node':<20} {'Server':<20} {'Port':>6}  {'Status':<16} {'Traffic':>12}")
    print("─" * 78)

    def check(node):
        host = node["ssh_host"]
        try:
            nd = query_node(host, cfg)
            vless = next((ib for ib in nd.get("inbounds", []) if ib["protocol"] == "vless"), None)
            reachable = verify_port(node["server"], node["port"])
            up = vless.get("up", 0) if vless else 0
            down = vless.get("down", 0) if vless else 0
            traffic = f"↑{up // 1048576}M ↓{down // 1048576}M"
            status = "✅ OK" if reachable else "⚠️  Unreachable"
            return node, status, traffic
        except Exception as e:
            return node, f"❌ {str(e)[:20]}", "—"

    with ThreadPoolExecutor(max_workers=len(nodes)) as pool:
        futures = [pool.submit(check, n) for n in nodes]
        for f in as_completed(futures):
            node, status, traffic = f.result()
            name = f"{node['emoji']} {node['name']}"
            print(f"{name:<20} {node['server']:<20} {node['port']:>6}  {status:<16} {traffic:>12}")

    sub = cfg["subscription"]
    print(f"\n📋 Subscription: https://{sub['domain']}/{sub['url_path']}/config.yaml")
    print(f"   Hosted on: {sub['ssh_host']} ({sub['file_path']})\n")

def cmd_deploy(hosts, nat_range=None, name_override=None, emoji_override=None):
    cfg = load_config()
    creds = cfg["credentials"]
    defaults = cfg["defaults"]

    for host in hosts:
        print(f"\n{'='*50}")
        print(f"Deploying to: {host}")
        print(f"{'='*50}")

        # 1. Check connectivity
        try:
            arch = ssh(host, "uname -m", timeout=10)
            print(f"  [{host}] Connected ({arch})")
        except Exception as e:
            print(f"  [{host}] ❌ Cannot connect: {e}")
            continue

        # 2. Scan ports
        used = scan_ports(host)
        print(f"  [{host}] Ports in use: {sorted(used)[:15]}{'...' if len(used) > 15 else ''}")

        # 3. Pick port
        try:
            port = pick_port(used, defaults["preferred_ports"], nat_range)
        except RuntimeError as e:
            print(f"  [{host}] ❌ {e}")
            continue
        print(f"  [{host}] Selected port: {port}")

        # 4. Install 3x-ui
        install_3xui(host, creds)

        # 5. Wait for x-ui to start
        time.sleep(2)

        # 6. Create inbound
        host_info = ssh_host_info(host)
        server = host_info["hostname"]
        remark = name_override or host.replace(".", "-").replace(" ", "-")
        try:
            result = create_inbound(host, port, remark, cfg)
        except Exception as e:
            print(f"  [{host}] ❌ Inbound creation failed: {e}")
            continue

        # 7. Firewall
        configure_firewall(host, [port, creds["panel_port"]])

        # 8. Verify
        reachable = verify_port(server, port)
        print(f"  [{host}] Connectivity: {'✅ OK' if reachable else '⚠️  Not reachable (may need time or external firewall)'}")

        # 9. Add to config
        emoji = emoji_override or "🌐"
        already = any(n["ssh_host"] == host for n in cfg["nodes"])
        if not already:
            cfg["nodes"].append({
                "name": remark,
                "emoji": emoji,
                "ssh_host": host,
                "server": server,
                "port": port,
                "nat_ports": list(nat_range) if nat_range else None,
            })
            save_config(cfg)
            print(f"  [{host}] Added to fleet config")
        else:
            for n in cfg["nodes"]:
                if n["ssh_host"] == host:
                    n["port"] = port
                    if name_override:
                        n["name"] = name_override
                    if emoji_override:
                        n["emoji"] = emoji_override
            save_config(cfg)
            print(f"  [{host}] Updated in fleet config")

    # 10. Sync subscription
    print(f"\n{'='*50}")
    print("Syncing subscription...")
    cmd_sync()

def cmd_remove(host):
    cfg = load_config()
    found = any(n["ssh_host"] == host for n in cfg["nodes"])
    if not found:
        print(f"Node '{host}' not found in config.")
        return

    cfg["nodes"] = [n for n in cfg["nodes"] if n["ssh_host"] != host]
    save_config(cfg)
    print(f"Removed {host} from fleet config")
    print(f"Note: 3x-ui is still installed on {host}. To uninstall:")
    print(f"  ssh {host} 'x-ui uninstall'")

    cmd_sync()

MIRROR_SCRIPT_PATH = "/usr/local/sbin/proxy-fleet-mirror.sh"
MIRROR_CRON_PATH = "/etc/cron.d/proxy-fleet-mirror"
MIRROR_LOG_PATH = "/var/log/proxy-fleet-mirror.log"


def _mirror_entries():
    """(upstream_url, local_filename, kind) for every mirrored rule set."""
    return ([(f"{META_RULES_BASE}/{up}", fn, "mrs") for _, _, up, fn in RULE_PROVIDERS] +
            [(url, fn, kind) for _, _, url, fn, kind in SR_RULESETS])


def _mirror_script(sub):
    """Shell script that refreshes the rule-set mirror, installed on the sub host.

    Runs unattended from cron, so every download is validated before it
    replaces a live file: .mrs is a zstd frame, and a CDN error page or a
    truncated response carries no 28 b5 2f fd magic; the Shadowrocket text
    lists are checked for an HTML error body. Refusing those means a bad
    upstream day degrades to "mirror is a bit stale", never to "clients fetch
    garbage and the iOS core fails to start".
    """
    rp_dir = f"{sub['file_path']}/ruleset"
    entries = " ".join(f"{url}::{fn}::{kind}" for url, fn, kind in _mirror_entries())
    return textwrap.dedent(f"""\
        #!/bin/sh
        # proxy-fleet: refresh mirrored rule sets (MetaCubeX .mrs for Clash,
        # Loyalsoldier surge-rules text lists for Shadowrocket).
        # Generated by `fleet.py sync` — edits are overwritten on next sync.
        set -u
        DIR={rp_dir}
        mkdir -p "$DIR" || exit 1
        cd "$DIR" || exit 1
        ok=1
        for e in {entries}; do
          url=${{e%%::*}}; rest=${{e#*::}}; fn=${{rest%%::*}}; kind=${{rest##*::}}
          if ! curl -fsSL --retry 3 --connect-timeout 20 "$url" -o "$fn.tmp"; then
            echo "FAIL fetch $url" >&2; rm -f "$fn.tmp"; ok=0; continue
          fi
          size=$(wc -c < "$fn.tmp")
          if [ "$kind" = mrs ]; then
            magic=$(od -An -N4 -tx1 "$fn.tmp" | tr -d ' \\n')
            [ "$magic" = "28b52ffd" ] && valid=1 || valid=0
          else
            magic=text
            head -c 512 "$fn.tmp" | grep -qi '<html\\|<!doctype' && valid=0 || valid=1
            # mihomo text geosite → Surge DOMAIN-SET: `+.x` (x and subdomains) is `.x`
            if [ $valid -eq 1 ] && [ "$kind" = mihomo-list ]; then
              sed 's/^+\\././' "$fn.tmp" > "$fn.conv" && mv "$fn.conv" "$fn.tmp" || valid=0
            fi
          fi
          if [ $valid -eq 1 ] && [ "$size" -gt 100 ]; then
            mv "$fn.tmp" "$fn"
            echo "OK $fn $size"
          else
            echo "FAIL invalid $fn magic=$magic size=$size" >&2
            rm -f "$fn.tmp"; ok=0
          fi
        done
        [ $ok -eq 1 ]
        """)


def mirror_rulesets(sub):
    """Mirror the MetaCubeX .mrs rule sets onto our own subscription host.

    Installs a validated refresh script plus a daily /etc/cron.d entry on the
    VPS (which reaches GitHub/jsdelivr fine), then runs the script once. The
    cron keeps the mirror fresh between syncs — previously the .mrs sets only
    updated when someone ran `sync`, so cn-domain/ads drifted weeks stale.
    Returns True if every ruleset passed validation on this run.
    """
    rp_dir = f"{sub['file_path']}/ruleset"
    # 04:17 host-local daily. No % in the command — cron would treat it as \n.
    cron_line = f"17 4 * * * root {MIRROR_SCRIPT_PATH} >{MIRROR_LOG_PATH} 2>&1\n"
    setup = (
        f"cat > {MIRROR_SCRIPT_PATH} && chmod 755 {MIRROR_SCRIPT_PATH} && "
        f"printf '%s' '{cron_line}' > {MIRROR_CRON_PATH} && chmod 644 {MIRROR_CRON_PATH} && "
        f"{MIRROR_SCRIPT_PATH}"
    )
    r = subprocess.run(
        ["ssh", sub["ssh_host"], setup],
        input=_mirror_script(sub), capture_output=True, text=True, timeout=180
    )
    oks = [l for l in r.stdout.strip().splitlines() if l.startswith("OK ")]
    if r.returncode == 0 and len(oks) == len(_mirror_entries()):
        print(f"  Mirrored {len(oks)} rulesets → {sub['ssh_host']}:{rp_dir}")
        print(f"  Cron installed: {MIRROR_CRON_PATH} (daily 04:17, log {MIRROR_LOG_PATH})")
        return True
    print(f"  ⚠️  Ruleset mirror incomplete: {r.stderr.strip() or 'validation failed'}")
    return False


def cmd_sync():
    cfg = load_config()
    nodes = cfg["nodes"]

    if not nodes:
        print("No nodes to sync.")
        return

    print("Querying all nodes...")
    node_details = {}
    with ThreadPoolExecutor(max_workers=len(nodes)) as pool:
        future_map = {pool.submit(query_node, n["ssh_host"], cfg): n for n in nodes}
        for f in as_completed(future_map):
            node = future_map[f]
            try:
                nd = f.result()
                node_details[node["ssh_host"]] = nd
                ib_count = len(nd.get("inbounds", []))
                print(f"  [{node['ssh_host']}] {ib_count} inbound(s)")
            except Exception as e:
                print(f"  [{node['ssh_host']}] ❌ Query failed: {e}")

    yaml_content = generate_subscription(cfg, node_details)
    if not yaml_content:
        print("❌ No subscription content generated.")
        return
    proxies, _, _ = build_proxies(cfg, node_details)
    outputs = {
        CLASH_FILE: yaml_content,
        SR_CONF_FILE: generate_shadowrocket_conf(cfg),
        SR_NODES_FILE: generate_sr_nodes(proxies),
    }
    print(f"\nGenerated subscriptions with {len(proxies)} nodes")

    # Mirror rule sets BEFORE publishing configs: a config that references a
    # ruleset the host can't serve yet 404s on a fresh client's cold start.
    # A failed refresh keeps the previous validated files, so we only abort
    # when something is actually missing, not merely stale.
    sub = cfg["subscription"]
    print("Mirroring rule sets...")
    if not mirror_rulesets(sub):
        rp_dir = f"{sub['file_path']}/ruleset"
        present = " && ".join(f"test -s {rp_dir}/{fn}" for _, fn, _ in _mirror_entries())
        if subprocess.run(["ssh", sub["ssh_host"], present]).returncode != 0:
            print("❌ Rule sets missing on the host — not publishing configs that would 404.")
            return
        print("  Previous rule sets still in place — publishing anyway.")

    # Upload each file to a temp name and mv it into place: a client fetching
    # mid-upload must see the old file or the new one, never a truncated mix.
    base_url = f"https://{sub['domain']}/{sub['url_path']}"
    for fname, content in outputs.items():
        dst = f"{sub['file_path']}/{fname}"
        r = subprocess.run(
            ["ssh", sub["ssh_host"], f"mkdir -p {sub['file_path']} && cat > {dst}.tmp && mv {dst}.tmp {dst}"],
            input=content, capture_output=True, text=True
        )
        if r.returncode != 0:
            print(f"❌ Upload of {fname} failed: {r.stderr}")
            return
        print(f"✅ Uploaded {sub['ssh_host']}:{dst}")

    print(f"📋 Clash:       {base_url}/{CLASH_FILE}")
    print(f"📋 Shadowrocket 节点订阅: {base_url}/{SR_NODES_FILE}")
    print(f"📋 Shadowrocket 配置:     {base_url}/{SR_CONF_FILE}")

# ── Main ─────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "init":
        cmd_init()
    elif cmd == "status":
        cmd_status()
    elif cmd == "deploy":
        if len(sys.argv) < 3:
            print("Usage: fleet.py deploy <host> [host...] [--nat START-END] [--name NAME] [--emoji EMOJI]")
            sys.exit(1)
        hosts = []
        nat_range = None
        name_override = None
        emoji_override = None
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--nat" and i + 1 < len(sys.argv):
                lo, hi = sys.argv[i + 1].split("-")
                nat_range = (int(lo), int(hi))
                i += 2
            elif sys.argv[i] == "--name" and i + 1 < len(sys.argv):
                name_override = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--emoji" and i + 1 < len(sys.argv):
                emoji_override = sys.argv[i + 1]
                i += 2
            else:
                hosts.append(sys.argv[i])
                i += 1
        cmd_deploy(hosts, nat_range, name_override, emoji_override)
    elif cmd == "remove":
        if len(sys.argv) < 3:
            print("Usage: fleet.py remove <host>")
            sys.exit(1)
        cmd_remove(sys.argv[2])
    elif cmd == "sync":
        cmd_sync()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)

if __name__ == "__main__":
    main()
