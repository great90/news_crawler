# news_crawler —— 每日主题资讯 + 社媒热榜摘要邮件

每日自动调用 [DeepSeek Search API](https://api-docs.deepseek.com/guides/search) 采集 **AI / 游戏 / 游戏引擎 / 新工具** 四主题近 24 小时资讯，经 `deepseek-flash` 生成中文要点，可选附加知乎/微博/抖音热榜板块，合并为一封 HTML 邮件发送到你的邮箱。

**特性**

- 四主题 × 3 组查询（宽域 + 垂域 + 微信公众号文章），多路合并去重
- 同一事件多家报道自动聚合为一条（主链接 + 相关报道），不刷屏
- 社媒热榜直连本地聚合服务，**零 LLM 调用**，不影响成本
- 指数退避重试；单日 API 调用硬上限（成本保护）；某板块失败降级标注、不阻塞整封邮件
- 按日期幂等：同一天重复触发不会重复发送
- 全流程可演练：`--dry-run` 采集/摘要/存档但不发信

## 快速开始

要求：Python 3.10+，可选 Node 18+（仅社媒热榜板块需要）。

```bash
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

## `.env` 配置

| 配置项 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek 平台 API Key（搜索与摘要共用，付费） |
| `SMTP_HOST` / `SMTP_PORT` | 发件邮箱 SMTP 服务器（163：`smtp.163.com:465`；QQ：`smtp.qq.com:465`） |
| `SMTP_USER` / `SMTP_AUTH_CODE` | 发件邮箱账号与**授权码**（不是登录密码） |
| `MAIL_TO` | 收件邮箱（可与发件相同） |
| `SEND_TIME` | 计划任务触发时间（默认 08:00，注册任务时使用） |
| `TOPIC_COUNT` | 每主题采集条数上限（1-50，默认 10） |
| `DAILY_CALL_CAP` | 单日 API 调用硬上限（默认 200） |
| `SOCIAL_ENABLED` | 社媒热榜板块开关（默认 false） |
| `SOCIAL_API_BASE` | DailyHotApi 服务地址（默认 http://localhost:6688） |
| `SOCIAL_PLATFORMS` | 启用的平台（默认 zhihu,weibo,douyin） |
| `SOCIAL_COUNT` | 每平台拉取条数（默认 10） |

> 邮箱授权码获取：163 网页版邮箱 → 设置 → POP3/SMTP/IMAP → 开启 SMTP 服务 → 短信验证后生成授权码。
> `.env` 含密钥，已被 `.gitignore` 忽略，绝不入库。日志与存档亦不记录密钥。

## 社媒热榜（可选）

开启后邮件追加"社媒热点"板块（平台实时榜单：标题 + 热度 + 链接，直连不过 LLM）。

部署本地聚合服务 [DailyHotApi](https://github.com/imsyy/DailyHotApi)（MIT，无需 Docker）：

```bash
cd scripts/dailyhot
npm install
node start.mjs        # 监听 http://localhost:6688，保持运行
```

然后在 `.env` 中 `SOCIAL_ENABLED=true`。可把 `node start.mjs` 注册为登录启动任务实现常驻。

已知边界：

- 微博路由在部分环境服务端 500（上游反爬变动），表现为该平台小节标「暂不可用」，不影响其他平台与整封邮件。
- 小红书与 X/Twitter 无零成本可行路径（官方 API 收费、公开镜像不可达），暂不支持。
- 单平台失败自动降级，重试策略见 `news_crawler/social_source.py`。

## 每日定时（Windows）

```powershell
powershell -ExecutionPolicy Bypass -File scripts/register_task.ps1 -Time 08:00
```

注册两个计划任务：`news_crawler-daily`（每日定时）与 `news_crawler-logon`（登录补跑）。
错过触发点（关机）由 `StartWhenAvailable` 补跑；配合应用内按日期幂等，同日重复触发不会重复发送。

## 行为语义

- **失败重试**：搜索 / 摘要 / 发送任一环节指数退避重试至多 3 次；余额不足（402）、鉴权失败不重试。
- **最终失败**：写日志，并在邮件通道可用时另发一封失败告警邮件。
- **主题无结果**：邮件中该节标注「本日无新增」，其余照发。
- **幂等**：当天已成功发送后再次触发直接退出。强制重发：删除 `data/state.json` 后重新运行。
- **本地存档**：`data/archive/YYYY-MM-DD.json` 保留原始结果与最终摘要，便于回溯。
- **成本上限**：搜索 + LLM 调用超过 `DAILY_CALL_CAP` 即熔断，只发存量内容。

## 架构

```
topics(3组查询/主题) → search_client(DeepSearch) → 去重 → summarizer(批量摘要+同事件聚合)
social_source(DailyHotApi 热榜，零 LLM) ──────────────┐
                                                      ↓
                          runner(编排/成本熔断) → renderer(Jinja2) → mailer(SMTP SSL)
                                                      ↓
                                            state(按日期幂等) / archive(JSON 存档)
```

| 模块 | 职责 |
|---|---|
| `news_crawler/__main__.py` | CLI 入口（`run` / `search` / `--dry-run` / `--date`） |
| `topics.py` | 查询模板：每主题 3 条（宽域 + 垂域 + `site:mp.weixin.qq.com` 公众号变体），带 24h 时间窗与 ASCII 守卫 |
| `search_client.py` | DeepSeek Search API 客户端（重试、鉴权快败） |
| `social_source.py` | DailyHotApi 客户端（逐平台拉取、重试、失败降级为独立小节） |
| `summarizer.py` | deepseek-flash 批量摘要 + 同事件聚合（吸收报道并入 sources） |
| `digest.py` | 数据模型（Digest / DigestSection / DigestItem） |
| `renderer.py` | Jinja2 HTML 渲染（显式 autoescape，板块主题色） |
| `mailer.py` | SMTP_SSL 465 发送；鉴权失败快败；瞬态错误退避重试 |
| `runner.py` | 编排、成本熔断、统计（含社媒分平台统计） |
| `state.py` / `config.py` | 幂等状态 / `.env` 解析与校验 |

## 扩展主题

编辑 `news_crawler/topics.py` 的查询模板即可新增主题。注意：**检索词必须为纯英文/ASCII**——DeepSeek Search API 对含中文的查询返回 0 结果（代码内已加 ASCII 守卫，违规模板会在启动时报错）；公众号变体走 `site:mp.weixin.qq.com`，该索引无日期，自动豁免时间窗。

## 开发

```bash
pip install pytest
python -m pytest tests -q    # 55 单测，全 mock 无网络，无密钥可跑
```

## 致谢

- [DeepSeek Search API](https://api-docs.deepseek.com/guides/search)
- [DailyHotApi](https://github.com/imsyy/DailyHotApi)（MIT）

## License

[MIT](LICENSE)
