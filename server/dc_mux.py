# -*- coding: utf-8 -*-
"""
青冥 —— 单端口分流器

一个问题：内网穿透服务通常只给一个公网端口，但它已经被 MC 服务器(25565)占了，
资源分发还需要一个 HTTP 端口。多数免费穿透不放行第二个端口。

解法：在这一个端口上做「首包分流」。
    中转客户端 : local_port = 25566  ->  公网共用端口
    本程序 : 监听 25566，看客户端发来的第一个数据包是什么
             - 以 HTTP 方法开头 (GET / HEAD / POST / ...)  -> 转发到 8080（资源服务）
             - 其它（MC 握手是 VarInt 包长，不会是 ASCII 方法名）-> 转发到 25565（MC 服务器）

玩家侧完全无感：服务器地址不变，不需要改。
资源更新走 http://<公网地址>:<共用端口>/dc/  ，也在同一个端口。

用法：
    python dc_mux.py            启动（前台，Ctrl+C 停）
    python dc_mux.py --check    只做连通性自检，不常驻
"""
from __future__ import print_function

import argparse
import socket
import sys
import threading
import time

try:
    from dc_config import load as _load_cfg
except ImportError:                       # 作为包/模块导入时
    from .dc_config import load as _load_cfg

_CFG = _load_cfg()

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = _CFG.mux_port              # 中转客户端的本地端口指到这里
MC_TARGET = ("127.0.0.1", _CFG.mc_port)      # 真正的 MC 服务器
WEB_TARGET = ("127.0.0.1", _CFG.http_port)   # 资源 HTTP 服务

# HTTP/1.x 请求行都以这些方法名开头；HTTP/2 前奏是 "PRI * HTTP/2.0"
HTTP_PREFIXES = (
    b"GET ", b"HEAD", b"POST", b"PUT ", b"DELE", b"OPTI",
    b"CONN", b"TRAC", b"PATC", b"PRI ",
)

# 分流器只握 32 字节：MC 1.12 握手包通常 16 字节左右，一次读完就不用拆分补发
SNIFF_BYTES = 32
SNIFF_TIMEOUT = 10.0      # 客户端连上后迟迟不发数据就断开（MC/浏览器都会立刻发）
CONNECT_TIMEOUT = 6.0
BUF = 65536

VERBOSE = False           # --verbose 时打印每条连接的首包，用于排查路由错误

_stop = threading.Event()
_stats = {"mc": 0, "web": 0, "reject": 0}
_slock = threading.Lock()


def _log(msg):
    sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


def _is_http(head):
    for p in HTTP_PREFIXES:
        if head[:len(p)] == p:
            return True
    return False


