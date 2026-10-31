# -*- coding: utf-8 -*-
"""青冥 · 统一配置读取。

两种对外暴露方式：
    mode = direct   本机或云服务器有公网 IP，直接对外，不需要任何中转
    mode = relay    没有公网 IP，用中转服务把内网端口映射到公网（单端口复用）

所有 IP / 端口 / 路径都从 config.ini 读，脚本里不再写死。
config.ini 找不到时回落到本机默认值，保证老用法（直接跑单个脚本）不炸。
"""
import configparser
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_NAME = "config.ini"

# 查找顺序：环境变量指定 -> 脚本同目录 -> 上一级目录（发布包的根）
SEARCH = [
    os.environ.get("DCMC_CONFIG") or "",
    os.path.join(HERE, CONFIG_NAME),
    os.path.join(os.path.dirname(HERE), CONFIG_NAME),
]

# 缺省值：全部中性留空，发布包必须靠 config.ini 提供实际配置
DEFAULTS = {
    "network": {
        "mode": "relay",
        "public_host": "",
        "public_port": "",
    },
    "relay": {
        "server": "",
        "port": "",
        "token": "",
    },
    "server": {
        "mc_port": "25565",
        "mux_port": "25566",
        "http_port": "8080",
    },
    "paths": {
        "source_pack": "",
        "webroot": "",
    },
    "client": {
        "base_url": "",
    },
}

MODE_RELAY = "relay"
MODE_DIRECT = "direct"


def _clean(p):
    p = (p or "").strip().strip('"').strip("'")
    return p.replace("\\", "/").rstrip("/")


