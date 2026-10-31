# -*- coding: utf-8 -*-
"""重新组装可分发 zip（在仓库根目录执行）

    python build/打包.py

产物：dist/青冥资源更新包.zip
"""
import io
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NAME = "青冥资源更新包"
OUT = os.path.join(ROOT, "dist", NAME)
ZIP = os.path.join(ROOT, "dist", NAME + ".zip")


def die(msg):
    raise SystemExit("!! " + msg)


def crlf_ascii(p):
    with io.open(p, "rb") as f:
        d = f.read()
    try:
        d.decode("ascii")
    except UnicodeDecodeError:
        die("%s 含非 ASCII 字符（bat 里不能有中文）" % os.path.basename(p))
    with io.open(p, "wb") as f:
        f.write(d.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))


def copy(src, dst):
    d = os.path.dirname(dst)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    shutil.copy2(src, dst)


def read_ini_value(txt, sec, key):
    cur = None
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip().lower()
            continue
        if cur == sec and "=" in s and not s.startswith((";", "#")):
            k, v = s.split("=", 1)
            if k.strip().lower() == key:
                return v.split(";")[0].strip()
    return ""


def build():
    # 语法闸门
    import py_compile
    sdir = os.path.join(ROOT, "server")
    for fn in sorted(os.listdir(sdir)):
        if fn.endswith(".py"):
            try:
                py_compile.compile(os.path.join(sdir, fn), cfile=None, doraise=True)
            except Exception as e:
                die("%s 语法错误：%s" % (fn, e))

    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    for fn in ("config.ini", "教程.md", "快速开始.txt", "README.md"):
        copy(os.path.join(ROOT, fn), os.path.join(OUT, fn))

    for fn in ("一键启动.bat", "停止.bat"):
        copy(os.path.join(ROOT, fn), os.path.join(OUT, fn))
        crlf_ascii(os.path.join(OUT, fn))

    for fn in sorted(os.listdir(sdir)):
        if fn.endswith(".py"):
            copy(os.path.join(sdir, fn), os.path.join(OUT, "server", fn))

    cdir = os.path.join(OUT, "client")
    copy(os.path.join(ROOT, "client", "QingMingUpdater-1.0.jar"),
         os.path.join(cdir, "QingMingUpdater-1.0.jar"))
    copy(os.path.join(ROOT, "client", "使用说明.txt"), os.path.join(cdir, "使用说明.txt"))

    with io.open(os.path.join(ROOT, "config.ini"), encoding="utf-8-sig") as f:
        ini = f.read()
    addr = read_ini_value(ini, "client", "base_url")
    if not addr:
        host = read_ini_value(ini, "network", "public_host") or "你的域名"
        port = read_ini_value(ini, "network", "public_port") or "端口"
        addr = "http://%s:%s/dc/" % (host, port)
    if not addr.endswith("/"):
        addr += "/"
    with io.open(os.path.join(cdir, "qingming-updater.txt"), "w",
                 encoding="utf-8", newline="\r\n") as f:
        f.write("# 青冥 资源更新地址\n"
                "# 复制本文件到玩家客户端 .minecraft/config/qingming-updater.txt\n"
                "# 换服务器只改下面这一行\n%s\n" % addr)

    exe = os.path.join(ROOT, "relay", "frpc.exe")
    if os.path.isfile(exe):
        copy(exe, os.path.join(OUT, "relay", "frpc.exe"))
        lic = os.path.join(ROOT, "relay", "LICENSE")
        if os.path.isfile(lic):
            copy(lic, os.path.join(OUT, "relay", "LICENSE"))
    else:
        print("   注意：relay/frpc.exe 不存在，zip 里不会有中转客户端")
        print("        用 frp 的话，下载 https://github.com/fatedier/frp/releases 的 windows_amd64 版本放进去")
    if not os.path.isdir(os.path.join(OUT, "relay")):
        os.makedirs(os.path.join(OUT, "relay"))
    readme = os.path.join(ROOT, "relay", "README.md")
    if os.path.isfile(readme):
        copy(readme, os.path.join(OUT, "relay", "说明.txt"))
    else:
        with io.open(os.path.join(OUT, "relay", "说明.txt"), "w",
                     encoding="utf-8", newline="\r\n") as f:
            f.write("mode = relay 时把中转客户端（如 frpc.exe）放进本目录；\n"
                    "启动器会按 ../config.ini 自动生成配置并调用。\n"
                    "mode = direct（VPS 直连）时本目录用不上。\n")

    if os.path.isfile(ZIP):
        os.remove(ZIP)
    n = 0
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for dirpath, dirnames, filenames in os.walk(OUT):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                z.write(full, os.path.relpath(full, os.path.join(ROOT, "dist")).replace("\\", "/"))
                n += 1

    print("打包完成：%s" % ZIP)
    print("  %d 个文件 / %.2f MB" % (n, os.path.getsize(ZIP) / 1048576.0))
    print("  玩家地址：%s" % addr)
    return 0


if __name__ == "__main__":
    sys.exit(build())
