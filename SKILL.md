---
name: proxy-fleet
description: >
  Manage multiple VPS proxy nodes: deploy 3x-ui + VLESS+Reality, generate Clash/Mihomo
  subscription URLs, and keep them in sync. Use this skill whenever the user mentions
  proxy servers, VPS deployment, 3x-ui, VLESS, Reality, Clash subscriptions, proxy fleet
  management, adding/removing proxy nodes, updating proxy rules, or checking proxy node
  status. Also trigger when the user says "部署代理", "代理节点", "订阅链接", "分流规则",
  or references their VPS proxy infrastructure in any way.
---

# proxy-fleet

Manage multiple VPS proxy nodes from a single command line. Deploy 3x-ui + VLESS+Reality
and generate Clash/Mihomo subscription URLs that auto-sync when nodes change.

## Working Directory

```bash
cd /Users/wangzhuc/proxy-fleet
```

## Commands

All commands run from the project root:

```bash
python3 scripts/fleet.py init                              # First-time setup
python3 scripts/fleet.py status                            # Parallel health check
python3 scripts/fleet.py deploy <host> [--name X --emoji 🇯🇵]  # One-click deploy
python3 scripts/fleet.py deploy <host> --nat 10000-10009   # NAT machine
python3 scripts/fleet.py remove <host>                     # Remove from subscription
python3 scripts/fleet.py sync                              # Regenerate subscription YAML
```

## When to Use Each Command

| User says | Command |
|-----------|---------|
| "加台机器 / deploy / 部署代理" | `deploy <ssh-host>` |
| "删掉这个节点 / remove" | `remove <ssh-host>` |
| "节点状态 / 看下代理" | `status` |
| "更新规则 / 加个 AI 域名" | Edit `templates/rules/*.yaml` → `sync` |
| "刷新订阅 / 同步" | `sync` |

## Deploy Flow

```
SSH → scan ports (avoid conflicts) → pick available port
  → install 3x-ui → reset credentials via CLI
  → generate x25519 keys → create VLESS+Reality inbound via API
  → detect firewall (ufw/iptables) → open ports
  → verify connectivity → save to config.json → sync subscription
```

## Key Files

| File | Purpose |
|------|---------|
| `config.json` | Fleet state — credentials, nodes, subscription hosting (gitignored) |
| `config.example.json` | Template for new users |
| `scripts/fleet.py` | Main CLI — all operations |
| `templates/rules/ai.yaml` | AI service routing rules (OpenAI, Claude, Gemini, etc.) |
| `templates/rules/proxy.yaml` | Common proxy rules (Google, GitHub, Twitter, etc.) |
| `templates/rules/streaming.yaml` | Streaming rules (YouTube, Netflix, etc.) |
| `templates/rules/direct.yaml` | China-direct and LAN rules |

## Updating Rules

Edit the relevant file in `templates/rules/`, then run `sync` to regenerate and upload
the subscription. Users refresh in Clash Verge Rev to get changes.

`sync` publishes three files under `subscription.file_path`: `config.yaml` (Clash/mihomo),
`shadowrocket.txt` (Shadowrocket node subscription, base64 vless:// links) and
`shadowrocket.conf` (Shadowrocket rules/groups; groups use `policy-regex-filter`, so the
conf never names a node). Inline rules in `templates/rules/` feed both; group names are
mapped via `SR_POLICY_MAP`.

## Technical Notes

These matter when debugging or extending the skill:

- **Xray v26+ key format**: `x25519` outputs `PrivateKey` / `Password` (= public key) / `Hash32`.
  Older versions use `Private key` / `Public key`. The script handles both.
- **3x-ui install** is interactive — the script installs with defaults, then resets credentials
  via the `/usr/local/x-ui/x-ui setting` CLI.
- **3x-ui API**: `POST /login` → session cookie → `/panel/api/inbounds/{add,update,del,list}`.
- **Reality returns HTTP 400** to non-VLESS clients — the connectivity check treats 400 as alive.
- **Port conflicts** are the #1 deploy failure cause — the script scans ports before configuring.
- **sync order**: rule sets are mirrored *before* configs are published, and every file is
  written to `.tmp` then `mv`'d — a config must never reference a ruleset the host can't serve.
- **Ruleset mirror cron**: `sync` installs `/etc/cron.d/proxy-fleet-mirror` on the subscription
  host (daily 04:17, script `/usr/local/sbin/proxy-fleet-mirror.sh`). The script validates the
  zstd magic before replacing any live `.mrs`, so a bad upstream day only means a stale mirror.
- **xray binary** path is auto-detected via glob (`/usr/local/x-ui/bin/xray-linux-*`), works
  on both amd64 and arm64.
