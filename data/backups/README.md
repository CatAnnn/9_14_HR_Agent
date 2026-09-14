# 本机私有备份目录

本目录用于保存当前仓库对应环境的 PostgreSQL 备份和 `data/` 文件快照。
除本说明外，本目录中的内容均被 Git 忽略；备份可能包含账号、密码哈希、
员工档案、完整对话和报告，不得使用 `git add -f` 提交。

## 创建备份

以下命令从仓库根目录执行。备份期间应停止后端写入；如果 PostgreSQL 已停止，
可以只启动 `postgres` 服务。

```bash
backup_dir="data/backups/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$backup_dir/data"
chmod 700 data/backups "$backup_dir" "$backup_dir/data"

docker compose up -d postgres
docker compose exec -T postgres sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U hr_agent -p 7114 -d hr_agent -Fc' \
  > "$backup_dir/hr_agent.dump.partial"

docker compose exec -T postgres pg_restore --list \
  < "$backup_dir/hr_agent.dump.partial" > /dev/null
mv "$backup_dir/hr_agent.dump.partial" "$backup_dir/hr_agent.dump"

rsync -a \
  --exclude 'asr_tmp/' \
  --exclude 'backups/' \
  data/ "$backup_dir/data/"

sha256sum "$backup_dir/hr_agent.dump" > "$backup_dir/SHA256SUMS"
chmod -R go-rwx "$backup_dir"
```

`data/asr_tmp/` 是临时录音目录，不属于长期用户记录；`backups/` 必须排除，
否则复制 `data/` 时会递归包含备份自身。

## 迁移与恢复

将整个时间戳目录通过受控方式复制到目标服务器。在目标服务器使用相同代码和
PostgreSQL/vchord 镜像，并且只恢复到确认无业务数据的空数据库：

```bash
rsync -a /path/to/backup/data/ data/
docker compose up -d postgres
docker compose exec -T postgres sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore -U hr_agent -p 7114 \
  -d hr_agent --no-owner --exit-on-error' \
  < /path/to/backup/hr_agent.dump
docker compose run --rm backend_migrate
```

恢复后至少核对 `app_users`、`employees`、`sessions`、`documents`、
`conversation_summaries`、`guidance_report_versions` 和
`coach_report_versions` 的记录数。Redis 通常不恢复，用户重新登录即可。

## 不包含的内容

- 浏览器 `sessionStorage` 中尚未提交的草稿；迁移前应先提交。
- Redis 中会过期的登录态、缓存、限流和分布式锁。
- 可重新下载的模型缓存。
- 自定义 TTS 音色卷；启用自定义音色时需单独备份。

历史 `data/backups/*.dump` 可能已经进入 Git 历史。本目录的忽略规则不会自动
清理那些旧文件或历史提交，处理前应先确认远端仓库和备份保留要求。
