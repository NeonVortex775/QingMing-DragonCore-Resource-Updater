# 青冥　客户端资源自动更新

> Forge 1.12.2 + DragonCore 的客户端资源**增量**更新方案。
> 服务端换了贴图，玩家下次启动游戏自动下载变化的那几个文件 ——
> **服务器地址不变，玩家不用重装客户端，也不用手动替换资源包。**

---

## 它解决什么问题

传统做法是每次改图就重打一个几百 MB 的资源包，让玩家重新下载。这个项目把它变成：

| | 传统方式 | 本项目 |
|---|---|---|
| 改一张 HUD 贴图 | 重发整包（几百 MB） | 玩家启动时下几百 KB |
| 玩家操作 | 手动替换 / 重装 | 什么都不用做 |
| 服务器地址 | 可能被迫更换 | 完全不变 |
| 带宽成本 | 峰值巨大 | 只有增量 |

## 两种部署方式

| | `mode = direct` | `mode = relay` |
|---|---|---|
| 适用 | **VPS / 云服务器**（有公网 IP） | **家里电脑**（无公网 IP） |
| 需要穿透工具 | 不需要 | 需要（如 frp，自备） |
| 需要分流器 | 不需要 | 需要（内置，自动跑） |
| 端口 | MC、资源各一个（防火墙放行） | MC 与资源**共用一个公网端口** |

`direct` 模式下 MC 服务器自己对外、资源走 `http_port`，最简单；
`relay` 模式下靠「首包分流」让一个端口同时干两件事：

```
 玩家客户端                                 开服机
┌────────────────────┐                   ┌──────────────────────────────────┐
│ DragonCore         │  MC 协议          │ 中转客户端（如 frpc，自备）       │
│  + QingMingUpdater │──────────────────►│  公网地址:端口 → 本地 25566       │
│                    │                   │     └─ 分流器 (dc_mux.py)        │
│                    │                   │          ├─ MC 握手 → 25565 服务器│
│                    │  HTTP /dc/...     │          └─ GET/POST → 8080 资源  │
│                    │──────────────────►│               (dc_sync_server.py)│
└────────────────────┘                   │                 webroot/dc/      │
                                         └──────────────────────────────────┘
```

- 首包以 `GET /`、`POST` 这类 HTTP 方法开头 → 转给资源服务
- 其它（MC 握手的第一个字节是 VarInt 包长） → 转给 MC 服务器

**版本号 = 资源清单的 sha1 指纹。** 内容不变 → 版本号不变 → 客户端只发一个 12 字节请求就放行（<1 秒）；
改了任何一张图 → 版本号变 → 客户端只下载 sha1 不同的文件。

## 特性

- **两种对外方式**：VPS 直连 / 内网穿透，一个 `mode` 字段切换
- **单端口复用**（relay）：MC 与资源 HTTP 共用一个公网端口，玩家无感
- **增量下载**：sha1 清单比对，只下变化的文件
- **6 线程并发**，单文件失败重试 3 次
- **自愈**：失败的文件会记账，下次启动自动补齐（`__count` 校验，避免“永久缺一点”）
- **首次安装有进度条**，日常更新静默且 <1 秒
- **断网/服务端挂了静默放行**，不影响进游戏
- **所有 IP / 端口 / 路径集中在 `config.ini`**，换服不用重新编译 jar
- **只写清单里列出的路径，从不删除玩家本地文件**

## 快速开始

### 0. 需要

- Windows 10/11
- Python 3.7+（安装时勾选 `Add Python to PATH`）
- Java 8（玩家端跑 Forge 1.12.2）
- 公网入口二选一：**VPS / 云服务器**（`mode = direct`），或 **frp 等内网穿透**（`mode = relay`，工具自备）

### 1. 改 `config.ini`

```ini
[network]
; VPS / 云服务器（有公网 IP）：
mode        = direct
public_host = 你的公网 IP 或域名

; 家里电脑（内网穿透）则改为：
;   mode = relay
;   public_port = 穿透映射出来的公网端口
;   [relay] server / port / token = 穿透服务的地址、端口、通道标识

[paths]
source_pack = C:/Users/你/.minecraft/resourcepacks/DragonCore
webroot     = C:/服务器/webroot
```

### 2. 双击 `一键启动.bat`

```
[1/5] 清理上一次的进程
[2/5] 同步资源到发布目录
[3/5] 检查端口
[4/5] 启动服务
[5/5] 全链路自检

local http 8080              PASS  200  version=xxxxxxxx  0.05s
local mux 25566 mc           PASS  Paper 1.12.2  online 0/9999  0.06s
public http <公网地址>        PASS  200  version=xxxxxxxx  0.18s
public mc <公网地址>:端口     PASS  Paper 1.12.2  online 0/9999  0.15s
4/4 passed   ALL GREEN
```

