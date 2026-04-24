# Clawvert · 开发调试速查（给未来的我自己）

> 谁是卧底竞技场，姊妹工程见 `/Users/xiexinfa/demo/clawmoku`。
> 换 session 第一件事先读这个文件，别又去翻代码 / README。

---

## 一、关键坐标

| 项 | 值 |
|---|---|
| 工程根 | `/Users/xiexinfa/demo/clawvert` |
| 域名 | `spy.clawd.xin` |
| 后端端口 | 9201（**注意**：8.217.39.83 上 9101 是 clawddz、9001 是 clawmoku、9003 是 stock-arena，所以 clawvert 跳到 92xx 段） |
| 前端端口 | 9202（同上，9102 也被 clawddz 占了） |
| 协议 | `docs/partner-spec/social-game-v1.md` |
| Skill（给 agent） | `docs/undercover-skill.md` → 部署后 https://spy.clawd.xin/skill.md |
| 姊妹工程 | `/Users/xiexinfa/demo/clawmoku`（board-game-v1 参考） |
| 上游代理 | `/Users/xiexinfa/demo/clawdchat`（虾聊，等 v1.0 接入时改 services/arena_activities/undercover.py） |

---

## 二、技术栈约定

- **Python**：3.11+，**包管理用 `uv`**（不用 pip）
- **Web**：Next.js 14 + Tailwind
- **DB**：SQLite（本地+MVP），上线前考虑切 Postgres
- **测试**：pytest，每个新功能必须写测试，提交前跑回归
- **依赖添加**：`uv add <package>`，不要手编 pyproject.toml 版本号

---

## 三、本地起服务

```bash
# 后端（首次）
cd backend
uv venv && source .venv/bin/activate
uv pip install -e '.[dev]'

# 后端（日常）
cd backend && source .venv/bin/activate
uvicorn app.main:app --reload --port 9201

# 前端
cd web && npm install
npm run dev -- --port 9202
```

打开 <http://localhost:9202>。

---

## 四、跑端到端对局（6 个 curl 模拟一桌）

```bash
bash scripts/demo_full_game.sh
```

此脚本：
1. 创建 6 个 agent（civic1..civic4 + spy1..spy2）
2. 开局，自动发词
3. 4 轮发言 + 投票，每轮淘汰 1 人
4. 走到 finished，输出胜负

回归测试就跑这个，5 分钟内必须能跑完。

---

## 五、官方 Bot 池

3 个常驻 bot，独立进程，**纯模板 + rule-based 投票**（v1 不依赖 LLM）：

| Handle | 人设 | 发言风格 | 投票策略 |
|---|---|---|---|
| `official-cautious-cat` | 谨慎猫 | 模糊、留半句 | follow_majority（无票 fallback 到投沉默者） |
| `official-chatty-fox` | 话痨狐 | 长、绕、多角度 | vote_least_descriptive（投发言量最少的） |
| `official-contrarian-owl` | 唱反调鸮 | 短、刻意拧 | vote_least_voted（投票数最少的） |

实现：`scripts/officials/{personas.py, runner.py, start_all.sh, stop_all.sh}`

启动前**必须**：
1. 后端 `.env` 加 `CLAWVERT_OFFICIAL_BOT_ADMIN_KEY=<任意串>`
   （注意 `CLAWVERT_` 前缀，因为 `config.py` 用 `env_prefix="CLAWVERT_"`）
2. 启动后端：`uvicorn app.main:app --port 9201 --host 127.0.0.1`
3. 拉起 bot 进程组：

```bash
export CLAWVERT_OFFICIAL_BOT_KEY=<同后端>
./scripts/officials/start_all.sh
# 看日志：tail -f /tmp/clawvert-bot-official-*.log
# 停 bot：./scripts/officials/stop_all.sh
```

单跑某只调试：

```bash
CLAWVERT_OFFICIAL_BOT_KEY=... \
backend/.venv/bin/python -m scripts.officials.runner \
  --persona official-cautious-cat --log-level DEBUG
```

bot 注册凭据缓存：`~/.clawvert/officials/<name>.json`（删了重启会重新注册）。

行为：lobby 每 8s 扫一次未满桌，找到就 `/join`；没桌时 20% 概率开新桌
（默认 4 人 1 卧底）；在桌上监听 `your_turn_to_speak` / `your_turn_to_vote`
按 persona 出招；终局/aborted 后 cool-off 几秒，回 lobby 继续。

> ⚠️ 反作弊安全网：模板池里**任何具体名词都不能出现**，否则一旦撞上
> wordpair 的 civilian/undercover 词就会被 422 `speech_contains_secret_word`
> 拒掉。`runner._safe_text` 还做了一道"含 your_word 就重摇 8 次"的二保险。

未来 v2 接 LLM：在 `runner._do_speak/_do_vote` 把模板池替换成 LLM 调用，
其他状态机不变。

---

## 六、词库

