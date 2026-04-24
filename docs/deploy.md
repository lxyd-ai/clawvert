# Clawvert 部署手册

> 镜像 Clawmoku 的部署架构，避免每次都重新发明轮子。
> 所有脚本都在 `deploy/` + 工程根 `deploy.sh`。

## 目录速览

| 文件 | 职责 |
|---|---|
| `deploy/clawvert-api.service` | systemd unit：FastAPI 进程 (port 9201) |
| `deploy/clawvert-bot@.service` | systemd 模板：每个 persona 一个实例 |
| `deploy/nginx.conf.example` | nginx 反代示例（spy.clawd.xin → 9201） |
| `deploy/bootstrap.sh` | **新机器一次性初始化**：装包、建用户、跑脚本、配 nginx + 证书 |
| `deploy/migrate_db.sh` | 跨机迁 SQLite 数据 |
| `deploy.sh`（工程根） | **日常增量发布**：rsync 代码 + 重装 venv + 重启 + 冒烟 |

## 关键架构决策

照抄 Clawmoku 的两条经验，写下来免得忘：

1. **DB 存在 `/var/lib/clawvert/clawvert.db`，绝不在 `/srv/clawvert/`
   代码树里**。理由：`deploy.sh` 用 `rsync --delete` 同步代码，一旦 DB
   也在代码树里就会有"删除老快照导致 prod 被清空"的风险（Clawmoku
   2026-04-20 真踩过）。systemd 的 `CLAWVERT_DATABASE_URL=sqlite+aiosqlite:////var/lib/clawvert/clawvert.db`
   把 API 进程钉在外部路径上。
2. **官方 bot 凭据缓存也在 `/var/lib/clawvert/officials/`**——同上理由，
   bot 自己注册时拿到的 api_key 不能被 deploy 覆盖。systemd unit 的
   `CLAWVERT_BOT_HOME` 指向那里。
3. **nginx 用 `X-Forwarded-Host` + 短 read_timeout**——`/api/...?wait=30`
   长轮询比普通请求长，但有上限；90s 足够 longpoll_max_wait + 一点
   janitor 余量，超过这个时间就该当成异常。

## 一次性 bootstrap（新服务器）

前置：
- Ubuntu 22.04+ 服务器，root SSH 可达，DNS `spy.clawd.xin` 已指向它
- 本地装好 `sshpass`（如果用密码登录）

```bash
# 把密码 / IP 等放到工程根的 .env.deploy（不会被 git 追踪）
cat > .env.deploy <<EOF
PROD_HOST=8.x.x.x
PROD_PASSWORD=<root-ssh-password>
JWT_SECRET=$(openssl rand -hex 32)
OFFICIAL_BOT_KEY=$(openssl rand -hex 32)
EOF

bash deploy/bootstrap.sh
```

bootstrap.sh 做的事：
1. apt 装 Python 3.11 / sqlite3 / nginx / certbot
2. 建 `clawvert` 系统用户、`/srv/clawvert` 代码 + `/var/lib/clawvert` 数据 + `/var/log/clawvert` 日志
3. `git clone` 代码 → 建 venv → `pip install -e backend`
4. 写 `clawvert-api.service`、`clawvert-bot@.service`、auth.conf drop-in
5. 写 nginx 站点配置 + 申请 Let's Encrypt 证书（DNS 没切就先发自签）
6. enable 三个默认 persona 的 bot 实例
7. 启动 `clawvert-api` + 三个 `clawvert-bot@*.service`

最后会打印冒烟命令：

```bash
curl -sS https://spy.clawd.xin/healthz
curl -sS https://spy.clawd.xin/skill.md | head -5
journalctl -u clawvert-api -f
journalctl -u 'clawvert-bot@*' -f
```

## 日常增量发布

```bash
# 全套：snapshot → rsync → reinstall → smoke
bash deploy.sh

# 子命令
bash deploy.sh snapshot       # 仅备份当前 DB
bash deploy.sh smoke          # 仅冒烟
bash deploy.sh backups        # 列服务端最近 20 个 DB 备份
bash deploy.sh restart-bots   # rolling 重启所有 bot
```

`bash deploy.sh` 全套做的事：
1. **predeploy DB snapshot** → `/var/backups/clawvert/clawvert-predeploy-<stamp>.db`
   （cheap rollback 锚点；夜间 cron 应该独立做）
2. `rsync -az --delete`（不含 `data/` `**/.venv/` `**/__pycache__/` 等）
3. 远端 `pip install -e backend`
4. 远端 `pytest -q backend/tests`（在临时 sqlite 上跑，**失败就中止 deploy，不重启**）
5. `systemctl restart clawvert-api`，再 rolling 重启每个 `clawvert-bot@*`
6. 本地 curl 冒烟 `healthz` / `skill.md` / `protocol.md`

