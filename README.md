# Docker备份 - 飞牛OS原生插件

飞牛OS (fnOS) 原生 `.fpk` 插件，一键完整备份 Docker 并支持迁移恢复。

## 功能

- **备份**：镜像（`docker save`）、容器配置（`docker inspect`）、命名卷、bind 挂载的宿主机目录
- **容器文件系统快照**：`docker commit` 保存容器内改过的配置文件，与原镜像一次 `docker save`（共享层只存一份）
- **恢复**：导入镜像、恢复命名卷/bind 目录、自动补建缺失网络、按 inspect 自动重建容器
- 备份文件名自动带容器名和插件版本号，如 `docker-backup-homeassistant-v1.3.4-20261007-104000.tar.gz`
- 数据卷显示占用容器，支持删除孤立卷

## 结构

```
├── manifest          # 插件清单（版本、显示名、权限）
├── cmd/              # 安装/启动/停止脚本
├── config/           # 权限配置（root 运行）
├── wizard/           # 安装向导
├── ICON.PNG / ICON_256.PNG
└── app/
    ├── server/server.py   # 后端（仅 Python 标准库，Unix Socket HTTP 服务）
    └── ui/index.html      # 前端（单文件）
```

## 打包

```bash
cd app && tar czf /tmp/app.tgz --owner=0 --group=0 server ui
# 替换 fpk 中的 app.tgz，更新 manifest 的 checksum 与 manifest.checksum
```

## 版本历史

- v1.3.4：应用中心显示名带版本号
- v1.3.3：备份文件名带插件版本号，meta.json 记录版本
- v1.3.2：快照与原镜像一次 save，共享层去重
- v1.3.1：备份文件名优先用容器名
- v1.3.0：容器文件系统快照（docker commit）
- v1.2.6：换上官方 Docker 鲸鱼图标
- v1.2.5：孤立卷删除/清理
- v1.2.4：修复 bind 恢复路径错误
