#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""青冥 · 客户端资源同步服务端工具。

把客户端 resourcepacks/DragonCore 整包镜像到 webroot/dc/，生成带 sha1 的 manifest.json，
供客户端 coremod 做增量更新（缺什么下什么）。

用法：
    python dc_sync_server.py            # 等价于 sync
    python dc_sync_server.py plan       # 只列表，不复制（看清要推多少再动手）
    python dc_sync_server.py sync       # 镜像 + 清理多余文件 + 重建 manifest
    python dc_sync_server.py serve      # 起 HTTP 静态服务（frp 指向这里）
    python dc_sync_server.py sync serve # 先同步再服务（最常用）
    python dc_sync_server.py sync --all # 取消单文件体积上限（默认 64MB）
    python dc_sync_server.py sync --max-mb 200

对外地址形如  http://<域名>:<端口>/dc/manifest.json
"""
import hashlib
import io
import json
import os
import shutil
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# ── 路径（全部来自同目录 config.ini）─────────────────────
try:
    from dc_config import load as _load_cfg
except ImportError:                       # 作为包/模块导入时
    from .dc_config import load as _load_cfg

CFG = _load_cfg()
SRC = CFG.source_pack                     # 资源来源：客户端 resourcepacks/DragonCore
WEB = CFG.webroot                         # 发布目录：服务端 webroot
SUB = CFG.sub                             # 对外路径前缀 /dc/
PORT = CFG.http_port                      # 本机 HTTP 端口

# 永不推送：本机状态文件、体积巨大且客户端自带的分发包
DENY_NAMES = {
    ".dcsync.json", ".dcsync.log",      # 客户端更新器的状态/日志，推过去会死循环
    "manifest.json", "version.txt",     # 服务端自身产物
    ".sha1cache.json",
    "Resource.zip",                     # 693MB，必须随客户端一起发
    "Thumbs.db", "desktop.ini",
}
DENY_PREFIX = ("_bak", "_preview", ".git", "~$", ".sha1")
DENY_EXT = (".bak", ".tmp", ".dctmp", ".part", ".log.bak")

# 单文件体积上限（MB）。超过就跳过并告警，避免误推大文件；--all 取消。
MAX_FILE_MB = 64
# 体积前列显示条数
TOP_N = 15

CACHE = os.path.join(WEB, ".sha1cache.json")
MANIFEST_NAME = "manifest.json"


# ── 工具 ────────────────────────────────────────────────
def load_cache():
    try:
        with io.open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(c):
    os.makedirs(WEB, exist_ok=True)
    with io.open(CACHE, "w", encoding="utf-8") as f:
        json.dump(c, f)


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def denied_file(name):
    if name in DENY_NAMES:
        return "保留名"
    if name.startswith(DENY_PREFIX):
        return "临时/备份"
    if name.lower().endswith(DENY_EXT):
        return "临时/备份"
    return None


def denied_dir(name):
    if name.startswith(DENY_PREFIX):
        return "临时/备份"
    return None


def mb(n):
    return n / 1048576.0


# ── 收集 ────────────────────────────────────────────────
def collect(max_mb, quiet=False):
    """整包扫描。返回 (files, skipped)：
    files   = [(相对路径, 绝对路径, 大小字节)]
    skipped = [(相对路径, 大小字节, 原因)]
    """
    files, skipped = [], []
    limit = max_mb * 1048576.0

    for name in sorted(os.listdir(SRC)):
        p = os.path.join(SRC, name)
        if os.path.isfile(p):
            why = denied_file(name)
            if why:
                skipped.append((name, os.path.getsize(p), why))
                continue
            size = os.path.getsize(p)
            if size > limit:
                skipped.append((name, size, "超 %.0fMB" % max_mb))
                continue
            files.append((name, p, size))
            continue

        if not os.path.isdir(p):
            continue
        why = denied_dir(name)
        if why:
            skipped.append((name + "/", 0, why))
            continue

        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = [d for d in dirnames if not denied_dir(d)]
            for fn in filenames:
                if denied_file(fn):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                if size > limit:
                    skipped.append((os.path.relpath(full, SRC).replace("\\", "/"), size,
                                    "超 %.0fMB" % max_mb))
                    continue
                rel = os.path.relpath(full, SRC).replace("\\", "/")
                files.append((rel, full, size))

    files.sort(key=lambda x: x[0])
    return files, skipped


def top_entries():
    """每个顶层条目的 (名称, 字节, 文件数)，用于 plan 展示。"""
    out = []
    for name in sorted(os.listdir(SRC)):
        p = os.path.join(SRC, name)
        if os.path.isfile(p):
            out.append((name, os.path.getsize(p), 1))
            continue
        if not os.path.isdir(p):
            continue
        b = n = 0
        for dp, dn, fn in os.walk(p):
            for f in fn:
                try:
                    b += os.path.getsize(os.path.join(dp, f))
                    n += 1
                except OSError:
                    pass
        out.append((name + "/", b, n))
    out.sort(key=lambda x: -x[1])
    return out


# ── plan ────────────────────────────────────────────────
def plan(max_mb):
    files, skipped = collect(max_mb)
    total = sum(f[2] for f in files)

    denied = {s[0] for s in skipped if s[2] in ("保留名", "临时/备份")}
    oversize = {}
    for rel, b, why in skipped:
        if "超" in why:
            top = rel.split("/")[0]
            oversize[top] = oversize.get(top, 0) + 1

    print("将推送 %d 个文件 / %.1f MB   （单文件上限 %.0f MB）"
          % (len(files), mb(total), max_mb))
    print("")
    print("  %-28s %9s %8s  %s" % ("顶层条目", "MB", "文件数", "状态"))
    print("  " + "-" * 64)
    for name, b, n in top_entries():
        key = name.rstrip("/")
        if name in denied or key in denied:
            state = "跳过（%s）" % next((s[2] for s in skipped if s[0] in (name, key)), "")
        elif oversize.get(key):
            state = "部分跳过 %d 个超大" % oversize[key]
        else:
            state = "推送"
        print("  %-28s %9.1f %8d  %s" % (name, mb(b), n, state))

    if skipped:
        print("")
        print("  跳过明细（前 10 条）：")
        for rel, b, why in skipped[:10]:
            print("    %-40s %8.1f MB  %s" % (rel, mb(b), why))
        if len(skipped) > 10:
            print("    ... 共 %d 条" % len(skipped))
    return files


# ── sync ────────────────────────────────────────────────
def sync(max_mb):
    files, skipped = collect(max_mb)
    total = sum(f[2] for f in files)
    print("待镜像 %d 个文件 / %.1f MB" % (len(files), mb(total)))
    if skipped:
        for rel, b, why in skipped[:5]:
            print("  ! 跳过 %-40s %.1fMB  %s" % (rel, mb(b), why))

    cache = load_cache()
    dst_root = os.path.join(WEB, SUB)
    os.makedirs(dst_root, exist_ok=True)

    manifest = {}
    copied = copy_bytes = skipped_copy = 0
    t0 = time.time()

    for rel, src, size in files:
        st = os.stat(src)
        c = cache.get(rel)
        if c and c.get("mtime") == int(st.st_mtime) and c.get("size") == size:
            digest = c["sha1"]
        else:
            digest = sha1_of(src)
            cache[rel] = {"mtime": int(st.st_mtime), "size": size, "sha1": digest}
        manifest[rel] = {"sha1": digest, "size": size}

        dst = os.path.join(dst_root, rel.replace("/", os.sep))
        # 目标已一致就不再复制（整包 190MB 时这一条能省掉几十秒）
        if os.path.isfile(dst) and os.path.getsize(dst) == size:
            skipped_copy += 1
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
        copy_bytes += size

    save_cache(cache)

    # 清理 webroot/dc 下已从源包删除的文件（否则玩家还会继续下旧的）
    # 默认「移到 webroot/_pruned/<时间戳>/」而不是删掉：源包里已经删了的情况下，
    # 这里往往是全世界最后一份副本，直接删就找不回来了。--purge 才真删。
    keep = set(manifest.keys())
    pruned = pruned_bytes = 0
    purge = "--purge" in sys.argv
    bak_root = os.path.join(WEB, "_pruned", time.strftime("%Y%m%d-%H%M%S"))
    for dirpath, dirnames, filenames in os.walk(dst_root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, dst_root).replace("\\", "/")
            if rel == MANIFEST_NAME or rel == "version.txt":
                continue
            if rel not in keep:
                try:
                    pruned_bytes += os.path.getsize(full)
                    if purge:
                        os.remove(full)
                    else:
                        dst_bak = os.path.join(bak_root, rel.replace("/", os.sep))
                        os.makedirs(os.path.dirname(dst_bak), exist_ok=True)
                        shutil.move(full, dst_bak)
                    pruned += 1
                except OSError:
                    pass
    # 顺手删空目录
    for dirpath, dirnames, filenames in os.walk(dst_root, topdown=False):
        if dirpath == dst_root:
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass

    # 版本号 = manifest 内容指纹，内容不变则版本不变
    body = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    ver = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
    doc = {"version": ver, "gen": time.strftime("%Y-%m-%d %H:%M:%S"), "files": manifest}

    with io.open(os.path.join(dst_root, MANIFEST_NAME), "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    with io.open(os.path.join(dst_root, "version.txt"), "w", encoding="utf-8") as f:
        f.write(ver)

    print("版本 %s | %d 个文件 | %.1f MB" % (ver, len(manifest), mb(total)))
    print("本次复制 %d 个 (%.1f MB)，已是同版本跳过 %d 个，清理旧文件 %d 个 (%.1f MB)"
          % (copied, mb(copy_bytes), skipped_copy, pruned, mb(pruned_bytes)))
    if pruned and not purge:
        print("清理的文件已移到 %s（确认不要了用 --purge 重跑才会真删）" % bak_root)
    print("耗时 %.1fs" % (time.time() - t0))
    print("对外： http://<你的frp域名>:<端口>/%s/manifest.json" % SUB)
    return ver


# ── rebuild ─────────────────────────────────────────────
def rebuild():
    """不复制文件，只按 webroot/dc 现有内容重建 manifest（手工改过 webroot 后使用）。"""
    root = os.path.join(WEB, SUB)
    manifest = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not denied_dir(d)]
        for fn in filenames:
            if fn in (MANIFEST_NAME, "version.txt") or denied_file(fn):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            manifest[rel] = {"sha1": sha1_of(full), "size": os.path.getsize(full)}

    body = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    ver = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
    doc = {"version": ver, "gen": time.strftime("%Y-%m-%d %H:%M:%S"), "files": manifest}
    with io.open(os.path.join(root, MANIFEST_NAME), "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    with io.open(os.path.join(root, "version.txt"), "w", encoding="utf-8") as f:
        f.write(ver)
    print("重建完成 版本 %s | %d 个文件" % (ver, len(manifest)))
    return ver


# ── HTTP ────────────────────────────────────────────────
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def do_GET(self):
        # 只放行 /dc/ 下的具体文件，其余一律 404（不暴露 webroot 其他内容）
        if not self.path.startswith("/" + SUB + "/"):
            return self.send_error(404, "Not Found")
        super().do_GET()

    def list_directory(self, path):
        # 不给目录列表：防止被人扫出全部文件名批量扒包
        self.send_error(403, "Forbidden")
        return None

    def log_message(self, fmt, *args):
        # 正常下载不刷屏，只记异常
        code = args[1] if len(args) > 1 else ""
        if str(code).startswith("2"):
            return
        sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


def serve():
    print("静态服务 0.0.0.0:%d  根目录 %s" % (PORT, WEB))
    print("本机自测: http://127.0.0.1:%d/%s/manifest.json" % (PORT, SUB))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


# ── main ────────────────────────────────────────────────
def main():
    args = sys.argv[1:] or ["sync"]

    max_mb = MAX_FILE_MB
    if "--all" in args:
        max_mb = 1024 * 16
    if "--max-mb" in args:
        i = args.index("--max-mb")
        if i + 1 < len(args):
            max_mb = float(args[i + 1])

    if "plan" in args:
        plan(max_mb)
        return
    if "rebuild" in args:
        rebuild()
    if "sync" in args or not ({"serve", "rebuild"} & set(args)):
        sync(max_mb)
    if "serve" in args:
        serve()


if __name__ == "__main__":
    main()