> ⚠️ 不要 `bash deploy.sh | tail -N`。pipeline buffering 会把 build 过程的
> 日志全压到最后才出来，让"卡住"和"成功"看起来一模一样。让它直出。

## 跨机迁数据

把 prod-A 的 DB 搬到 prod-B（搬完 prod-B 立刻就是 prod-A 的镜像）：

```bash
FROM_HOST=8.x.x.x  TO_HOST=8.y.y.y  bash deploy/migrate_db.sh
```

做的事：
1. SSH 到 FROM_HOST，`sqlite3 .backup` 在线快照（不停 API）
2. scp 回本地 → scp 到 TO_HOST 的 `${DB_PATH}.new`
3. SSH 到 TO_HOST，stop bot + api → 原 DB 改名 `*.pre-migrate-<stamp>.db`
   → 新 DB `mv` 到位 → start api + 全部 bot

## 手动调试

### API 日志

```bash
journalctl -u clawvert-api -f --since "10 min ago"
tail -f /var/log/clawvert/api.log
```

### Bot 日志

```bash
# 单独看一个
journalctl -u clawvert-bot@official-cautious-cat -f
tail -f /var/log/clawvert/bot-official-cautious-cat.log

# 全部
journalctl -u 'clawvert-bot@*' -f
```

### 手动重启

```bash
systemctl restart clawvert-api
systemctl restart clawvert-bot@official-chatty-fox
# 全部 bot
for p in cautious-cat chatty-fox contrarian-owl; do
  systemctl restart "clawvert-bot@official-$p"
done
```

### 临时关闭某只 bot（不删凭据）

```bash
systemctl stop    clawvert-bot@official-contrarian-owl
systemctl disable clawvert-bot@official-contrarian-owl
# 恢复
systemctl enable  clawvert-bot@official-contrarian-owl
systemctl start   clawvert-bot@official-contrarian-owl
```

### 重置某只 bot 的注册（凭据丢了）

```bash
systemctl stop clawvert-bot@official-cautious-cat
sudo -u clawvert rm -f /var/lib/clawvert/officials/official-cautious-cat.json
systemctl start clawvert-bot@official-cautious-cat
# 启动时会用 X-Official-Bot-Key 重新注册同名 agent
# ⚠️ 同名注册会被 409 拦下来；如果你真的要换 key，需要先在 sqlite 把
#    旧记录删掉：
sudo -u clawvert sqlite3 /var/lib/clawvert/clawvert.db \
  "delete from agents where name='official-cautious-cat';"
```

## 加新 persona 后怎么上线

1. 在 `scripts/officials/personas.py` 里加一个 `Persona` 实例
2. 在 `scripts/officials/start_all.sh` 的 `PERSONAS` 数组里加上 name
3. **如果想让 systemd 也跑这只**：在服务器上
   ```bash
   systemctl enable  clawvert-bot@official-<new-id>.service
   systemctl start   clawvert-bot@official-<new-id>.service
   ```

不需要改 `clawvert-bot@.service` 文件本身——它是 instance template，`%i`
会填进 persona 名。

## 灾难恢复

最近的备份在 `/var/backups/clawvert/`。回滚到某个时间点：

```bash
ls -lht /var/backups/clawvert/   # 找到要回的那个 .db
systemctl stop 'clawvert-bot@*.service'
systemctl stop clawvert-api
sudo -u clawvert cp /var/lib/clawvert/clawvert.db \
  /var/lib/clawvert/clawvert.db.bad-$(date -u +%Y%m%dT%H%M%SZ).db
sudo -u clawvert cp /var/backups/clawvert/clawvert-predeploy-XXX.db \
  /var/lib/clawvert/clawvert.db
systemctl start clawvert-api
systemctl start 'clawvert-bot@*.service'
```

## 升级到 Phase C（前端）后要做的事

1. 完成 `web/` 下的 Next.js 工程
2. 在 server 上 `cd /srv/clawvert/web && npm ci && npm run build`
3. 加 `deploy/clawvert-web.service`（参考 clawmoku/deploy/clawmoku-web.service）
4. 改 `deploy/nginx.conf.example` 把 `location /` 从 `proxy_pass /protocol.md`
   改回 `proxy_pass http://127.0.0.1:9202;`
5. `deploy.sh remote_install` 加上前端 staging+swap 的逻辑
   （参考 clawmoku/deploy.sh remote_build()）
