# -*- coding: utf-8 -*-
"""青冥 · 统一配置读取。

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

# 缺省值全为空：必须在 config.ini 里填好，否则启动器会指出缺哪一项
DEFAULTS = {
    "frp": {
        "server_addr": "free.frp.vip",
        "server_port": "3010",
        "user": "",
        "remote_port": "11491",
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


def _clean(p):
    p = (p or "").strip().strip('"').strip("'")
    return p.replace("\\", "/").rstrip("/")


class Cfg(object):
    def __init__(self, cp, path):
        self._cp = cp
        self.path = path                      # 实际读到的 config.ini（None = 全用默认值）
        self.root = os.path.dirname(path) if path else os.path.dirname(HERE)

        g = self._get
        self.server_addr = g("frp", "server_addr") or "free.frp.vip"
        self.server_port = self._int(g("frp", "server_port"), 3010)
        self.user = g("frp", "user")
        self.remote_port = self._int(g("frp", "remote_port"), 11491)

        self.mc_port = self._int(g("server", "mc_port"), 25565)
        self.mux_port = self._int(g("server", "mux_port"), 25566)
        self.http_port = self._int(g("server", "http_port"), 8080)

        self.source_pack = _clean(g("paths", "source_pack"))
        self.webroot = _clean(g("paths", "webroot"))
        self.sub = "dc"

        self._base_url = g("client", "base_url")

    # ---------- 派生 ----------
    @property
    def public_addr(self):
        """玩家在多人游戏里填的地址"""
        return "%s:%d" % (self.server_addr, self.remote_port)

    @property
    def base_url(self):
        """客户端拉资源的根地址，结尾带 /"""
        v = _clean(self._base_url)
        if not v:
            v = "http://%s:%d/%s" % (self.server_addr, self.remote_port, self.sub)
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
        if self.mc_port == self.mux_port:
            bad.append("server.mux_port 不能和 mc_port 相同（%d）" % self.mc_port)
        if self.http_port == self.mux_port:
            bad.append("server.http_port 不能和 mux_port 相同（%d）" % self.mux_port)
        if not (1 <= self.server_port <= 65535):
            bad.append("frp.server_port 不合法：%s" % self.server_port)
        if not (1 <= self.remote_port <= 65535):
            bad.append("frp.remote_port 不合法：%s" % self.remote_port)
        return bad

    def warnings(self):
        """不致命但会让第一次启动失败/困惑的问题"""
        w = []
        if not self.user and "frp.vip" in self.server_addr:
            w.append("frp.user 为空：free.frp.vip 必须填你自己的用户标识，"
                     "否则 frpc 登录会失败（自建 frps 无鉴权时才可以留空）")
        if self.server_addr in ("", "127.0.0.1", "localhost"):
            w.append("frp.server_addr 是本机地址，玩家无法从外网连进来")
        return w

    # ---------- 生成 frpc 配置 ----------
    def frpc_ini(self):
        lines = [
            "[common]",
            "server_addr = %s" % self.server_addr,
            "server_port = %d" % self.server_port,
            "login_fail_exit = false",
        ]
        if self.user:
            lines.append("user = %s" % self.user)
        lines += [
            "",
            "[proxy.%d]" % self.remote_port,
            "type = tcp",
            "local_ip = 127.0.0.1",
            "local_port = %d" % self.mux_port,
            "remote_port = %d" % self.remote_port,
            "",
        ]
        return "\n".join(lines)

    def frpc_ini_path(self):
        return os.path.join(self.run_dir, "frpc.ini")

    def write_frpc_ini(self):
        p = self.frpc_ini_path()
        with io.open(p, "w", encoding="ascii", newline="\r\n") as f:
            f.write(self.frpc_ini())
        return p

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
    print("frp          : %s:%d  user=%s" % (c.server_addr, c.server_port, c.user or "-"))
    print("玩家地址     : %s" % c.public_addr)
    print("资源地址     : %s" % c.base_url)
    print("本机端口     : mc=%d  mux=%d  http=%d" % (c.mc_port, c.mux_port, c.http_port))
    print("资源来源     : %s" % c.source_pack)
    print("发布目录     : %s" % c.web_dir)
    bad = c.problems()
    print("检查         : %s" % ("OK" if not bad else ""))
    for b in bad:
        print("  ! %s" % b)
