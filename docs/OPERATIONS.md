# 运维手册（Operations）

> 面向运营者的完整操作参考。项目介绍与架构见仓库根 [README](../README.md)。

## 日常操作

```bash
source .venv/bin/activate     # 或直接用 .venv/bin/rebas
rebas collect                 # 采集（建议每天早晚各一次；--force 忽略间隔）
rebas enrich                  # 外部指标增补（OpenAlex 作者 h 指数等；publish 也会自动跑）
rebas publish                 # 出刊：增补→粗筛→主编→取材→背景调查→核查→撰写→渲染→瘦身
rebas render                  # 只重渲染（改前端/样式后，零 token；全量重建）
rebas status                  # 池子与配置状态
rebas backfill --date X       # arXiv 按日回填（调画像关键词后的后悔药）
rebas prune --days 7          # 手动瘦身（publish 尾部也会自动跑）
```

本地预览：直接打开 `site/index.html`，或 `cd site && python3 -m http.server 8000`。

## 结构速览

- `config/config.toml` —— 全局配置（llm 后端与角色型号、出刊参数；`site_keep_days=7` 往期完整页面保留一周，更早进归档存目——渲染期策略，调大重渲染可找回）
- `config/sources.toml` —— 信息源定义（enabled 开关随时增删）
- `config/profiles/*.toml` —— 板块兴趣画像；`[reader]` 段 = 读者画像与**背调切入角度**（assumed=已掌握 / explain=要铺垫）：技术板块角度=概念解释，商业/艺术=背景故事，调角度直接改画像文本
- `config/prompts/*.md` —— agent 提示词模板（string.Template `$` 占位符，改文风不动代码）；**style.md = 全刊文风单点调节**；researcher*.md = 背景调查（含 30 天往期查阅两轮协议 + **调查补充**：仅标题级的新闻/repo 选题、以及原文精读拿不到的论文（付费墙顶刊无预印本，以取材期缓存为准）标注【需调查补充】，背调联网搜索补事实细节，`research_facts_max=0` 关闭；搜索走 `[llm] search_roles`，须写在 `[llm.roles]` 表头之前）；checker_background.md = 背景审核（概念宁删勿留，facts 按新闻口径放宽）
- `src/rebas/collect/` —— 十二类采集器（rss/gnews/arXiv/HF×2/HN/GH/期刊×2/Reddit/X 镜像/Truth 归档；HTTP 用 urllib 封装，**勿换 httpx**——Cloudflare 按 TLS 指纹拦；新源冒烟用生产同款客户端，curl 通过≠urllib 可用）
- `src/rebas/llm/` —— 模型抽象层（codex_cli 主力 / openai_api 预留）
- `src/rebas/agents/` + `pipeline.py` —— 出刊各阶段 + 编排（issues.status 断点续跑）
- `src/rebas/render/export.py` —— SQLite → `web/data/*.json` 数据契约 + 调 Astro 构建；构建期 LaTeX→MathML
- `web/` —— 前端（Astro + TS，零 JS 渲染 + PWA；渐进增强仅 SW 注册与移动端分享键两处）→ `site/`；设计 token 在 `web/src/styles/global.css`；**`build.inlineStylesheets="always"` 必须保留**（否则 file:// 直开与子路径托管全裂）；艺术/设计板块多图排版（`raw_items.image_urls` 图库 ≥2 时导出 `images`，头条/专题卡/报道页多图版式，其余板块单图）
- `data/rebas.sqlite` —— 原始池 + 管线产物（gitignored）；`data/paper_cache/` = 精读原文临时缓存（writer 用完即删，prune 兜底清扫）
- `.codex/`、`.secrets/` —— 凭证（gitignored，chmod 700）

## 云端部署（四主批 + 兜底批备刊模型）

**架构**：站点 = Cloudflare Pages 免费层（wrangler 直传）；管线 = $5-7/月 VPS（**零入站端口**——只出站抓源/调 LLM/推 Pages）。

刊物日历按达拉斯（`timezone=America/Chicago`）。白天四批备**明日刊**（每批 ≈20-30 次 LLM 调用，各占订阅的一个 5h 额度窗口），达拉斯 00:00 零 token render 翻牌上线：

| 达拉斯时刻 | 批次 | 内容 |
|---|---|---|
| 00:01 | 批1 | 翻牌今日刊 + 自愈补齐 + 部署 + 备明日（学术+艺术，慢内容） |
| 05:00 | 批2 | 备明日（开源+数据；`--boards` 累积带上批1板块=顺延其未完成的） |
| 10:00 | 批3 | 备明日（量化；累积带上批1-2板块） |
| 15:00 | 批4 | 备明日（科技+商业=美股收盘后最鲜）+ 全板块收尾扫尾 + **补充轮**（--refill：前三批备的板块若选题 < refill_min_topics，用白天新采集的候选补选不重复的新选题；选题够则不动） |
| 20:00 | 批5 | **兜底收尾**=批4的重试：批4因额度耗尽/中途失败没做完时，从状态断点补完剩余并推进状态；一切正常时零 token 空转（实测 0.7 秒） |

机制：`publish --boards a,b` = 部分模式（只跑指定板块、不推进状态不渲染）；收尾批幂等扫尾 + `--refill` 补充轮兜底薄板块；导出层发布闸门只出 `issue_date ≤ 达拉斯今天` 的期次。**顺延/兜底不做显式额度检测**：靠 issue 状态检查点 + 板块级幂等守卫——做过的零 token 跳过，没做完的自动重试。

