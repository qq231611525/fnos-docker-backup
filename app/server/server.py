#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Docker备份 - 飞牛OS原生插件后端
仅使用 Python 标准库，无第三方依赖。
通过 Unix Socket 提供 HTTP 服务，由飞牛统一网关代理 /app/dockerbackup。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from socketserver import UnixStreamServer

# ---------- 路径与环境 ----------
PLUGIN_VERSION = "1.3.4"  # 发版时与 manifest 中的 version 保持一致
APPDEST = os.environ.get("TRIM_APPDEST", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKGVAR = os.environ.get("TRIM_PKGVAR", os.path.join(tempfile.gettempdir(), "dockerbackup"))
SOCKET_PATH = os.environ.get("FNOS_SOCKET_PATH", os.path.join(APPDEST, "app.sock"))
UI_DIR = os.path.join(APPDEST, "ui")
BACKUP_DIR = os.path.join(PKGVAR, "data", "backups")
TASK_DIR = os.path.join(PKGVAR, "data", "tasks")
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(TASK_DIR, exist_ok=True)

DOCKER = shutil.which("docker") or "/usr/bin/docker"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,200}$")

tasks = {}
tasks_lock = threading.Lock()


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def run(cmd, timeout=120):
    """执行命令，返回 (rc, stdout, stderr)"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 127, "", str(e)


def docker_ok():
    rc, out, _ = run([DOCKER, "info", "--format", "{{.ServerVersion}}"], timeout=15)
    return rc == 0, out


def docker_root_dir():
    rc, out, _ = run([DOCKER, "info", "--format", "{{.DockerRootDir}}"], timeout=15)
    if rc == 0 and out:
        return out
    return "/var/lib/docker"


def safe_filename(name):
    base = os.path.basename(name)
    if not SAFE_NAME.match(base):
        return None
    return base


def update_task(tid, **kw):
    with tasks_lock:
        t = tasks.get(tid)
        if t:
            t.update(kw)


# ---------- docker 查询 ----------
def list_containers():
    rc, out, err = run([DOCKER, "ps", "-a", "--format", "{{json .}}"], timeout=30)
    if rc != 0:
        return None, err
    items = []
    for line in out.splitlines():
        try:
            items.append(json.loads(line))
        except Exception:
            pass
    return items, ""


def list_images():
    rc, out, err = run([DOCKER, "images", "--format", "{{json .}}"], timeout=30)
    if rc != 0:
        return None, err
    items = []
    for line in out.splitlines():
        try:
            items.append(json.loads(line))
        except Exception:
            pass
    return items, ""


def volume_usage_map():
    """返回 {卷名: [容器名]} 的映射"""
    usage = {}
    items, _ = list_containers()
    if not items:
        return usage
    for c in items:
        name = (c.get("Names") or "").lstrip("/").split(",")[0].strip()
        if not name:
            continue
        ins, _ = inspect_container(name)
        if not ins:
            continue
        for m in ins.get("Mounts", []) or []:
            if m.get("Type") == "volume" and m.get("Name"):
                usage.setdefault(m["Name"], [])
                if name not in usage[m["Name"]]:
                    usage[m["Name"]].append(name)
    return usage


def list_volumes():
    rc, out, err = run([DOCKER, "volume", "ls", "--format", "{{json .}}"], timeout=30)
    if rc != 0:
        return None, err
    items = []
    for line in out.splitlines():
        try:
            items.append(json.loads(line))
        except Exception:
            pass
    return items, ""


def inspect_container(name):
    rc, out, err = run([DOCKER, "inspect", name], timeout=30)
    if rc != 0:
        return None, err
    try:
        data = json.loads(out)
        return data[0] if data else None, ""
    except Exception as e:
        return None, str(e)


# ---------- inspect -> docker run 命令（尽力还原） ----------
def sh_quote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


def inspect_to_run_argv(ins):
    """从 docker inspect 生成 argv 列表（用于直接执行，无 shell）"""
    name = (ins.get("Name") or "").lstrip("/")
    cfg = ins.get("Config", {}) or {}
    host = ins.get("HostConfig", {}) or {}
    image = cfg.get("Image", "")

    argv = ["docker", "run", "-d"]
    if name:
        argv += ["--name", name]
    rp = host.get("RestartPolicy") or {}
    if rp.get("Name") and rp.get("Name") != "no":
        argv += ["--restart", rp["Name"]]
    netmode = host.get("NetworkMode", "")
    if netmode and netmode != "default":
        argv += ["--network", netmode]
    pb = host.get("PortBindings") or {}
    for cport, bindings in pb.items():
        for b in bindings or []:
            hip = b.get("HostIp", "")
            hport = b.get("HostPort", "")
            if hip and hip not in ("", "0.0.0.0"):
                argv += ["-p", "%s:%s:%s" % (hip, hport, cport)]
            else:
                argv += ["-p", "%s:%s" % (hport, cport)]
    for m in ins.get("Mounts", []) or []:
        if m.get("Type") == "volume":
            mp = "%s:%s" % (m["Name"], m["Destination"])
            if not m.get("RW", True):
                mp += ":ro"
            argv += ["-v", mp]
        elif m.get("Type") == "bind":
            mp = "%s:%s" % (m["Source"], m["Destination"])
            if not m.get("RW", True):
                mp += ":ro"
            argv += ["-v", mp]
    for e in cfg.get("Env") or []:
        argv += ["-e", e]
    argv.append(image)
    cmd = cfg.get("Cmd")
    if cmd:
        argv += list(cmd)
    return argv


def inspect_to_run(ins):
    """从 docker inspect 生成尽力还原的 docker run 命令（展示用）"""
    try:
        return " ".join(sh_quote(a) for a in inspect_to_run_argv(ins))
    except Exception as e:
        return "# 生成失败: %s" % e


def container_networks(ins):
    """容器使用的自定义网络名（排除默认网络）"""
    nets = (ins.get("NetworkSettings") or {}).get("Networks") or {}
    return [n for n in nets.keys() if n not in ("bridge", "host", "none")]


def existing_containers():
    rc, out, _ = run([DOCKER, "ps", "-a", "--format", "{{.Names}}"], timeout=30)
    if rc != 0:
        return set()
    return set(out.split())


def existing_networks():
    rc, out, _ = run([DOCKER, "network", "ls", "--format", "{{.Name}}"], timeout=30)
    if rc != 0:
        return set()
    return set(out.split())


def recreate_container(name, argv, networks, force=False):
    """重建单个容器，返回 (ok, msg)"""
    if name in existing_containers():
        if not force:
            return False, "同名容器已存在，已跳过（如需重建请先删除旧容器）"
        # 强制重建：先停再删
        run([DOCKER, "stop", name], timeout=60)
        rc, _, err = run([DOCKER, "rm", name], timeout=60)
        if rc != 0:
            return False, "删除旧容器失败: %s" % err
    for net in networks:
        if net not in existing_networks():
            rc, _, err = run([DOCKER, "network", "create", net], timeout=60)
            if rc != 0:
                return False, "创建网络 %s 失败: %s" % (net, err)
    rc, out, err = run(argv, timeout=120)
    if rc == 0:
        return True, "已启动，ID %s" % out[:12]
    return False, err or out or "未知错误"


# ---------- 备份任务 ----------
RESTORE_SH = r"""#!/bin/bash
# Docker备份一键恢复脚本（无需安装插件）
# 用法：bash restore.sh
set -e
cd "$(dirname "$0")"
echo "== 恢复镜像 =="
for f in images/*.tar; do
  [ -e "$f" ] || continue
  echo "  docker load -i $f"
  docker load -i "$f"
done
echo "== 恢复快照镜像 =="
for f in snapshots/*.tar; do
  [ -e "$f" ] || continue
  echo "  docker load -i $f"
  docker load -i "$f"
done
echo "== 恢复数据卷 =="
ROOTDIR=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)
for f in volumes/*.tar.gz; do
  [ -e "$f" ] || continue
  vol=$(basename "$f" .tar.gz)
  echo "  恢复卷: $vol"
  docker volume create "$vol" >/dev/null 2>&1 || true
  mkdir -p "$ROOTDIR/volumes/$vol/_data"
  tar xzf "$f" -C "$ROOTDIR/volumes/$vol/_data"
done
echo "== 恢复宿主机目录(bind挂载) =="
for f in binds/*.tar.gz; do
  [ -e "$f" ] || continue
  echo "  恢复: $f (解压到原路径)"
  tar xzf "$f" -C /
done
echo "== 完成 =="
echo "容器 inspect 配置在 inspect/ 目录，还原命令见 run-commands.sh"
"""


def do_backup(tid, images, volumes, containers, binds, snapshot=False):
    tmp = tempfile.mkdtemp(prefix="dkbak-", dir=TASK_DIR)
    try:
        update_task(tid, status="running", phase="准备", progress=2)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        work = os.path.join(tmp, "backup")
        os.makedirs(os.path.join(work, "images"))
        os.makedirs(os.path.join(work, "volumes"))
        os.makedirs(os.path.join(work, "binds"))
        os.makedirs(os.path.join(work, "snapshots"))
        os.makedirs(os.path.join(work, "inspect"))

        meta = {"created": stamp, "plugin_version": PLUGIN_VERSION,
                "images": images, "volumes": volumes,
                "containers": containers, "binds": binds,
                "snapshots": {}, "container_images": {}, "run_commands": {}}
        total = len(images) + len(volumes) + len(containers) + len(binds)
        if snapshot:
            total += len(containers)
        done = 0

        def tick(phase):
            nonlocal done
            done += 1
            pct = 5 + int(85 * done / max(total, 1))
            update_task(tid, phase=phase, progress=pct)

        # 1. 容器 inspect + 生成 run 命令
        for c in containers:
            ins, err = inspect_container(c)
            if ins:
                with open(os.path.join(work, "inspect", re.sub(r"[^\w.\-]", "_", c) + ".json"), "w") as f:
                    json.dump(ins, f, indent=2, ensure_ascii=False)
                meta["run_commands"][c] = inspect_to_run(ins)
                # 记录原镜像名（快照打包与恢复时用）
                meta["container_images"][c] = (ins.get("Config") or {}).get("Image", "")
                try:
                    meta.setdefault("run_argv", {})[c] = inspect_to_run_argv(ins)
                    meta.setdefault("run_networks", {})[c] = container_networks(ins)
                except Exception as e:
                    update_task(tid, warnings=tasks[tid].get("warnings", []) + ["生成重建命令失败 %s: %s" % (c, e)])
            tick("备份容器配置: %s" % c)

        # 1b. 容器文件系统快照（docker commit，只提交暂存；导出时与原镜像一次save，共享层只存一份）
        snap_tags = []       # 待清理的快照tag
        snap_by_image = {}   # 原镜像 -> [snap_tag]
        if snapshot:
            for c in containers:
                update_task(tid, phase="快照容器: %s" % c)
                snap_tag = "%s-snapshot:%s" % (re.sub(r"[^\w.\-]", "_", c), stamp)
                rc, _, err = run([DOCKER, "commit", c, snap_tag], timeout=1800)
                if rc == 0:
                    meta["snapshots"][c] = snap_tag
                    snap_tags.append(snap_tag)
                    orig = meta["container_images"].get(c, "")
                    snap_by_image.setdefault(orig, []).append(snap_tag)
                else:
                    update_task(tid, warnings=tasks[tid].get("warnings", []) + ["容器快照失败 %s: %s" % (c, err)])
                tick("快照容器: %s" % c)

        # 2. 镜像 docker save（有快照的镜像与快照一次save，共享层只存一份）
        snapshotted = set(k for k in snap_by_image if k)
        for img in images:
            if img in snapshotted:
                continue
            safename = re.sub(r"[^\w.\-]", "_", img)
            dest = os.path.join(work, "images", safename + ".tar")
            update_task(tid, phase="导出镜像: %s" % img)
            rc, _, err = run([DOCKER, "save", "-o", dest, img], timeout=1800)
            if rc != 0:
                update_task(tid, warnings=tasks[tid].get("warnings", []) + ["镜像导出失败 %s: %s" % (img, err)])
            tick("导出镜像: %s" % img)
        for orig, snaps in snap_by_image.items():
            bundle = ([orig] if orig else []) + snaps
            safename = re.sub(r"[^\w.\-]", "_", (orig or snaps[0]))
            dest = os.path.join(work, "snapshots", safename + "_bundle.tar")
            update_task(tid, phase="导出镜像+快照: %s" % (orig or snaps[0]))
            rc, _, err = run([DOCKER, "save", "-o", dest] + bundle, timeout=3600)
            if rc != 0:
                update_task(tid, warnings=tasks[tid].get("warnings", []) + ["镜像快照导出失败 %s: %s" % (orig, err)])
            tick("导出镜像+快照: %s" % (orig or snaps[0]))
        # 清理临时快照镜像，保持环境干净（tar包里已有）
        for st in snap_tags:
            run([DOCKER, "rmi", st], timeout=120)
        # 快照与原镜像合并导出后，step2 实际次数变化，重算总数保证进度准确
        total = done + len([i for i in images if i not in snapshotted]) + len(snap_by_image) + len(volumes) + len(binds)

        # 3. 数据卷打包
        rootdir = docker_root_dir()
        for vol in volumes:
            vdir = os.path.join(rootdir, "volumes", vol, "_data")
            dest = os.path.join(work, "volumes", vol + ".tar.gz")
            update_task(tid, phase="打包数据卷: %s" % vol)
            if os.path.isdir(vdir):
                rc, _, err = run(["tar", "czf", dest, "-C", vdir, "."], timeout=1800)
                if rc != 0:
                    update_task(tid, warnings=tasks[tid].get("warnings", []) + ["数据卷打包失败 %s: %s" % (vol, err)])
            else:
                update_task(tid, warnings=tasks[tid].get("warnings", []) + ["数据卷目录不存在: %s" % vdir])
            tick("打包数据卷: %s" % vol)

        # 3b. bind 挂载的宿主机目录打包
        for b in binds:
            src = b.get("source", "")
            update_task(tid, phase="打包宿主机目录: %s" % src)
            if src and os.path.exists(src):
                safename = re.sub(r"[^\w.\-]", "_", src.strip("/")) or "root"
                dest = os.path.join(work, "binds", safename + ".tar.gz")
                # 用相对于 / 的完整路径打包，恢复时 -C / 解压可精确还原到原路径
                rel = src.strip("/")
                rc, _, err = run(["tar", "czf", dest, "-C", "/", rel], timeout=1800)
                if rc != 0:
                    update_task(tid, warnings=tasks[tid].get("warnings", []) + ["宿主机目录打包失败 %s: %s" % (src, err)])
            else:
                update_task(tid, warnings=tasks[tid].get("warnings", []) + ["宿主机目录不存在已跳过: %s" % src])
            tick("打包宿主机目录: %s" % src)

        # 4. 生成 run-commands.sh 与 restore.sh
        update_task(tid, phase="生成恢复脚本", progress=92)
        with open(os.path.join(work, "run-commands.sh"), "w") as f:
            f.write("#!/bin/bash\n# 在新机器上执行以下命令重建容器（请按需修改卷路径/端口）\nset -e\n\n")
            for c, cmdline in meta["run_commands"].items():
                f.write("# ---- %s ----\n%s\n\n" % (c, cmdline))
        with open(os.path.join(work, "restore.sh"), "w") as f:
            f.write(RESTORE_SH)
        os.chmod(os.path.join(work, "restore.sh"), 0o755)
        os.chmod(os.path.join(work, "run-commands.sh"), 0o755)
        with open(os.path.join(work, "meta.json"), "w") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        # 5. 打总包（文件名：docker-backup-<容器名>-v<插件版本>-<时间戳>.tar.gz）
        update_task(tid, phase="打包备份文件", progress=95)
        if len(containers) == 1:
            tag = re.sub(r"[^\w.\-]", "_", containers[0])
            fname = "docker-backup-%s-v%s-%s.tar.gz" % (tag[:60], PLUGIN_VERSION, stamp)
        elif len(images) == 1:
            tag = re.sub(r"[^\w.\-]", "_", images[0].split("/")[-1].replace(":", "_"))
            fname = "docker-backup-%s-v%s-%s.tar.gz" % (tag[:60], PLUGIN_VERSION, stamp)
        else:
            n = len(images) + len(containers)
            fname = "docker-backup-%ditems-v%s-%s.tar.gz" % (n, PLUGIN_VERSION, stamp)
        fpath = os.path.join(BACKUP_DIR, fname)
        rc, _, err = run(["tar", "czf", fpath, "-C", tmp, "backup"], timeout=1800)
        if rc != 0:
            update_task(tid, status="failed", error="打包失败: %s" % err, progress=100)
            return
        size = os.path.getsize(fpath)
        update_task(tid, status="done", phase="完成", progress=100, file=fname, size=size)
        log("备份完成: %s (%d bytes)" % (fname, size))
    except Exception as e:
        update_task(tid, status="failed", error=str(e), progress=100)
        log("备份失败: %s" % e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 恢复任务 ----------
def do_restore(tid, pkg_path, recreate=False, force=False):
    tmp = tempfile.mkdtemp(prefix="dkrst-", dir=TASK_DIR)
    try:
        update_task(tid, status="running", phase="解压备份包", progress=5)
        rc, _, err = run(["tar", "xzf", pkg_path, "-C", tmp], timeout=600)
        if rc != 0:
            update_task(tid, status="failed", error="解压失败: %s" % err, progress=100)
            return
        work = os.path.join(tmp, "backup")
        if not os.path.isdir(work):
            # 兼容直接打包内容无 backup 目录的情况
            work = tmp
        meta = {}
        mp = os.path.join(work, "meta.json")
        if os.path.isfile(mp):
            try:
                meta = json.load(open(mp))
            except Exception:
                pass

        logs = []

        # 1. 导入镜像
        imgdir = os.path.join(work, "images")
        imgs = sorted([f for f in os.listdir(imgdir) if f.endswith(".tar")]) if os.path.isdir(imgdir) else []
        for i, f in enumerate(imgs):
            update_task(tid, phase="导入镜像 %d/%d" % (i + 1, len(imgs)),
                        progress=10 + int(35 * (i + 1) / max(len(imgs), 1)))
            rc, out, err = run([DOCKER, "load", "-i", os.path.join(imgdir, f)], timeout=1800)
            logs.append(("镜像 %s" % f, rc == 0, out or err))

        # 1b. 导入快照镜像
        snapdir = os.path.join(work, "snapshots")
        snaps = sorted([f for f in os.listdir(snapdir) if f.endswith(".tar")]) if os.path.isdir(snapdir) else []
        for i, f in enumerate(snaps):
            update_task(tid, phase="导入快照 %d/%d" % (i + 1, len(snaps)),
                        progress=10 + int(35 * (i + 1) / max(len(snaps), 1)))
            rc, out, err = run([DOCKER, "load", "-i", os.path.join(snapdir, f)], timeout=1800)
            logs.append(("快照 %s" % f, rc == 0, out or err))
        # 2. 恢复数据卷
        voldir = os.path.join(work, "volumes")
        vols = sorted([f for f in os.listdir(voldir) if f.endswith(".tar.gz")]) if os.path.isdir(voldir) else []
        rootdir = docker_root_dir()
        for i, f in enumerate(vols):
            vol = f[:-len(".tar.gz")]
            update_task(tid, phase="恢复数据卷 %d/%d: %s" % (i + 1, len(vols), vol),
                        progress=45 + int(35 * (i + 1) / max(len(vols), 1)))
            rc1, _, e1 = run([DOCKER, "volume", "create", vol], timeout=60)
            vdir = os.path.join(rootdir, "volumes", vol, "_data")
            os.makedirs(vdir, exist_ok=True)
            rc2, _, e2 = run(["tar", "xzf", os.path.join(voldir, f), "-C", vdir], timeout=1800)
            ok = rc2 == 0
            logs.append(("数据卷 %s" % vol, ok, e2 if not ok else ""))
            if rc1 != 0 and not ok:
                logs.append(("数据卷 %s 创建" % vol, False, e1))

        # 2b. 恢复 bind 宿主机目录
        binddir = os.path.join(work, "binds")
        bindfiles = sorted([f for f in os.listdir(binddir) if f.endswith(".tar.gz")]) if os.path.isdir(binddir) else []
        bind_meta = meta.get("binds", []) or []
        # 用 meta 记录的原路径恢复；文件名是路径编码，优先用 meta
        for i, f in enumerate(bindfiles):
            update_task(tid, phase="恢复宿主机目录 %d/%d" % (i + 1, len(bindfiles)),
                        progress=80 + int(5 * (i + 1) / max(len(bindfiles), 1)))
            rc, _, e2 = run(["tar", "xzf", os.path.join(binddir, f), "-C", "/"], timeout=1800)
            logs.append(("宿主机目录 %s" % f, rc == 0, e2 if rc != 0 else ""))
        recreated = []
        run_argv = meta.get("run_argv", {}) or {}
        run_networks = meta.get("run_networks", {}) or {}
        snapshots = meta.get("snapshots", {}) or {}
        container_images = meta.get("container_images", {}) or {}
        if recreate and run_argv:
            names = list(run_argv.keys())
            for i, cname in enumerate(names):
                update_task(tid, phase="重建容器 %d/%d: %s" % (i + 1, len(names), cname),
                            progress=80 + int(18 * (i + 1) / max(len(names), 1)))
                argv = list(run_argv[cname])
                # 有快照则替换为快照镜像（含容器内文件状态）
                snap_img = snapshots.get(cname)
                orig_img = container_images.get(cname)
                if snap_img and orig_img:
                    argv = [snap_img if a == orig_img else a for a in argv]
                try:
                    ok, msg = recreate_container(cname, argv, run_networks.get(cname, []), force)
                except Exception as e:
                    ok, msg = False, str(e)
                recreated.append((cname, ok, msg))
                logs.append(("重建容器 %s" % cname, ok, msg))

        hint = "镜像与数据卷已恢复。"
        if recreate:
            hint += "容器已按备份配置自动重建。"
        else:
            hint += "如需重建容器，请参考各容器的 docker run 命令（见下方）或执行包内 run-commands.sh。"
        update_task(tid, status="done", phase="完成", progress=100, logs=logs,
                    run_commands=meta.get("run_commands", {}),
                    run_argv=meta.get("run_argv", {}),
                    run_networks=meta.get("run_networks", {}),
                    recreated=[{"name": n, "ok": ok, "msg": msg} for n, ok, msg in recreated],
                    hint=hint)
        log("恢复完成: %d 镜像, %d 数据卷, %d 重建" % (len(imgs), len(vols), len(recreated)))
    except Exception as e:
        update_task(tid, status="failed", error=str(e), progress=100)
        log("恢复失败: %s" % e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- HTTP 处理 ----------
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json",
        ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = "DockerBackup/1.0"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, code=200, ctype="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        # 兼容网关透传完整路径的情况
        if path == "/app/dockerbackup" or path.startswith("/app/dockerbackup/"):
            path = path[len("/app/dockerbackup"):] or "/"

        if path == "/":
            return self.serve_file("index.html")
        if path.startswith("/static/"):
            return self.serve_file(path[len("/static/"):])
        if path == "/ui/index.html":
            return self.serve_file("index.html")

        if path == "/api/status":
            ok, ver = docker_ok()
            return self.send_json({"docker": ok, "version": ver})
        if path == "/api/containers":
            items, err = list_containers()
            return self.send_json({"items": items, "error": err} if items is not None else {"error": err}, 200 if items is not None else 500)
        if path == "/api/images":
            items, err = list_images()
            return self.send_json({"items": items, "error": err} if items is not None else {"error": err}, 200 if items is not None else 500)
        if path == "/api/volumes":
            items, err = list_volumes()
            if items is None:
                return self.send_json({"error": err}, 500)
            usage = volume_usage_map()
            for v in items:
                v["used_by"] = usage.get(v.get("Name", ""), [])
            return self.send_json({"items": items, "error": ""})
        if path == "/api/container-volumes":
            name = qs.get("name", [""])[0]
            ins, err = inspect_container(name)
            if not ins:
                return self.send_json({"error": err}, 500)
            vols = [m["Name"] for m in ins.get("Mounts", []) if m.get("Type") == "volume"]
            binds = [{"source": m["Source"], "destination": m["Destination"]}
                     for m in ins.get("Mounts", []) if m.get("Type") == "bind" and m.get("Source")]
            img = (ins.get("Config") or {}).get("Image", "")
            return self.send_json({"volumes": vols, "binds": binds, "image": img})
        if path == "/api/task":
            tid = qs.get("id", [""])[0]
            with tasks_lock:
                t = tasks.get(tid)
            if not t:
                return self.send_json({"error": "任务不存在"}, 404)
            return self.send_json(t)
        if path == "/api/backups":
            files = []
            for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
                if f.endswith(".tar.gz"):
                    fp = os.path.join(BACKUP_DIR, f)
                    files.append({"name": f, "size": os.path.getsize(fp),
                                  "mtime": int(os.path.getmtime(fp))})
            return self.send_json({"items": files})
        if path == "/api/download":
            fname = safe_filename(qs.get("file", [""])[0])
            if not fname:
                return self.send_text("非法文件名", 400)
            fp = os.path.join(BACKUP_DIR, fname)
            if not os.path.isfile(fp):
                return self.send_text("文件不存在", 404)
            size = os.path.getsize(fp)
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", "attachment; filename=\"%s\"" % fname)
            self.end_headers()
            with open(fp, "rb") as f:
                shutil.copyfileobj(f, self.wfile, length=1024 * 1024)
            return
        return self.send_text("Not Found", 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        # 兼容网关透传完整路径的情况
        if path == "/app/dockerbackup" or path.startswith("/app/dockerbackup/"):
            path = path[len("/app/dockerbackup"):] or "/"
        length = int(self.headers.get("Content-Length", 0) or 0)

        if path == "/api/backup":
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self.send_json({"error": "请求体解析失败"}, 400)
            images = [i for i in body.get("images", []) if isinstance(i, str)]
            volumes = [v for v in body.get("volumes", []) if isinstance(v, str) and SAFE_NAME.match(v)]
            containers = [c for c in body.get("containers", []) if isinstance(c, str)]
            binds = [b for b in body.get("binds", [])
                     if isinstance(b, dict) and isinstance(b.get("source"), str) and b["source"].startswith("/")]
            # binds 去重
            seen = set()
            ubinds = []
            for b in binds:
                if b["source"] not in seen:
                    seen.add(b["source"])
                    ubinds.append(b)
            binds = ubinds
            snapshot = body.get("snapshot", False) is True
            if not images and not volumes and not containers and not binds:
                return self.send_json({"error": "请至少选择一项"}, 400)
            tid = hashlib.md5(str(time.time()).encode()).hexdigest()[:12]
            with tasks_lock:
                tasks[tid] = {"id": tid, "type": "backup", "status": "queued",
                              "phase": "排队", "progress": 0, "warnings": []}
            th = threading.Thread(target=do_backup, args=(tid, images, volumes, containers, binds, snapshot), daemon=True)
            th.start()
            return self.send_json({"task_id": tid})

        if path == "/api/restore":
            fname = qs.get("filename", ["upload.tar.gz"])[0]
            fname = os.path.basename(fname)
            recreate = qs.get("recreate", ["0"])[0] == "1"
            force = qs.get("force", ["0"])[0] == "1"
            if not fname.endswith((".tar.gz", ".tgz")):
                self.rfile.read(length)
                return self.send_json({"error": "只支持 .tar.gz 备份包"}, 400)
            # 流式写入临时文件（支持大文件）
            tmpf = tempfile.mktemp(prefix="upload-", dir=TASK_DIR) + ".tar.gz"
            try:
                remain = length
                with open(tmpf, "wb") as f:
                    while remain > 0:
                        chunk = self.rfile.read(min(1024 * 1024, remain))
                        if not chunk:
                            break
                        f.write(chunk)
                        remain -= len(chunk)
            except Exception as e:
                return self.send_json({"error": "上传失败: %s" % e}, 500)
            # 简单校验 tar.gz
            if not tarfile.is_tarfile(tmpf):
                os.unlink(tmpf)
                return self.send_json({"error": "文件不是有效的 tar.gz"}, 400)
            tid = hashlib.md5(str(time.time()).encode()).hexdigest()[:12]
            with tasks_lock:
                tasks[tid] = {"id": tid, "type": "restore", "status": "queued",
                              "phase": "排队", "progress": 0}
            th = threading.Thread(target=do_restore, args=(tid, tmpf, recreate, force), daemon=True)
            th.start()
            return self.send_json({"task_id": tid})

        if path == "/api/delete":
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self.send_json({"error": "请求体解析失败"}, 400)
            fname = safe_filename(body.get("file", ""))
            if not fname:
                return self.send_json({"error": "非法文件名"}, 400)
            fp = os.path.join(BACKUP_DIR, fname)
            if os.path.isfile(fp):
                os.unlink(fp)
            return self.send_json({"ok": True})

        if path == "/api/volume-rm":
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self.send_json({"error": "请求体解析失败"}, 400)
            name = body.get("name", "")
            if not name or not SAFE_NAME.match(name):
                return self.send_json({"error": "非法卷名"}, 400)
            # 安全检查：确认无容器使用
            usage = volume_usage_map()
            if usage.get(name):
                return self.send_json({"error": "该卷仍被容器使用: %s" % ",".join(usage[name])}, 400)
            rc, _, err = run([DOCKER, "volume", "rm", name], timeout=60)
            if rc != 0:
                return self.send_json({"error": err or "删除失败"}, 500)
            return self.send_json({"ok": True})

        if path == "/api/volume-prune":
            self.rfile.read(length)
            rc, out, err = run([DOCKER, "volume", "prune", "-f"], timeout=120)
            if rc != 0:
                return self.send_json({"error": err or "清理失败"}, 500)
            return self.send_json({"ok": True, "output": out})

        if path == "/api/recreate-one":
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self.send_json({"error": "请求体解析失败"}, 400)
            tid = body.get("task_id", "")
            container = body.get("container", "")
            new_name = body.get("new_name", "")
            if not new_name or not SAFE_NAME.match(new_name):
                return self.send_json({"error": "新名称不合法（字母数字下划线，字母开头）"}, 400)
            with tasks_lock:
                t = tasks.get(tid)
            if not t:
                return self.send_json({"error": "任务已过期，请重新恢复"}, 400)
            run_argv = t.get("run_argv", {}) or {}
            run_networks = t.get("run_networks", {}) or {}
            argv = run_argv.get(container)
            if not argv:
                return self.send_json({"error": "找不到该容器的配置"}, 400)
            if new_name in existing_containers():
                return self.send_json({"error": "新名称 %s 已被占用" % new_name}, 400)
            new_argv = list(argv)
            try:
                idx = new_argv.index("--name")
                new_argv[idx + 1] = new_name
            except (ValueError, IndexError):
                new_argv[3:3] = ["--name", new_name]
            try:
                ok, msg = recreate_container(new_name, new_argv, run_networks.get(container, []))
            except Exception as e:
                ok, msg = False, str(e)
            return self.send_json({"ok": ok, "msg": msg})

        self.rfile.read(length)
        return self.send_text("Not Found", 404)

    def serve_file(self, name):
        if ".." in name or name.startswith("/"):
            return self.send_text("Forbidden", 403)
        fp = os.path.join(UI_DIR, name if name else "index.html")
        if not os.path.isfile(fp):
            return self.send_text("Not Found", 404)
        ext = os.path.splitext(fp)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        size = os.path.getsize(fp)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(fp, "rb") as f:
            shutil.copyfileobj(f, self.wfile, length=1024 * 1024)


def main():
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    server = UnixStreamServer(SOCKET_PATH, Handler)
    os.chmod(SOCKET_PATH, 0o777)
    log("监听 socket: %s" % SOCKET_PATH)
    log("UI 目录: %s" % UI_DIR)
    ok, ver = docker_ok()
    log("docker 状态: %s %s" % ("可用" if ok else "不可用", ver))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