窗口最小化即可（**不能关，关了服务就停**）。停服务按 `Ctrl+C` 或双击 `停止.bat`。

### 3. 装到玩家客户端

| 文件 | 放到 |
|---|---|
| `client/QingMingUpdater-1.0.jar` | `.minecraft/mods/` |
| `client/qingming-updater.txt` | `.minecraft/config/` |

`qingming-updater.txt` 里只有一行有效内容 —— 资源地址，启动器每次都会按 `config.ini` 重新生成它。

### 4. 以后改贴图

改完 `source_pack` 里的文件 → **再双击一次 `一键启动.bat`** → 玩家下次启动自动更新。

只推资源不动服务：`python server/launcher.py sync`

## 换服务器 / 换端口改哪里

| 想改的 | 改 `config.ini` 的 | 还要动什么 |
|---|---|---|
| 公网域名或 IP | `network.public_host` | 无 |
| 公网端口（玩家连的） | `network.public_port` | 无，玩家地址自动跟着变 |
| 穿透服务 | `relay.server` / `relay.port` / `relay.token` | 无 |
| 本机 MC 端口 | `server.mc_port` | 无 |
| 资源服务端口 | `server.http_port` | 无 |
| 贴图来源 / 发布目录 | `paths.*` | 无 |
| 公网地址不一致（CDN） | `client.base_url` | 重发 `qingming-updater.txt` |

客户端地址是三级优先级，**换服不用重新编译 jar**：

```
-Ddcmc.base=http://x.x.x.x:port/dc/          ← JVM 参数，最高优先
.minecraft/config/qingming-updater.txt       ← 文件配置
jar 内编译期默认值                            ← 兜底
```

## 目录结构

```
├── config.ini              ★ 唯一需要改的文件
├── 一键启动.bat / 停止.bat
├── 教程.md                  完整教程（13 节，含排错）
├── 快速开始.txt              5 步速查
├── server/
│   ├── launcher.py          启动器：一个进程管 HTTP + 分流器 + 中转客户端
│   ├── dc_config.py         读 config.ini
│   ├── dc_sync_server.py    资源镜像 + 清单 + 静态服务
│   ├── dc_mux.py            单端口首包分流（仅 relay 模式）
│   └── dc_verify.py         全链路自检
├── client/                  发给玩家的东西
├── relay/                   内网穿透客户端的放置处（工具自备，不随仓库分发）
├── src/                     CoreMod 源码（Java 8，不引用任何 MC 类）
└── build/                   重新编译 / 重新打包
```

## 首次发客户端：别让玩家从公网裸拉

穿透隧道 / 小水管 VPS 的带宽有限，150 MB 的整包可能要等几分钟。

**正确做法**：把 `resourcepacks/DragonCore` 整个目录随客户端一起发。
更新器发现本地文件大小对得上就直接建索引，**0 下载、1 秒放行**，之后走增量。

## 自己编译 / 打包

```bash
# CoreMod（需要 Forge 1.12.2 的 universal jar 作 classpath）
bash build/build.sh

# 重新组装可分发 zip
python build/打包.py
```

或者直接下载 [Releases](https://github.com/NeonVortex775/QingMing-DragonCore-Resource-Updater/releases) 里已经打包好的 zip。

## 常见问题

| 现象 | 处理 |
|---|---|
| `端口 8080 被占用` | 上次进程没退干净，双击 `停止.bat`；或改 `http_port` |
| 中转客户端反复退出 | 看 `server/.run/logs/relay.log`，多半是 `relay.token` 或 `relay.port` 错了 |
| 公网 FAIL、本地 PASS | 穿透没建起来，检查 `[relay]` 三项和公网端口是否被占；direct 模式检查安全组 |
| 能进服但贴图不更新 | 浏览器开 `.../dc/version.txt`：出版本号 → 问题在客户端；超时 → 穿透/防火墙 |
| 客户端没下载 | 删掉客户端 `.dcsync.json` 强制全量校验 |

更详细的排查见 [`教程.md`](教程.md)。

## 已知限制

- 只支持 Windows 服务端（脚本用 bat + Python 标准库，逻辑本身跨平台）
- 需要 Python 3.7+，未打包成 exe
- 分流器按首包特征判断，非 HTTP / 非 MC 的 TCP 流量会被当成 MC 转发
- 内网穿透的带宽是硬瓶颈，大更新建议直接发整包

## 许可

未声明许可证。若使用 frp 做内网穿透，其客户端属 [fatedier/frp](https://github.com/fatedier/frp)，Apache-2.0（自备，不随本仓库分发）。
本产品完全免费，并且没有违反任何相关规则，如果你觉得损害了你的个人利益，请不要破坏公益资源。