**慢车道采集**（每小时 :25，独立 cron）：`rebas collect --paced` 只跑 `pace_seconds > 0` 的源，串行抓取、源间隔 pace 秒——Reddit 等按 IP 严格限速的源（实测 ~1 请求/分钟，连发即 429）绝不能进批次采集的 8 线程并发池。到期判定与常规源共用 fetch_interval（Reddit 源 6h → 每天 4 次滴灌）；独立 `data/paced.lock`（批次的 cron.lock 会被 LLM 长任务占数小时，慢车道不等它）；日志 `data/logs/paced.log`。候选照常入池，出刊管线零感知。新增慢车道源只需在 sources.toml 配 `pace_seconds`。

**部署三步**（脚本在 `scripts/`，Ubuntu 24.04）：
1. 本机首次搬家：`scripts/vps_sync.sh --with-secrets root@<ip>`（之后日常推代码不带 `--with-secrets`——VPS 是数据与凭证唯一正本）
2. VPS 一键就绪：`bash /opt/rebas_daily/scripts/vps_bootstrap.sh`（时区/Node 22/codex+wrangler/venv/冒烟/装 crontab，幂等）
3. Cloudflare API Token（Pages:Edit）+ Account ID 追加进 `.secrets/.env`；跑 `bash scripts/cron_batch.sh 4` 验证全链路

监控：healthchecks.io 建 5 个 check（`rebas-batch-1..5`，Period 1d / Grace 1h），crontab 设 `HEALTHCHECK_URL=https://hc-ping.com/<ping-key>/rebas-batch-`（末尾连字符；勿用单 check UUID 直拼——批号会被判成失败退出码）。rebas-batch-5 未建时 ping 404 被 `|| true` 吞掉，不影响批次本身。

## 接 cron / 运维须知

- **时钟**：发布闸门与 issue_date 都按达拉斯；闸门放行条件 `status∈(written,rendered) 且 date≤今天`——提前翻牌需临时把渲染时钟拨到更早的时区跑一次 build_site
- **环境**：cron 精简 PATH → crontab 导出 PATH 或设 `REBAS_NPM`；`PYTHONUTF8=1` 防中文日志编码崩；`PROJECT_ROOT` 按包路径推断，仅对 editable 安装成立
- **自愈**：publish 有进程锁；date 缺省自动续跑最近未完成期次；`--force-stage X` 级联清理该阶段及下游产物后重跑；**force-stage 与 --boards 互斥**（清理级联按整期生效）
- **已知限制（Phase 2）**：跨板块去重（url_canonical 全局唯一，正解 item↔board 关联表）；content_hash 已入库未启用

## 待办 / 观察

- [ ] M5 试运行：连跑一周观察（文风稳定性、聚类、跨日去重、订阅额度）
- [ ] 背景调查观察：概念/故事挑选准度、织入自然度、审核 fix/drop 率、follow_up 连续报道实战
- [ ] 周榜生成：设计已定（thread_key 事件线，周日出），攒一周数据后实现
- [ ] enrich 二期：HN 反查 + HF 论文↔仓库联动；alphaXiv upvotes
- [ ] 旧 Jinja 渲染层（`render/site.py` + templates）已停用，可删

测试：`.venv/bin/pytest -q tests/`（80 项）。

## 管理后台（2026-07-05 上线）

VPS 上的编辑部控制台：备稿状态监控、板块画像（关键词/权重/读者画像）与出刊参数在线编辑、报道点赞点踩（反馈池，供后续选题加权算法）。

- 服务：`systemd` 单元 `rebas-admin`（`scripts/rebas-admin.service`），只听 `127.0.0.1:8787`，零入站端口原则不破。
- **常驻服务与代码版本错位**：admin 进程不像管线每次冷启动，代码/配置同步后必须重启——`vps_sync.sh` 尾部已自动 `systemctl try-restart rebas-admin`（2026-07-05 踩坑：旧进程的 `Source` 类读不了带新字段的 `sources.toml`，后台整体 500）。
- 访问（备用，SSH 隧道）：`ssh -L 8787:127.0.0.1:8787 <user>@<vps>`，浏览器开 `http://localhost:8787`。
- 账号：`rebas admin-seed --email <邮箱>`（交互式输密码，scrypt 入库不存明文）；JWT 有效期 180 天，密钥在 `.secrets/admin_jwt.secret`（换密钥即吊销所有已发 token）。
- 正式入口（2026-07-05 已挂通）：**https://admin.rebasdaily.com** —— Cloudflare Tunnel（VPS systemd 服务 `cloudflared`，dash 里 tunnel 名 rebas-admin，Public hostname → `http://localhost:8787`，出站连接不开端口）。可选加固：Zero Trust → Access 给该域套邮箱 OTP 策略做边缘拦截。
- **画像/参数编辑写的是 VPS 上的 config 文件**：改完记得本机跑 `scripts/vps_pull_config.sh <user>@<vps>` 拉回入 git，否则下次 `vps_sync.sh` 会用本地旧版覆盖线上改动。
- 反馈数据在 `feedback` 表（topic_id/vote/updated_at），选题加权算法未实现——先攒信号。