def _duplex(a, b):
    """把 a<->b 双向转发，任一方向 EOF 就断开两端。"""
    def pump(src, dst):
        try:
            while True:
                d = src.recv(BUF)
                if not d:
                    break
                dst.sendall(d)
        except (OSError, socket.error):
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except (OSError, socket.error):
                pass

    t1 = threading.Thread(target=pump, args=(a, b), daemon=True)
    t2 = threading.Thread(target=pump, args=(b, a), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    for s in (a, b):
        try:
            s.close()
        except (OSError, socket.error):
            pass


def _handle(conn, addr):
    tag = None
    upstream = None
    try:
        conn.settimeout(SNIFF_TIMEOUT)
        head = b""
        try:
            # 必须凑够 4 字节才能可靠区分「HTTP 方法」和「MC 的 VarInt 包长」，
            # 否则 TCP 分片时 "GE" 会被误判成 MC 流量。
            while len(head) < SNIFF_BYTES:
                d = conn.recv(SNIFF_BYTES - len(head))
                if not d:
                    break
                head += d
                if len(head) >= 4:
                    break
                # 老式 ping 只发 1 字节(0xFE)，别死等
                conn.settimeout(0.25)
        except socket.timeout:
            pass
        except (OSError, socket.error):
            pass

        if not head:
            with _slock:
                _stats["reject"] += 1
            return

        if _is_http(head):
            target, tag = WEB_TARGET, "web"
        else:
            target, tag = MC_TARGET, "mc"
        if VERBOSE:
            _log("<- %-15s %-4s 首包 %r" % (addr[0], tag, head[:24]))

        try:
            upstream = socket.create_connection(target, timeout=CONNECT_TIMEOUT)
        except (OSError, socket.error) as e:
            _log("%s -> %s:%d 连接失败 %s（后端没起来？）" % (tag, target[0], target[1], e))
            with _slock:
                _stats["reject"] += 1
            return

        conn.settimeout(None)
        upstream.settimeout(None)
        upstream.sendall(head)          # 嗅探掉的字节必须补发
        with _slock:
            _stats[tag] += 1
        _duplex(conn, upstream)
        upstream = None
    except Exception as e:
        _log("处理 %s 异常: %r" % (addr[0], e))
    finally:
        for s in (conn, upstream):
            if s is not None:
                try:
                    s.close()
                except (OSError, socket.error):
                    pass


def serve():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((LISTEN_HOST, LISTEN_PORT))
    except (OSError, socket.error) as e:
        _log("监听 %s:%d 失败: %s" % (LISTEN_HOST, LISTEN_PORT, e))
        _log("（是不是中转客户端的本地端口还没指到 %d，而旧进程还占着？）" % LISTEN_PORT)
        return 1
    srv.listen(64)
    _log("分流器已启动  0.0.0.0:%d" % LISTEN_PORT)
    _log("  Minecraft   首包非 HTTP  -> %s:%d" % MC_TARGET)
    _log("  资源 HTTP   首包 GET/POST -> %s:%d" % WEB_TARGET)
    _log("请把中转客户端的本地端口设为 %d，映射到公网共用端口" % LISTEN_PORT)

    def watchdog():
        while not _stop.wait(300):
            with _slock:
                s = dict(_stats)
            _log("累计  玩家连接 %d  |  资源请求 %d  |  丢弃 %d"
                 % (s["mc"], s["web"], s["reject"]))

    threading.Thread(target=watchdog, daemon=True).start()

    try:
        while not _stop.is_set():
            try:
                conn, addr = srv.accept()
            except (OSError, socket.error):
                break
            threading.Thread(target=_handle, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        _log("收到 Ctrl+C，正在停止…")
    finally:
        _stop.set()
        try:
            srv.close()
        except (OSError, socket.error):
            pass
    return 0


# ---------------- 自检 ----------------

def _varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _read_varint(sock):
    n, sh = 0, 0
    while True:
        b = sock.recv(1)
        if not b:
            return None
        n |= (b[0] & 0x7F) << sh
        sh += 7
        if not (b[0] & 0x80):
            return n


def check(host="127.0.0.1", port=LISTEN_PORT):
    ok = True

    sys.stdout.write("[1/2] HTTP 分流 … ")
    try:
        s = socket.create_connection((host, port), timeout=6)
        s.sendall(b"GET /dc/version.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        data = b""
        while True:
            d = s.recv(4096)
            if not d:
                break
            data += d
        s.close()
        body = data.split(b"\r\n\r\n", 1)[-1].strip()
        if data.startswith(b"HTTP/1.") and b"200" in data.split(b"\r\n", 1)[0]:
            sys.stdout.write("OK  版本=%s\n" % body.decode("utf-8", "replace"))
        else:
            sys.stdout.write("失败  响应=%r\n" % data[:80])
            ok = False
    except Exception as e:
        sys.stdout.write("失败  %r\n" % (e,))
        ok = False

    sys.stdout.write("[2/2] MC 分流   … ")
    try:
        s = socket.create_connection((host, port), timeout=8)
        hst = host.encode()
        # handshake: id=0x00, proto=340, String(host), ushort(port), next_state=1(status)
        # 注意 String 前面必须带 varint 长度，漏了服务器会解析错位直接断开
        payload = (_varint(0) + _varint(340) + _varint(len(hst)) + hst
                   + (port & 0xFFFF).to_bytes(2, "big") + _varint(1))
        s.sendall(_varint(len(payload)) + payload)
        # status request: len=1, id=0x00
        s.sendall(b"\x01\x00")
        ln = _read_varint(s)
        if not ln:
            raise IOError("对端无响应")
        data = s.recv(ln)
        s.close()
        txt = data[1:].decode("utf-8", "replace")
        sys.stdout.write("OK  %s\n" % txt[:160])
    except Exception as e:
        sys.stdout.write("失败  %r\n" % (e,))
        ok = False

    sys.stdout.write("\n%s\n" % ("分流器两条通道都正常" if ok else "有通道不通，看上面的错误"))
    return 0 if ok else 1


def main():
    global LISTEN_PORT, MC_TARGET, WEB_TARGET, VERBOSE
    ap = argparse.ArgumentParser(description="青冥 单端口分流器（MC + HTTP 共用一个公网端口）")
    ap.add_argument("--check", action="store_true", help="自检两条通道是否能走通")
    ap.add_argument("--say", action="store_true", help="不做事，只打印用法")
    ap.add_argument("--verbose", action="store_true", help="打印每条连接的首包，排查路由问题")
    ap.add_argument("--port", type=int, default=LISTEN_PORT, help="分流器监听端口（默认 25566）")
    ap.add_argument("--mc-port", type=int, default=MC_TARGET[1], help="MC 服务器端口（默认 25565）")
    ap.add_argument("--web-port", type=int, default=WEB_TARGET[1], help="资源 HTTP 端口（默认 8080）")
    a = ap.parse_args()

    LISTEN_PORT = a.port
    MC_TARGET = (MC_TARGET[0], a.mc_port)
    WEB_TARGET = (WEB_TARGET[0], a.web_port)
    VERBOSE = a.verbose

    if a.say:
        print(__doc__)
        return 0
    if a.check:
        return check(port=LISTEN_PORT)
    return serve()


if __name__ == "__main__":
    code = main()
    _stop.set()
    sys.exit(code)
