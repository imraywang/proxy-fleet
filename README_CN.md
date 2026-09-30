# proxy-fleet

一行命令管理多台 VPS 代理节点：部署 [3x-ui](https://github.com/MHSanaei/3x-ui) + VLESS+Reality，自动生成 [Clash/Mihomo](https://github.com/MetaCubeX/mihomo) 订阅链接，增删节点后自动同步。

## 特性

- **一键部署** — 自动安装 3x-ui、扫描端口冲突并选择可用端口、配置 VLESS+Reality、开放防火墙、更新订阅文件，全部在一个 `deploy` 命令完成
- **订阅同步** — 从每个节点的 API 拉取实时状态，重新生成 Clash YAML，订阅永远反映真实配置
- **NAT 支持** — `--nat 10000-10009` 自动在端口段内选择可用端口
- **舰队状态** — 并行健康检查，显示所有节点的连通性和流量统计
- **白名单模式** — 基于 [Loyalsoldier/clash-rules](https://github.com/Loyalsoldier/clash-rules) 的 rule-provider，国内域名/IP 自动直连，其余全走代理。AI 服务强制代理，规则每日自动更新

## 环境要求

- Python 3.8+（仅使用标准库，无需 pip 安装依赖）
- 通过 `~/.ssh/config` 配置好 SSH 密钥登录到各 VPS
- 本机有 `curl`（用于连通性检测）
- VPS 系统为 Debian/Ubuntu（其他发行版未测试）

## 快速开始

```bash
# 1. 克隆
git clone https://github.com/oaker-io/proxy-fleet.git
cd proxy-fleet

# 2. 交互式初始化 — 生成 config.json
python3 scripts/fleet.py init

# 3. 部署到第一台 VPS
python3 scripts/fleet.py deploy my-vps --name "Tokyo" --emoji "🇯🇵"

# 4. 查看状态
python3 scripts/fleet.py status
```

## 命令一览

```
init                                    交互式创建配置文件
status                                  查看所有节点状态（并行检查）
deploy <host> [host...]                 部署到一台或多台 SSH 主机
deploy <host> --nat 10000-10009         NAT 机器指定端口范围
deploy <host> --name "名称" --emoji "🇺🇸"  自定义节点显示名
remove <host>                           从订阅中移除节点
sync                                    从所有节点重新生成并上传订阅文件
```

## 工作原理

### 部署流程

```
SSH 连接 → 扫描已占用端口 → 自动选可用端口
  → 安装 3x-ui（若未安装）→ CLI 重置面板凭证
  → Xray 生成 x25519 密钥 → API 创建 VLESS+Reality 入站
  → 检测防火墙类型（ufw/iptables/无）→ 开放端口
  → 验证连通性 → 写入 config.json → 同步订阅
```

### 订阅托管

生成的 Clash YAML 通过 SSH 上传到指定 VPS，用 nginx + SSL 提供 HTTPS 访问（推荐 Cloudflare 代理）。用户在 Clash Verge Rev / Mihomo 中导入订阅 URL 即可获取全部节点和分流规则。

`sync` 同时生成一对 Shadowrocket 文件（Shadowrocket 读不了 mihomo 的 `.mrs` 规则集）：

| 文件 | 在 Shadowrocket 中的导入方式 |
|---|---|
| `shadowrocket.txt` | 首页「添加订阅」— base64 编码的 `vless://` 节点 |
| `shadowrocket.conf` | 配置页「添加远程配置」— 分流规则 + 按节点名正则收纳节点的策略组 |

两种客户端的规则集都镜像到同一台订阅服务器，设备端不需要访问 GitHub/jsDelivr。

### 代理分组

| 分组 | 用途 |
|------|------|
| 🤖 AI Services | OpenAI、Claude、Gemini、Copilot、Cursor、Midjourney 等 — 优先走美国节点 |
| 🚀 Proxy | 未匹配的海外流量 — 优先走低延迟节点 |
| 🐟 Final | 兜底规则（白名单模式下默认走代理） |

## 文件结构

```
proxy-fleet/
├── config.json              # 舰队状态（含凭证，已 gitignore）
├── config.example.json      # 新用户配置模板
├── scripts/
│   └── fleet.py             # 主脚本
└── templates/rules/
    ├── ai.yaml              # AI 服务规则 (必须走代理)
    └── direct.yaml          # 自定义直连 (国内 AI、补充条目)
```

## 更新规则

编辑 `templates/rules/` 下的对应文件，然后：

```bash
python3 scripts/fleet.py sync
```

用户在 Clash Verge Rev 中刷新订阅即可拿到最新规则。

> **白名单模式说明**：国内域名和 IP 的规则由 [Loyalsoldier/clash-rules](https://github.com/Loyalsoldier/clash-rules) 的 rule-provider 自动提供（每 24 小时更新），无需手动维护。`direct.yaml` 仅用于补充 rule-provider 可能遗漏的条目（如较新的国内 AI 服务）。未匹配到任何规则的流量默认走代理。

## 技术备注

- **Xray v26 密钥格式**：`x25519` 输出 `PrivateKey` / `Password`（= 公钥）/ `Hash32`。旧版输出 `Private key` / `Public key`。脚本兼容两种格式。
- **3x-ui 安装脚本**是交互式的，无法可靠 pipe 输入。策略是先装默认配置，再通过 CLI 重置凭证。
- **3x-ui API**：`POST /login` → 获取 session cookie → `/panel/api/inbounds/{add,update,del,list}`
- **Reality 对非 VLESS 客户端返回 400** — 连通性检测时 400 = 节点正常。
- **端口冲突**是最常见的部署失败原因 — 脚本会在配置前先扫描端口。
- **xray 二进制**路径自动检测（glob `/usr/local/x-ui/bin/xray-linux-*`），同时支持 amd64 和 arm64。

## 许可证

[MIT](LICENSE)
