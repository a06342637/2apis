# 主机系统更新助手

管理台「任务与配置 → 更新系统」可以检查 GitHub `owner/repo` 的 main 分支，
查看版本和完整 commit，更新通过 CI 的源码。版本号相同、commit 不同时仍可更新。
默认来源是 `a06342637/2apis`，管理员可以保存其他合法 GitHub 仓库名称。

更新必须使用 `.github/workflows/ci.yml` 已成功运行的固定 commit；backend、frontend、
compose、security、platform 均需成功，其他 job 也不能失败或跳过。
「覆盖本地源码修改」仅允许覆盖源码，不会跳过 CI。备份会保留本地源码修改。

应用以 uid 1001 运行。Docker socket **不能**挂进应用容器；主机另装 root systemd
helper，应用仅通过专用 Unix socket 与只读 secret 发出 status/check/source/start。
helper 不接受 shell、命令、路径或任意分支参数。所有进程调用使用固定 argv。
helper 的 Python 代码在独立目录安装，需要 Python 3.11 或更高版本，仅依赖标准库，
不执行下载的运维文档或脚本。

主机管理员安装独立副本到 `/opt/2apis-update-helper`（含 `src/updater/`、
`src/instance_lock.py`、`scripts/update_host.py`），安装 systemd unit
[`system-update-helper.service`](system-update-helper.service)，并依据实际部署调整目录。
创建专用 `2apis-update` 组，容器以 supplementary group 获得 socket 与 secret 的读取权限。
配置和 helper 源码由 root 持有，禁止应用用户写入；状态目录 mode 0700。
socket 所在目录 mode 0750、socket mode 0660，secret mode 0640 且至少 32 个随机字符。
secret 与 socket 所在目录均不得对无关用户开放。

`/etc/2apis-update/config.json` 的示例（所有路径必须为规范化绝对路径，无链接）：

```json
{
  "source_root": "/opt/2apis",
  "state_dir": "/var/lib/2apis-update",
  "data_dir": "/opt/2apis/data",
  "secrets_dir": "/opt/2apis/secrets",
  "compose_files": ["/opt/2apis/docker-compose.yml", "/opt/2apis/docker-compose.prod.yml", "/opt/2apis/docker-compose.host.yml"],
  "env_file": "/opt/2apis/.env",
  "socket_path": "/run/2apis-update/update.sock",
  "secret_file": "/etc/2apis-update/secret",
  "project": "2apis",
  "service": "coding2api",
  "source": "a06342637/2apis",
  "current_version": "0.1.0",
  "current_commit": "填写当前部署的完整40位commit",
  "health_timeout": 180,
  "drain_timeout": 180
}
```

`current_commit` 首次安装时由主机管理员填写已部署 commit；确实无法确认时留空，
界面显示「提交未知」。首次安装保存源码 hash 清单，后续检查可以发现本地源修改。
状态/备份目录必须在源码、数据和密钥目录之外；不要将生产数据放入 src/scripts/web
等代码目录。Compose 文件与 `.env` 始终采用主机的部署配置，并保留原 APP_SECRET。

应用配置 `UPDATE_SOCKET_PATH=/run/2apis-update/update.sock`、
`UPDATE_SECRET_FILE=/run/2apis-update-secret`；挂载 socket **目录**到相同路径（保证 helper
重启换 socket inode 后仍可重新连接），把 secret 单独只读挂载。加 `group_add` 允许访问，
并在 host-owned Compose 覆盖文件中保留这些挂载，不修改非 root 运行方式。
两个变量留空时页面显示此部署未启用主机更新助手。

事务先 clone/fetch 验证固定 Git SHA，然后构建 `2apis-update:<commit>`；旧服务继续运行。
构建后重新检查目标 main 与 CI，再停止并排空旧应用，备份代码、完整 data/secrets、
`.env` 和全部 Compose 配置。主库用 VACUUM INTO 生成包含 WAL 已提交事务的独立快照，
备份用 SHA-256 清单与 SQLite integrity_check 校验。
只有校验成功才切换代码与镜像，等待新服务 healthy；过程中不会替换 Tunnel/Caddy 配置。

切换失败时停止新服务，校验备份，恢复原代码、数据库、文件属主、密钥与 Compose 配置，
使用原镜像 ID 重建并等待 healthy。构建或 CI 失败不会停止旧服务。主机锁阻止多个 helper
实例，服务内锁阻止并发更新。进度和日志持久化，页面刷新和应用重启后继续展示。
helper 重启时自动恢复未完成事务；若备份损坏或原服务无法 healthy，则锁定后续更新，
保留全部备份与失败信息，等待主机管理员检查。不会自动清理历史备份或镜像。

管理员 API：GET `/api/system-updates`；POST `/api/system-updates/check`；
PUT `/api/system-updates/source` 传 `{ "source": "owner/repo" }`；
POST `/api/system-updates/start` 传 `{ "commit": "完整SHA", "force": false }`。
状态端点包含版本、提交、CI、dirty 与持久化 job/progress/logs，不下发主机私密路径或镜像回滚信息。
