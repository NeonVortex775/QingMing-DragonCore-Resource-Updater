# -*- coding: utf-8 -*-
"""
青冥 开服链路自检（纯 ASCII 输出，避免任何控制台编码问题）

  1. 本地资源服务  127.0.0.1:<http_port>/dc/version.txt
  2. 本机 MC       relay 模式对比「分流器 vs 直连」，direct 模式只确认 MC 有响应
  3. 公网资源      <公网地址>:<端口>/dc/version.txt
  4. 公网 MC       relay 走共用端口；direct 走 MC 自己的端口

用法：python dc_verify.py [--public-host <域名或IP>] [--public-port <端口>]
"""

import json
import socket
import sys
import time

try:
    from dc_config import load as _load_cfg
except ImportError:                       # 作为包/模块导入时
    from .dc_config import load as _load_cfg

_CFG = _load_cfg()

LOCAL_HTTP = ("127.0.0.1", _CFG.http_port)
LOCAL_MC = ("127.0.0.1", _CFG.mc_port)
LOCAL_MUX = ("127.0.0.1", _CFG.mux_port)
PUBLIC = (_CFG.host, _CFG.public_http_port)   # 公网资源地址
PUBLIC_MC = (_CFG.host, _CFG.mc_port)         # 公网 MC 地址（direct 模式用）
DIRECT = _CFG.is_direct
CHECK_MC_PUBLIC = True


def _varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _read_varint(sock):
    n = 0
    shift = 0
    while True:
        b = sock.recv(1)
        if not b:
            return None
        n |= (b[0] & 0x7F) << shift
        shift += 7
        if not (b[0] & 0x80):
            return n


def http_get(host, port, path, timeout=8):
    """裸 socket 发 HTTP，绕开系统代理"""
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        req = "GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % (path, host)
        s.sendall(req.encode("ascii"))
        buf = b""
        while True:
            d = s.recv(4096)
            if not d:
                break
            buf += d
    finally:
        s.close()
    head, _, body = buf.partition(b"\r\n\r\n")
    code = 0
    parts = head.split(b" ")
    if len(parts) >= 2 and parts[1].isdigit():
        code = int(parts[1])
    return code, body


def mc_status(host, port, timeout=10):
    """MC 1.7+ 状态查询。注意 server address 前必须带 varint 长度前缀"""
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        h = host.encode()
        payload = (_varint(0) + _varint(340) + _varint(len(h)) + h
                   + (port & 0xFFFF).to_bytes(2, "big") + _varint(1))
        s.sendall(_varint(len(payload)) + payload)
        s.sendall(b"\x01\x00")
        ln = _read_varint(s)
        if not ln:
            return None
        data = s.recv(ln)
    finally:
        s.close()

    i = 0
    while data[i] & 0x80:
        i += 1
    i += 1
    j = i
    ln2 = 0
    shift = 0
    while True:
        b = data[j]
        ln2 |= (b & 0x7F) << shift
        shift += 7
        j += 1
        if not (b & 0x80):
            break
    txt = data[j:j + ln2].decode("utf-8", "replace")
    return json.loads(txt)


def _retry(fn, times=4, delay=3.0):
    """中转客户端刚启动时隧道可能还没建好，公网检查给几次机会"""
    last = None
    for i in range(times):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < times - 1:
                time.sleep(delay)
    raise last


def summarize(status):
    if not status:
        return "no response"
    ver = (status.get("version") or {}).get("name", "?")
    pl = status.get("players") or {}
    return "%s  online %s/%s" % (ver, pl.get("online", "?"), pl.get("max", "?"))


def main():
    host, port = PUBLIC
    join_addr = "%s:%d" % (PUBLIC_MC if DIRECT else PUBLIC)   # 玩家进服地址
    for i, a in enumerate(sys.argv):
        if a == "--public-host" and i + 1 < len(sys.argv):
            host = sys.argv[i + 1]
        if a == "--public-port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])

    rows = []
    t_all = time.time()

    # ---- 1. 本地资源服务 ----
    t0 = time.time()
    try:
        code, body = http_get(*LOCAL_HTTP, "/dc/version.txt", timeout=5)
        ver = body.strip().decode("ascii", "replace")
        ok = code == 200 and len(ver) >= 8
        rows.append(("local http %d" % LOCAL_HTTP[1], ok, "%s  version=%s  %.2fs"
                     % (code, ver or "-", time.time() - t0)))
    except Exception as e:
        rows.append(("local http %d" % LOCAL_HTTP[1], False, "%s: %s" % (type(e).__name__, e)))

    # ---- 2. 本机 MC（relay 模式同时对比分流器通道）----
    t0 = time.time()
    try:
        direct = mc_status(*LOCAL_MC, timeout=8)
        if DIRECT:
            ok = bool(direct)
            rows.append(("local mc %d" % LOCAL_MC[1], ok, "%s  %.2fs"
                         % (summarize(direct), time.time() - t0)))
        else:
            muxed = mc_status(*LOCAL_MUX, timeout=8)
            ok = bool(direct) and bool(muxed) and direct == muxed
            rows.append(("local mux %d mc" % LOCAL_MUX[1], ok, "%s  %.2fs"
                         % (summarize(muxed), time.time() - t0)))
    except Exception as e:
        label = "local mc %d" % LOCAL_MC[1] if DIRECT else "local mux %d mc" % LOCAL_MUX[1]
        rows.append((label, False, "%s: %s" % (type(e).__name__, e)))

    # ---- 3. 公网：资源 ----
    t0 = time.time()
    try:
        code, body = _retry(lambda: http_get(host, port, "/dc/version.txt", timeout=10))
        ver = body.strip().decode("ascii", "replace")
        ok = code == 200 and len(ver) >= 8
        rows.append(("public http %s" % host, ok, "%s  version=%s  %.2fs"
                     % (code, ver or "-", time.time() - t0)))
    except Exception as e:
        rows.append(("public http %s" % host, False, "%s: %s" % (type(e).__name__, e)))

    # ---- 4. 公网：MC 状态 ----
    t0 = time.time()
    mc_host, mc_port = (PUBLIC_MC if DIRECT else PUBLIC)
    try:
        if CHECK_MC_PUBLIC:
            st = _retry(lambda: mc_status(mc_host, mc_port, timeout=10), times=3, delay=2.0)
            rows.append(("public mc %s:%d" % (mc_host, mc_port), bool(st),
                         "%s  %.2fs" % (summarize(st), time.time() - t0)))
    except Exception as e:
        rows.append(("public mc %s:%d" % (mc_host, mc_port), False,
                     "%s: %s" % (type(e).__name__, e)))

    print("")
    print("  %-26s %-6s %s" % ("CHECK", "RESULT", "DETAIL"))
    print("  " + "-" * 68)
    for name, ok, detail in rows:
        print("  %-26s %-6s %s" % (name, "PASS" if ok else "FAIL", detail))
    print("  " + "-" * 68)

    good = sum(1 for r in rows if r[1])
    print("  %d/%d passed   total %.2fs" % (good, len(rows), time.time() - t_all))
    if good == len(rows):
        print("  ALL GREEN - players can join %s" % join_addr)
    else:
        print("  Something is down. Check the 3 minimized windows.")
        for name, ok, detail in rows:
            if not ok:
                print("    -> %s : %s" % (name, detail))
    return 0 if good == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