class Cfg(object):
    def __init__(self, cp, path):
        self._cp = cp
        self.path = path                      # 实际读到的 config.ini（None = 全用默认值）
        self.root = os.path.dirname(path) if path else os.path.dirname(HERE)

        g = self._get
        legacy = cp.has_section("frp")        # 兼容老版本的 [frp] 段

        mode = (g("network", "mode") or MODE_RELAY).strip().lower()
        self.mode = mode if mode in (MODE_RELAY, MODE_DIRECT) else MODE_RELAY

        # ---- 中转服务（仅 relay 模式用到）----
        self.relay_server = g("relay", "server")
        self.relay_port = self._int(g("relay", "port"), 7000)
        self.relay_token = g("relay", "token")
        if legacy:                            # 老配置：字段全部平移
            self.relay_server = self.relay_server or g("frp", "server_addr")
            self.relay_port = self._int(g("relay", "port"), 0) or self._int(
                g("frp", "server_port"), 7000)
            self.relay_token = self.relay_token or g("frp", "user")

        # ---- 对外地址 ----
        self.public_host = _clean(g("network", "public_host"))
        self.public_port = self._int(g("network", "public_port"), 0)
        if not self.public_port and legacy:
            self.public_port = self._int(g("frp", "remote_port"), 0)
        if not self.public_port:
            self.public_port = 25565 if self.mode == MODE_DIRECT else 0

        # ---- 本机端口 ----
        self.mc_port = self._int(g("server", "mc_port"), 25565)
        self.mux_port = self._int(g("server", "mux_port"), 25566)
        self.http_port = self._int(g("server", "http_port"), 8080)

        # ---- 路径 ----
        self.source_pack = _clean(g("paths", "source_pack"))
        self.webroot = _clean(g("paths", "webroot"))
        self.sub = "dc"

        self._base_url = g("client", "base_url")

    # ---------- 派生 ----------
    @property
    def is_direct(self):
        """有公网 IP，直接对外：不需要分流器，也不需要中转客户端"""
        return self.mode == MODE_DIRECT

    @property
    def host(self):
        """玩家看到的域名 / IP。direct 模式必须自己填；relay 模式默认用中转服务的地址"""
        if self.public_host:
            return self.public_host
        return self.relay_server

    @property
    def public_addr(self):
        """玩家在多人游戏里填的地址"""
        if self.is_direct:
            return "%s:%d" % (self.host or "?", self.mc_port)
        return "%s:%d" % (self.host or "?", self.public_port)

    @property
    def public_http_port(self):
        """公网访问资源用的端口：direct 直接是 http_port，relay 是共用端口"""
        return self.http_port if self.is_direct else self.public_port

    @property
    def base_url(self):
        """客户端拉资源的根地址，结尾带 /"""
        v = _clean(self._base_url)
        if not v:
            v = "http://%s:%d/%s" % (self.host or "?", self.public_http_port, self.sub)
        if not v.endswith("/"):
            v += "/"
        return v

    @property
    def web_dir(self):
        """webroot/dc  —— 实际对外发布的目录（webroot 没填时返回空串，避免显示成孤零零的 dc）"""
        if not self.webroot:
            return ""
        return os.path.join(self.webroot, self.sub)

    @property
    def run_dir(self):
        d = os.path.join(HERE, ".run")
        if not os.path.isdir(d):
            try:
                os.makedirs(d)
            except OSError:
                pass
        return d

    # ---------- 校验 ----------
    def problems(self):
        bad = []
        if not self.source_pack:
            bad.append("paths.source_pack 没填（客户端 resourcepacks/DragonCore 的路径）")
        elif not os.path.isdir(self.source_pack):
            bad.append("资源来源目录不存在：%s" % self.source_pack)
        if not self.webroot:
            bad.append("paths.webroot 没填（服务端 webroot 的路径）")
        elif not os.path.isdir(self.webroot):
            bad.append("资源发布目录不存在：%s" % self.webroot)

        if self.is_direct:
            if not self.public_host:
                bad.append("network.public_host 没填（云服务器的公网 IP 或域名），"
                           "否则玩家不知道连哪，客户端也不知道去哪拉资源")
            if self.http_port == self.mc_port:
                bad.append("server.http_port 不能和 mc_port 相同（%d）" % self.mc_port)
        else:
            if not self.relay_server:
                bad.append("relay.server 没填（中转服务地址）")
            if not (1 <= self.public_port <= 65535):
                bad.append("network.public_port 不合法：%s" % self.public_port)
            if self.mc_port == self.mux_port:
                bad.append("server.mux_port 不能和 mc_port 相同（%d）" % self.mc_port)
            if self.http_port == self.mux_port:
                bad.append("server.http_port 不能和 mux_port 相同（%d）" % self.mux_port)
        if not (1 <= self.http_port <= 65535):
            bad.append("server.http_port 不合法：%s" % self.http_port)
        if not (1 <= self.mc_port <= 65535):
            bad.append("server.mc_port 不合法：%s" % self.mc_port)
        return bad

    def warnings(self):
        """不致命但会让第一次启动失败/困惑的问题"""
        w = []
        if self.is_direct:
            if self.host in ("127.0.0.1", "localhost"):
                w.append("network.public_host 是本机地址，玩家无法从外网连进来")
            if not self.public_host:
                w.append("network.public_host 为空：能启动，但客户端会拿到错误的资源地址")
        else:
            if not self.relay_token:
                w.append("relay.token 为空：多数免费中转服务需要通道标识，"
                         "留空会登录失败（自建服务且关闭鉴权时才可以留空）")
            if self.relay_server in ("", "127.0.0.1", "localhost"):
                w.append("relay.server 是本机地址，玩家无法从外网连进来")
        return w

    # ---------- 生成中转客户端的配置（keys 是对接方要求的，不对外暴露）----------
    def relay_ini(self):
        lines = [
            "[common]",
            "server_addr = %s" % self.relay_server,
            "server_port = %d" % self.relay_port,
            "login_fail_exit = false",
        ]
        if self.relay_token:
            lines.append("user = %s" % self.relay_token)
        lines += [
            "",
            "[proxy.%d]" % self.public_port,
            "type = tcp",
            "local_ip = 127.0.0.1",
            "local_port = %d" % self.mux_port,
            "remote_port = %d" % self.public_port,
            "",
        ]
        return "\n".join(lines)

    def relay_ini_path(self):
        return os.path.join(self.run_dir, "relay.ini")

    def write_relay_ini(self):
        p = self.relay_ini_path()
        with io.open(p, "w", encoding="ascii", newline="\r\n") as f:
            f.write(self.relay_ini())
        return p

    # 老名字保留，避免外部脚本引用不到
    frpc_ini = relay_ini
    write_frpc_ini = write_relay_ini

    # ---------- 内部 ----------
    def _get(self, sec, key):
        try:
            return self._cp.get(sec, key).strip()
        except Exception:
            return ""

    @staticmethod
    def _int(v, d):
        try:
            n = int(str(v).strip())
            return n if n > 0 else d
        except Exception:
            return d


def load(path=None):
    cp = configparser.ConfigParser()
    for sec, kv in DEFAULTS.items():
        cp[sec] = dict(kv)

    used = None
    for cand in ([path] if path else SEARCH):
        if cand and os.path.isfile(cand):
            try:
                with io.open(cand, encoding="utf-8-sig") as f:
                    cp.read_file(f)
            except Exception as e:
                raise RuntimeError("config.ini 解析失败（%s）：%s" % (cand, e))
            used = cand
            break
    return Cfg(cp, used)


if __name__ == "__main__":
    c = load()
    print("config       : %s" % (c.path or "（未找到，使用内置默认）"))
    print("对外方式     : %s" % ("公网直连" if c.is_direct else "中转映射"))
    if not c.is_direct:
        print("中转服务     : %s:%d  通道=%s"
              % (c.relay_server or "?", c.relay_port, c.relay_token or "-"))
    print("玩家地址     : %s" % c.public_addr)
    print("资源地址     : %s" % c.base_url)
    print("本机端口     : mc=%d  mux=%d  http=%d" % (c.mc_port, c.mux_port, c.http_port))
    print("资源来源     : %s" % c.source_pack)
    print("发布目录     : %s" % c.web_dir.replace("\\", "/"))
    bad = c.problems()
    print("检查         : %s" % ("OK" if not bad else ""))
    for b in bad:
        print("  ! %s" % b)
