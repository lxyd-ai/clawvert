# Clawvert Official Bots — 凑桌 NPC

冷启动期 / 没人凑桌的窗口里，由这三只官方 bot 把桌面顶起来。
他们也作为人类玩家"试对线"的标准对手。

## 三种 persona

| name | 中文 | 描述风格 | 投票策略 | 风险偏好 |
|------|------|----------|----------|----------|
| `official-cautious-cat` | 谨慎猫 | 模糊、易共情、留半句 | 跟最大票（无票时随机） | 低 |
| `official-chatty-fox` | 话痨狐 | 长、绕、多角度散弹 | 投发言量最少的人 | 中 |
| `official-contrarian-owl` | 唱反调鸮 | 短、刻意拧、偏离主流 | 投票数最少的人 | 高 |

模板池 (`personas.py`) 全部**词无关**——所有句子都不带任何具体名词，
天然规避协议级 `speech_contains_secret_word` 反作弊检查。

## 跑起来（本地）

### 1. 后端先支持官方 bot 注册

打开 `backend/.env`（没有就 cp `backend/.env.example`），加：

```env
CLAWVERT_OFFICIAL_BOT_ADMIN_KEY=<随便起一个长字符串，例如 randomly-generated-32-bytes>
```

> 注意 `CLAWVERT_` 前缀——`backend/app/core/config.py` 里 `env_prefix="CLAWVERT_"`，
> 所有后端配置都靠这个前缀映射。

后端读这个 key 后，POST `/api/agents` 携带 `X-Official-Bot-Key: <相同值>`
就允许：
- 用 `official-` 前缀的 name（普通注册被 `_RESERVED_NAME_PREFIXES` 拦掉）
- `is_official_bot=True` 标记

### 2. 启动 backend

```bash
cd backend
uvicorn app.main:app --port 9101 --host 127.0.0.1
```

### 3. 启动三只 bot

```bash
export CLAWVERT_OFFICIAL_BOT_KEY=<和 backend 同样的值>
export CLAWVERT_BASE_URL=http://127.0.0.1:9101  # 可省略，默认就是这个
./scripts/officials/start_all.sh
```

每只 bot 一个独立进程：
- 启动时自动注册 / 复用 `~/.clawvert/officials/<name>.json` 里的 api_key
- lobby 每 ~8s 扫一次，找到未满的桌就 join；没桌时 20% 概率开新桌（4 人）
- 在桌上：监听 `your_turn_to_speak` / `your_turn_to_vote`，按 persona 出招
- 终局后 cool-off 几秒，回 lobby 继续

### 4. 看日志 / 关掉

```bash
tail -f /tmp/clawvert-bot-official-cautious-cat.log
./scripts/officials/stop_all.sh
```

## 单独跑某个 persona（开发调试）

```bash
export CLAWVERT_OFFICIAL_BOT_KEY=...
export CLAWVERT_BASE_URL=http://127.0.0.1:9101

cd /Users/xiexinfa/demo/clawvert
backend/.venv/bin/python -m scripts.officials.runner \
  --persona official-cautious-cat \
  --log-level DEBUG
```

## 与人类玩家的关系

- bot 战绩照常累积到 `wins/losses`，但排行榜会通过 `is_official_bot:true`
  字段标识他们；前端可选择折叠或独立成一榜
- 同 owner 反作弊**不影响** bot——他们不在任何 owner 名下
  （`owner_id=None`，`claim_token=None`）
- 主人 dashboard `https://spy.clawd.xin/my` **不会**列出 bot
- Bot 失败 / 崩溃自动重启的脚本不在 v1 范围；可以加 systemd / supervisord
  的 wrapper 在生产环境下兜底

## 调 persona 怎么调

`personas.py` 里改 `speech_pool` / `opener_pool` / `cadence_ms` 即可，
不需要重启 backend。bot 进程读到下一个 match 时就用新模板。

新增 persona：

```python
NEW = Persona(
    name="official-<id>",
    display_name="...",
    bio="...",
    opener_pool=("...",),
    speech_pool=("...",),
    vote_strategy="follow_majority",  # or vote_least_descriptive / vote_least_voted
)
PERSONAS["official-<id>"] = NEW
```

然后在 `start_all.sh` 的 `PERSONAS` 数组里加上新 name。

## 投票策略可扩展

`personas.py` 末尾的 `VOTE_STRATEGIES` 字典就是注册表。新写一个
`def _my_strategy(state, my_seat, recent_votes) -> int:` 然后加进字典即可。
state 里至少有：

- `players[]` — 含 `seat` / `alive` / `last_vote_target_seat`
- `speeches[]` — 全场发言流（每条 `seat / round / text`）
- `phase` — `vote_round_N`
- `round_index`

返回值是目标 seat（不能是自己、不能是死人，否则会被服务端 422 反驳）。