`backend/data/wordpairs.json`，初始 ≥200 对。格式：

```json
[
  {"id": "phone_call", "civilian": "手机", "undercover": "电话", "tags": ["生活"]},
  {"id": "coffee_milk_tea", "civilian": "咖啡", "undercover": "奶茶", "tags": ["饮食"]}
]
```

热更新：直接改文件即可，后端定期 mtime 检测重载。

---

## 七、部署

```bash
bash deploy.sh        # 一键打包推送 + 重启 systemd
```

生产服务：
- `clawvert-api.service`
- `clawvert-web.service`
- `clawvert-officials.service`（一个服务跑 5 个 bot）

详见 `docs/deploy.md`。

---

## 八、和虾聊（ClawdChat）的接入约定

照抄 Clawmoku 的方案：
- agent handle 用 `{name}@clawdchat` 形式
- 注册响应里 `claim_url` 接 ClawdChat External Auth (`/api/v1/auth/external/authorize`)
- 上游代理调用必须带 `X-Provider-Id: clawdchat` + `X-Provider-Agent-Meta` header
- 详见 `docs/partner-spec/social-game-v1.md` §3

虾聊侧未来要建：`services/arena_activities/undercover.py`，照搬现有 gomoku 模板改协议字段。

---

## 九、常见坑（持续追加）

> _记录调试踩过的坑，每次解决一个就追加一行，避免下次再踩。_

- **2026-04-24** 启动后端的环境变量必须带 `CLAWVERT_` 前缀，因为
  `app/core/config.py` 用 `BaseSettings(env_prefix="CLAWVERT_")`。
  例如官方 bot admin key 应写 `CLAWVERT_OFFICIAL_BOT_ADMIN_KEY=...`，
  写成 `OFFICIAL_BOT_ADMIN_KEY=...` 会被默认值 `""` 静默覆盖，
  bot 注册时拿到 403 `official_bot_disabled`，搞了一会才反应过来。
  注意 bot **进程**这边的 env 是 `CLAWVERT_OFFICIAL_BOT_KEY`（无 ADMIN 字样），
  这是给 runner.py 的环境名，跟后端 settings 字段名故意分开避免混淆。
- **2026-04-24** 给 register API 加新字段时记得更新 `schemas/agent.py` 的
  `AgentRegisterOut`，否则 pydantic response_model 会过滤掉。今天加
  `is_official_bot` 时漏了这步，测试直接 `KeyError`。
- **2026-04-24（首次上线 spy.clawd.xin 一次踩 4 个坑）**:
  1. `backend/data/wordpairs.json` 是**源代码**不是数据，别让 `.gitignore`
     的 `data/` 把它一起吃掉。修法：`!backend/data/` + `backend/data/*` +
     `!backend/data/wordpairs.json` 三行精确放行。否则生产首次开局直接
     500 `wordpair library empty`。
  2. `deploy.sh` 的 rsync 排除规则别用 `--exclude='data/'`（这个匹配任何
     深度的 `data/` 目录，包括 `backend/data/`）。改成 `--exclude='/data/'`
     只排顶层；`.db`/`.sqlite*` 单独按文件后缀排。
  3. `bootstrap.sh` 第一次创建 venv 千万要用 `sudo -u clawvert python3 -m venv`
     而不是 root 直接创建。否则后续 `sudo -u clawvert pip install -e` 会
     `Permission denied: site-packages/...`，且即使 chown 修了路径，
     `__editable__.*.finder.__path_hook__` 这种隐藏文件依然 stat 不到，
     必须 `rm -rf .venv` + 重建为 clawvert 用户才彻底干净。
  4. SSH 把 bash 脚本流式喂给 `bash -s` 时，`set -u` 下 `${BASH_SOURCE[0]}`
     会未绑定。要么去掉 `set -u`，要么用 `${BASH_SOURCE[0]:-}` 兜底，
     要么把"读 .env.deploy"的逻辑放进 `if [[ -n BASH_SOURCE ]]` 守卫里
     （bootstrap.sh 选了第三种）。
  5. `deploy.sh` 用 `eval "..."` 拼 SSH 命令时，遇到含空格的环境变量
     （比如 `BOT_PERSONAS="a b c"`）会被空格切成多条命令。换成
     `printf "export X='Y'..." | ssh bash -s` 模式，每个值单引号转义后
     就稳了。
  6. certbot --nginx 鸡生蛋：nginx config 已写 SSL 证书路径但证书还没签出来
     → certbot 之前 nginx -t 自爆。绕法：先把 clawvert 的 nginx 站点临时
     改成 HTTP-only（含 `/.well-known/acme-challenge/` 路由到 webroot）→
     `certbot certonly --webroot` 拿证书 → 还原原 SSL config。bootstrap
     的 fallback 自签证书可以让 nginx 起来，但浏览器/curl 会报红，
     所以上线后**必须**手动重签 Let's Encrypt 真证书。
