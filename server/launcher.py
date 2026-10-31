#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""青冥 · 资源自动更新  一键启动器

    python launcher.py           启动（前台常驻，Ctrl+C 停止）
    python launcher.py start     同上
    python launcher.py stop      停掉上一次启动的进程
    python launcher.py sync      只把资源推上线（改了贴图后跑这个）
    python launcher.py verify    只做全链路自检（4 项）
    python launcher.py check     只读 config.ini 并检查配置是否合法

一个进程里跑两件事（HTTP 静态服务 + 首包分流器），中转客户端作为子进程。
关掉这个窗口 = 全部停止，不会出现「隧道还在但后面没服务」的半死状态。

两种对外方式（config.ini 的 network.mode）：
    direct  有公网 IP：只跑 HTTP，MC 服务自己对外，不需要分流器与中转
    relay   没有公网 IP：HTTP 和 MC 共用同一个公网端口，靠首包分流区分
"""
from __future__ import print_function

import json
import os
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import dc_config

PY = sys.executable
FAR = "\n" + "=" * 62


# ── 小工具 ──────────────────────────────────────────────
def log(msg):
    sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


def title(msg):
    sys.stdout.write(FAR + "\n " + msg + "\n" + "=" * 62 + "\n")
    sys.stdout.flush()


def port_busy(port, host="0.0.0.0"):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return False
    except (OSError, socket.error):
        return True
    finally:
        s.close()


def proc_name(pid):
    """Windows 下取进程映像名，避免 PID 回收被误杀"""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return "python"
        except Exception:
            return None
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL)
        txt = out.decode("mbcs", "replace")
        if "No tasks" in txt or "," not in txt:
            return None
        return txt.split(",")[0].strip('"').lower()
    except Exception:
        return None


def kill_pid(pid):
    name = proc_name(pid)
    if not name:
        return False
    if name not in ("python.exe", "pythonw.exe", "py.exe", "frpc.exe",
                    "relay-client.exe"):
        return False                      # 不是我们的进程，坚决不动
    try:
        subprocess.call(["taskkill", "/F", "/PID", str(pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


# ── 上次运行的清理 ──────────────────────────────────────
def pid_file(cfg):
    return os.path.join(cfg.run_dir, "pids.json")


def stop_all(cfg, quiet=False):
    p = pid_file(cfg)
    killed = 0
    if os.path.isfile(p):
        try:
            with open(p) as f:
                d = json.load(f)
        except Exception:
            d = {}
        for key in ("frpc", "main"):
            pid = d.get(key)
            if pid and int(pid) != os.getpid() and kill_pid(int(pid)):
                killed += 1
        try:
            os.remove(p)
        except OSError:
            pass
    if not quiet:
        log("清理上一次进程：%s" % ("已结束 %d 个" % killed if killed else "无残留"))
    if killed:
        time.sleep(1.0)
    return killed


def save_pids(cfg, frpc_pid):
    try:
        with open(pid_file(cfg), "w") as f:
            json.dump({"main": os.getpid(), "frpc": frpc_pid}, f)
    except Exception:
        pass


# ── 服务启动 ────────────────────────────────────────────
def start_http(cfg):
    from http.server import ThreadingHTTPServer
    import dc_sync_server
    srv = ThreadingHTTPServer(("0.0.0.0", cfg.http_port), dc_sync_server.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5},
                     daemon=True).start()
    log("资源 HTTP 已启动  0.0.0.0:%d  ->  %s"
        % (cfg.http_port, cfg.web_dir.replace("\\", "/")))
    return srv


def start_mux(cfg):
    import dc_mux
    dc_mux.LISTEN_PORT = cfg.mux_port
    dc_mux.MC_TARGET = ("127.0.0.1", cfg.mc_port)
    dc_mux.WEB_TARGET = ("127.0.0.1", cfg.http_port)
    threading.Thread(target=dc_mux.serve, daemon=True).start()
    time.sleep(0.4)
    log("分流器已启动    0.0.0.0:%d  ->  mc:%d / http:%d"
        % (cfg.mux_port, cfg.mc_port, cfg.http_port))
    return dc_mux


def relay_client_path():
    """中转客户端（如果你用 frp 做内网穿透，把 frpc.exe 放进 relay/ 目录即可）"""
    for rel in ("relay/relay-client.exe", "relay/frpc.exe",
                "../relay/frpc.exe", "relay-client.exe", "frpc.exe"):
        p = os.path.normpath(os.path.join(HERE, rel))
        if os.path.isfile(p):
            return p
    return None


def start_relay(cfg):
    exe = relay_client_path()
    if not exe:
        log("！relay/ 目录下没有中转客户端，外网连不进来")
        log("  如果你用 frp 做内网穿透：去 frp 项目 releases 下载 windows_amd64 的 frpc.exe，")
        log("  放到 relay/ 目录后重新启动。用云服务器直连的话请在 config.ini 把 mode 改成 direct")
        return None
    ini = cfg.write_relay_ini()
    logdir = os.path.join(cfg.run_dir, "logs")
    if not os.path.isdir(logdir):
        os.makedirs(logdir)
    logf = open(os.path.join(logdir, "relay.log"), "ab")
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)                  # 本机代理会让中转客户端连不上服务端
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000   # CREATE_NO_WINDOW
    p = subprocess.Popen([exe, "-c", ini], cwd=os.path.dirname(exe),
                         stdout=logf, stderr=subprocess.STDOUT, env=env, **kw)
    log("中转客户端已启动  %s:%d  ->  本地:%d   公网端口:%d"
        % (cfg.relay_server, cfg.relay_port, cfg.mux_port, cfg.public_port))
    return p


# 兼容老引用
frpc_path = relay_client_path
start_frpc = start_relay


# ── 客户端配置 ──────────────────────────────────────────
def write_client_cfg(cfg):
    """把最终资源地址写成玩家端可直接用的配置文件"""
    d = os.path.join(ROOT, "client")
    if not os.path.isdir(d):
        return None
    p = os.path.join(d, "qingming-updater.txt")
    body = ("# 青冥 资源更新地址\n"
            "# 复制本文件到玩家客户端 .minecraft/config/qingming-updater.txt\n"
            "# 换服务器只改下面这一行\n"
            "%s\n" % cfg.base_url)
    try:
        with open(p, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(body)
        return p
    except Exception:
        return None


# ── 子命令 ──────────────────────────────────────────────
def cmd_check(cfg):
    print("配置文件   : %s" % (cfg.path or "（未找到，用内置默认值）"))
    if cfg.is_direct:
        print("对外方式   : 公网直连（VPS / 云服务器）")
    else:
        print("对外方式   : 内网穿透（单端口复用 MC 与资源）")
        print("中转服务   : %s:%d  通道=%s"
              % (cfg.relay_server or "?", cfg.relay_port, cfg.relay_token or "-"))
    print("玩家地址   : %s" % cfg.public_addr)
    print("资源地址   : %s" % cfg.base_url)
    print("本机端口   : mc=%d  mux=%d  http=%d" % (cfg.mc_port, cfg.mux_port, cfg.http_port))
    print("资源来源   : %s" % cfg.source_pack)
    print("发布目录   : %s" % cfg.web_dir.replace("\\", "/"))
    if not cfg.is_direct:
        print("中转客户端 : %s" % (relay_client_path() or "未找到（relay/ 目录）"))
    bad = cfg.problems()
    warn = cfg.warnings()
    print("")
    if warn:
        for w in warn:
            print("  [!] %s" % w)
        print("")
    if bad:
        for b in bad:
            print("  [X] %s" % b)
        return 1
    print("  [OK] 配置没有问题")
    return 0


def cmd_sync(cfg):
    import dc_sync_server
    dc_sync_server.SRC = cfg.source_pack
    dc_sync_server.WEB = cfg.webroot
    ver = dc_sync_server.sync(dc_sync_server.MAX_FILE_MB)
    return 0 if ver else 1


def cmd_verify(cfg):
    import dc_verify
    dc_verify.LOCAL_HTTP = ("127.0.0.1", cfg.http_port)
    dc_verify.LOCAL_MC = ("127.0.0.1", cfg.mc_port)
    dc_verify.LOCAL_MUX = ("127.0.0.1", cfg.mux_port)
    dc_verify.PUBLIC = (cfg.host, cfg.public_http_port)
    dc_verify.CHECK_MC_PUBLIC = not cfg.is_direct
    sys.argv = [sys.argv[0]]
    return dc_verify.main()


def cmd_start(cfg):
    title("青冥 · 资源自动更新  启动中")
    print(" 配置文件 : %s" % (cfg.path or "（未找到，用内置默认值）"))
    print(" 玩家地址 : %s" % cfg.public_addr)
    print(" 资源地址 : %s" % cfg.base_url)
    print("")

    bad = cfg.problems()
    if bad:
        print(" 配置有问题，先改 config.ini：")
        for b in bad:
            print("   [X] %s" % b)
        return 1
    for w in cfg.warnings():
        print("  [!] %s" % w)
        print("      （可以继续，但上面这项多半会让公网连不上）")
        print("")

    # 1) 清理上一次
    print("[1/5] 清理上一次的进程")
    stop_all(cfg)

    # 2) 同步资源
    print("[2/5] 同步资源到发布目录")
    if cmd_sync(cfg) != 0:
        print("      资源同步失败，先解决上面的报错")
        return 1

    # 3) 端口
    print("[3/5] 检查端口")
    ports = [("http", cfg.http_port)]
    if not cfg.is_direct:
        ports.append(("mux", cfg.mux_port))
    for name, port in ports:
        if port_busy(port):
            print("   [X] 端口 %d（%s）被别的程序占用" % (port, name))
            print("       要么关掉那个程序，要么改 config.ini 里的 %s_port" % name)
            return 1
        print("   %-5s %d  空闲" % (name, port))

    # 4) 起服务
    print("[4/5] 启动服务")
    try:
        start_http(cfg)
        if not cfg.is_direct:
            start_mux(cfg)
    except Exception as e:
        print("   [X] 启动失败：%r" % (e,))
        return 1
    relay = None if cfg.is_direct else start_relay(cfg)
    save_pids(cfg, relay.pid if relay else 0)
    p = write_client_cfg(cfg)
    if p:
        log("玩家端配置已更新  client/qingming-updater.txt")

    # 5) 自检
    print("[5/5] 全链路自检（公网项最多重试 4 次，约 10 秒）")
    time.sleep(1.5)
    code = cmd_verify(cfg)

    title("服务已在运行 —— 不要关闭这个窗口，最小化即可")
    print(" 玩家在游戏里连接 : %s" % cfg.public_addr)
    print(" 浏览器可验证     : %sversion.txt" % cfg.base_url)
    print(" 停止服务         : 按 Ctrl+C，或双击 停止.bat")
    print("")
    if code != 0:
        print(" 上表有 FAIL，服务仍在运行；修好后在另一个窗口跑：")
        print("     python server\\launcher.py verify")
        print("")
    sys.stdout.flush()

    # 常驻：relay 模式下看着中转客户端，掉了就拉起来
    restarts = 0
    try:
        while True:
            time.sleep(5)
            if relay is not None and relay.poll() is not None:
                restarts += 1
                if restarts > 5:
                    log("中转客户端反复退出（已重启 %d 次），停止重试，看 server/.run/logs/relay.log"
                        % restarts)
                    relay = None
                    continue
                log("中转客户端退出了，第 %d 次重启…" % restarts)
                relay = start_relay(cfg)
                if relay:
                    save_pids(cfg, relay.pid)
    except KeyboardInterrupt:
        print("")
        log("收到 Ctrl+C，正在停止…")
        if relay is not None and relay.poll() is None:
            try:
                relay.terminate()
            except Exception:
                pass
        stop_all(cfg, quiet=True)
        log("已停止")
        return 0


def main():
    action = (sys.argv[1] if len(sys.argv) > 1 else "start").lower()
    try:
        cfg = dc_config.load()
    except RuntimeError as e:
        print(str(e))
        return 1

    if action in ("check", "config", "cfg"):
        return cmd_check(cfg)
    if action == "sync":
        return cmd_sync(cfg)
    if action == "verify":
        return cmd_verify(cfg)
    if action == "stop":
        title("停止服务")
        n = stop_all(cfg)
        print(" 已结束 %d 个进程" % n if n else " 没有发现上次启动的进程")
        return 0
    if action in ("start", "run", "serve"):
        return cmd_start(cfg)
    print(__doc__)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
