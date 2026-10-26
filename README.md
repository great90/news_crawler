# news_crawler —— 每日四主题资讯摘要邮件

每日自动采集 **AI / 游戏 / 游戏引擎 / 新工具** 四主题当日资讯，经 LLM 生成中文要点，合并为一封 HTML 邮件发送到你的邮箱。

搜索源可切换（`SEARCH_PROVIDER`）：默认 `feed`（免费订阅源：本地 DailyHotApi 路由 + 量子位/Hacker News 原生 RSS，公众号不采集）；`websearch`（[BigModel web_search](https://docs.bigmodel.cn/api-reference/model-api/web-search)，与摘要 LLM 同一 API Key、独立计费需购资源包）；`deepseek`（[DeepSeek Search API](https://api-docs.deepseek.com/guides/search)，遗留选项）。

## 快速开始

```bash
cd news_crawler
pip install -r requirements.txt

# 1. 复制配置模板并填入真实值
cp .env.example .env

# 2. 单主题搜索调试
python -m news_crawler search --topic ai

# 3. 全链路演练（采集→去重→摘要→存档，不发邮件）
python -m news_crawler run --dry-run

# 4. 正式运行（采集→摘要→发邮件→记幂等状态）
python -m news_crawler run
```

## .env 配置

| 配置项 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | API Key（BigModel 或 DeepSeek，搜索与摘要共用） |
| `SEARCH_PROVIDER` | 搜索源：`feed`（默认，免费订阅源：本地 DailyHotApi + 原生 RSS）\| `websearch`（BigModel web_search，独立计费）\| `deepseek`（遗留） |
| `SMTP_HOST` / `SMTP_PORT` | 发件邮箱 SMTP 服务器（163：`smtp.163.com:465`） |
| `SMTP_USER` / `SMTP_AUTH_CODE` | 发件邮箱账号与**授权码**（不是登录密码） |
| `MAIL_TO` | 收件邮箱（可与发件相同） |
| `SEND_TIME` | 计划任务触发时间（默认 08:00，注册任务时使用） |
| `TOPIC_COUNT` | 每主题采集条数上限（1-50，默认 10） |
| `DAILY_CALL_CAP` | 单日 API 调用硬上限（NFR-3 成本保护，默认 200） |
| `SOCIAL_ENABLED` | 社媒热榜板块开关（默认 false） |
| `SOCIAL_API_BASE` | DailyHotApi 服务地址（默认 http://localhost:6688） |
| `SOCIAL_PLATFORMS` | 启用的平台（默认 zhihu,weibo,douyin） |
| `SOCIAL_COUNT` | 每平台拉取条数（默认 10） |
| `PUSH_CHANNELS` / `BARK_URL` / `SERVERCHAN_SEND_KEY` | 手机推送（可选）：投递完成后推摘要到 Bark/微信，逗号分隔通道 |

> 163 邮箱授权码获取：网页版邮箱 → 设置 → POP3/SMTP/IMAP → 开启 SMTP 服务 → 短信验证后生成授权码。
> `.env` 含密钥，已被 `.gitignore` 忽略，绝不入库。

## 社媒热榜（可选，REQ-20261020-2）

开启后每日邮件追加"社媒热点"板块（知乎/微博/抖音等平台实时榜单，标题+热度+链接，不经 LLM）。

部署本地聚合服务 [DailyHotApi](https://github.com/imsyy/DailyHotApi)（Node 18+，无需 Docker）：

```bash
cd scripts/dailyhot
npm install
node start.mjs        # 监听 http://localhost:6688，保持运行
```

然后在 `.env` 中 `SOCIAL_ENABLED=true`（平台/条数见下方配置表）。可把 `node start.mjs` 注册为登录启动任务实现常驻。
已知问题：微博路由在部分环境服务端 500（上游反爬变动），表现为该平台小节标「暂不可用」，不影响其他平台与整封邮件；小红书与 X/Twitter 无零成本可行路径，本期不支持（详见 .project/requirements）。

## 每日定时

```powershell
powershell -ExecutionPolicy Bypass -File scripts/register_task.ps1 -Time 08:00
```

注册两个 Windows 计划任务：`news_crawler-daily`（每日定时）与 `news_crawler-logon`（登录补跑）。
错过触发点（关机）由 `StartWhenAvailable` 补跑；应用内按日期幂等（`data/state.json`），同日重复触发不会重复发送。

## 行为语义（规格 REQ-20261020-1 冻结件）

- 失败重试：搜索 / 摘要 / 发送任一环节指数退避重试至多 3 次；余额不足（402）、鉴权失败不重试。
- 最终失败：写日志，并在邮件通道可用时另发一封失败告警邮件。
- 某主题当日无结果：邮件中该节标注「本日无新增」，其余照发。
- 幂等：当天已成功发送后再次触发直接退出。强制重发当天：删除 `data/state.json` 后重新运行。
- 同事件聚合：多家媒体报道同一事件时合并为一条（主链接 + 相关报道），避免重复刷屏。
- 本地存档：`data/archive/YYYY-MM-DD.json` 保留原始结果与最终摘要，便于回溯。

## 开发

```bash
python -m pytest tests -q    # 26+ 单测，无密钥可跑
```

需求规格、领域模型、追溯矩阵等流程产物见 `../.project/`。
