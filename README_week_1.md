# 第 1 周：环境搭建 + 邮件渠道基础 + Focus 1（`deploy_week_1.sh`）

Example Hotel Email AI PoC：Amazon Connect Email 渠道，flow 中的 Lambda 调用 Amazon Bedrock Claude 处理客人来信。场景说明、动态流程图、定制难易度和 4 周计划见 `index.html`（浏览器直接打开）。

本文件同时包含 4 周通用的说明（目录、运行时序、通用变量、已知限制）。后续各周见 `README_week_2.md`、`README_week_3.md`、`README_week_4.md`。

## 本周启用的场景

`ENABLED_CAPABILITIES=KB`

| 场景 | 需求文档对应 | 定制难易度 |
|---|---|---|
| 酒店基础信息问答（知识库） | Focus 1: Hotel General Information | 低 |
| 多语言：英文 / 简体 / 繁体 | 功能评估准则 | 低 |
| 新邮件 / 回复邮件识别与多轮上下文 | 非 Focus（Req 5、Req 6） | 低 |
| 安全防护：垃圾/病毒、钓鱼、自动回复防回环、重复收件 | 非 Focus（Req 27、官方文档邮件回环要求） | 低 |
| 聊天转邮件只由人工处理 | 非 Focus（Req 2） | 低 |
| 多邮箱路由与签名 | 非 Focus（Req 1） | 低 |

预订（Area 2）和示例会员俱乐部（Focus 3/4）的邮件本周会被识别和打标，但 AI 只起草，进入 General 队列由人工回复，不会自动发送。

## 前置条件

- AWS CLI v2、python3；凭证所属账号与 Connect 实例一致。
- Connect 实例已启用 Email 渠道；实例所在区域已开通 Claude Haiku 4.5 和 Claude Sonnet 4.6 的模型访问。
- 邮件正文存储（EMAIL_MESSAGES）：已配置则直接使用；未配置时设置 `EMAIL_BUCKET_NAME`，脚本会建私有加密桶并关联。
- 知识库已用 `./setup_kb.sh` 准备好（见 `README_kb.md`）。仓库里已带一份由 `00_Vendor KB (pdf)` 生成的本地索引，不换文档可以跳过。

## 执行

```bash
export POC_NOW='2026-06-15T10:00:00+08:00'     # 假数据库存是 2026 年 7–10 月，演示时固定“当前时间”
./deploy_week_1.sh
```

脚本会用中文交互询问：

| 问题 | 默认 | 说明 |
|---|---|---|
| Connect 实例 ARN | 上次的选择；首次为 `list` | 可以直接粘贴 ARN。回车选 `list` 时，会询问区域（默认 `aws configure` 的区域），列出该区域的实例后按序号选择 |
| 资源命名前缀 | 上次的选择；首次为 `sl` | 小写字母开头，由小写字母和数字组成，最多 10 位 |
| 确认 | Y | 先列出实例和全部资源名称，确认后才开始部署 |

选择会保存在 `PoC/.deploy_settings`，第 2–4 周的脚本和 `setup_kb.sh` 默认沿用，保证每周都部署到同一个实例、使用同一套名称。同一个实例改用新的前缀，会部署一套新的、互不影响的资源，脚本会给出提醒。

非交互运行（CI 等）：

```bash
CONNECT_INSTANCE_ARN='arn:aws:connect:us-west-2:<acct>:instance/<id>' NAME_PREFIX=sl ASSUME_YES=true \
EMAIL_BUCKET_NAME=<仅在未配置邮件存储时需要> ./deploy_week_1.sh </dev/null
```

### 资源命名（前缀 `<p>`，示例为 `sl`）

| 资源 | 命名规则 | 前缀 sl 时 |
|---|---|---|
| Lambda | `<p>-email-ai-triage` / `<p>-email-ai-agent` | `sl-email-ai-triage` / `sl-email-ai-agent` |
| IAM 角色 / 策略 | `<p>-email-ai-lambda-role` / `<p>-email-ai-policy` | `sl-email-ai-lambda-role` |
| DynamoDB 表 | `<p>-email-ai-poc` | `sl-email-ai-poc` |
| Connect flow | `<P>-Email-AI-Inbound` | `SL-Email-AI-Inbound` |
| Connect 队列 | `<P>-Email-General` / `-Security`（第 2 周 `-Reservations`，第 4 周 `-LOYALTY`） | `SL-Email-General` … |
| 路由配置文件 | `<P>-Email-AI-Agents` | `SL-Email-AI-Agents` |
| Connect 邮箱 | `<p>_reservations@`、`<p>_rewards@`、`<p>_reservations.pen1@`、`<p>_reservations.pen2@`、`<p>_reservations.szx1@`、`<p>_reservations.hgh1@` | `sl_reservations@connect-us-email.email.connect.aws` … |
| 知识库（setup_kb.sh） | `<p>-email-ai-kb`、`<p>-email-ai-kb-role` 等 | `sl-email-ai-kb` |

`<P>` 是大写的前缀。Lambda 通过环境变量 `MAILBOX_PREFIX`（如 `sl_`）去掉邮箱前缀后，再按 `hotels.json` 的规则匹配签名和默认酒店：例如 `sl_reservations.pen1@` 对应 Seaview Resort。单项名称仍可用 `TABLE_NAME`、`FLOW_NAME`、`QUEUE_*` 等环境变量覆盖。

## 脚本做了什么

1. 交互确认 Connect 实例和资源前缀，然后检查或关联 EMAIL_MESSAGES 存储。
2. 在 `EMAIL_DOMAIN`（默认 `<实例别名>.email.connect.aws`）下创建 6 个 PoC 邮箱，均带前缀：`<p>_reservations`、`<p>_rewards`、`<p>_reservations.pen1`、`<p>_reservations.pen2`、`<p>_reservations.szx1`、`<p>_reservations.hgh1`。
3. 知识库：不在本脚本里构建。本地索引方式下把 `lambda/data/kb_index.json` 打包进 Lambda；已切换到 Bedrock Knowledge Base 时保留该配置。构建或切换请用 `./setup_kb.sh`。
4. 创建 DynamoDB 表、IAM 角色、triage 和 agent 两个 Lambda（`POC_WEEK=1`，`ENABLED_CAPABILITIES=KB`）。
5. 创建 General、Security 两个队列（坐席从 sl_reservations@ 回复），以及只含 Email 渠道的路由配置文件 `SL-Email-AI-Agents`。
6. 发布 flow `SL-Email-AI-Inbound`，并绑定上面的 6 个邮箱。
7. 自检（直接调用 agent Lambda，不发邮件）：
   - Focus 1 泳池问题由知识库回答；
   - 繁体中文来信用繁体回复；
   - 询价邮件因预订能力未启用而转人工（ESCALATE）。

## 本周人工验收

- [ ] 用外部邮箱给 sl_reservations@ 发一封新邮件，再回复 AI 的邮件。用 `aws connect describe-contact` 查看这封回复的 `RelatedContactId` / `ContactAssociationId`，确认 triage 判定为 REPLY 并归入原案例。
- [ ] Focus 1 泳池问题、Sample 6（Executive Club）按知识库正确回复。
- [ ] 从 no-reply@ 地址发信，以及主题为 `Automatic reply:` 的邮件：AI 不回复，联系直接结束。
- [ ] 同一封信同时发给 sl_reservations@ 和 sl_reservations.pen1@：只处理一次。
- [ ] 聊天离线留言邮件（来自 `CHAT_ORIGIN_SENDERS` 或含聊天记录特征）：进入人工队列，AI 不回复。
- [ ] 简体、繁体、英文来信分别用对应语言回复；发到不同邮箱时使用对应的签名。
- [ ] 坐席已分配到 `SL-Email-AI-Agents`（或设置了 `AGENT_USERNAMES`），能在 Agent Workspace 收到转人工的邮件。

## 本周相关变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `EMAIL_BUCKET_NAME` | 空 | 仅在实例未配置 EMAIL_MESSAGES 时使用 |
| `CHAT_ORIGIN_SENDERS` | 空 | 聊天离线留言系统的发件地址，逗号分隔 |

## 4 周通用

### 目录

```
index.html                           支持场景 / 动态执行流程图 / 定制难易度 / 4 周计划
deploy_week_1.sh … deploy_week_4.sh  按周部署（累积式，可重复执行）
deploy_lib.sh                        四个周脚本共用的函数（被 source，不单独执行）
README_week_1.md … README_week_4.md  每周说明
setup_kb.sh / README_kb.md           知识库配置（中文交互式）与说明
flow/sl_email_inbound_flow.json      Connect 入站邮件 flow（占位符在部署时替换）
lambda/triage.py                     flow 同步调用（8s）：读信、新邮件/回复识别、防护、Haiku 分类、启动 agent、poll
lambda/agent.py                      异步 Claude 工具调用循环、模板化组信、grounding 自检、报表行
lambda/tools.py                      知识库 + 模拟 PMS 预订 / 示例会员俱乐部 CRM（SOP 规则在代码里强制）
lambda/kb.py                         知识库检索（内置 BM25；可切 Bedrock Knowledge Base）
lambda/prompts.py                    SOP 提示词、Dcode、问候/结束语模板
lambda/data/                         kb_index.json（由 PDF 生成）、hotels.json（房价/库存/签名/邮箱映射）、seed_data.json
tools/build_kb_index.py              知识库文档 → kb_index.json（由 setup_kb.sh 调用）
reports/export_reports.py            导出 POC Report 11 项指标 CSV
```

### 运行时序

1. 邮件到达 Connect，flow 先按官方 “Handling email loops” 过滤自动发件人（mailer-daemon / postmaster / noreply / no-reply / bounces+）和退信主题。
2. flow 同步调用 triage：
   - 用 DescribeContact 取 Connect 线程字段判定 NEW / REPLY（RelatedContactId → 其上一跳 → ContactAssociationId 线程根，其次 `[Ref: EA-xxxx]` 案例号、回复前缀 + 同主题未结案例）；
   - 按 `connect:EmailMessageId` 去重；
   - 执行防护（SES 垃圾/病毒、自动回复/退信、聊天转邮件、钓鱼）；
   - 用 Claude Haiku 分类语言 / 意图 Dcode / 酒店 / 情绪，异步启动 agent。
3. flow 进入 Wait(10s) → poll 循环（最多 30 次）。
4. agent 用 Claude Sonnet + 工具完成查询 / 预订 / 改单 / 取消 / 会员验证，再做一次事实核查（grounding judge）。
5. `AUTO_SEND` 由 flow 的 Send message 从客人来信的同一邮箱回复；`HUMAN_REVIEW` / `ESCALATE` 按团队进入队列，AI 草稿、摘要、未答问题放在联系属性 `reply_body` / `ai_summary` / `open_items`。

### 按周启用的机制

- 未启用的场景：triage 识别到对应 Dcode 后设为 DRAFT_ONLY，agent 拿不到对应工具，回复 ESCALATE 进人工队列，不会自动发送。
- 每周脚本都会重新应用前几周的配置，可重复执行；还没创建的团队队列在 flow 里回落到 General 队列。
- 每个脚本最后跑自检（`SMOKE-*` 记录不计入报表），并打印本周需要人工验证的项目；`SKIP_SMOKE=true` 可跳过。

### 通用变量（完整列表见 `deploy_lib.sh` 开头）

| 变量 | 默认 | 说明 |
|---|---|---|
| `CONNECT_INSTANCE_ARN` | 交互选择 / `.deploy_settings` | 非交互运行时必填 |
| `NAME_PREFIX` | `sl` / `.deploy_settings` | 资源命名前缀，见“资源命名” |
| `ASSUME_YES` | false | 非交互运行时设为 true |
| `POC_NOW` | 空 | 固定“当前时间”，第 2、3 周的报价 / 下单测试需要 |
| `EMAIL_DOMAIN` | `<实例别名>.email.connect.aws` | |
| `EMAIL_LOCAL_PARTS` | 6 个邮箱基础名 | 实际地址自动加 `<p>_`；基础名要与 `lambda/data/hotels.json` 的 `mailboxes.rules` 一致 |
| `BIND_EMAIL_ADDRESSES` | true | false 时不改动邮箱的 flow 绑定 |
| `QUEUE_GENERAL` / `QUEUE_SECURITY` / `QUEUE_RESERVATIONS` / `QUEUE_LOYALTY` | `<P>-Email-*` | 覆盖派生的队列名 |
| `ROUTING_PROFILE_NAME` | `<P>-Email-AI-Agents` | 脚本创建，只含 Email 渠道 |
| `AGENT_USERNAMES` | 空 | 设置后会把这些坐席改用上面的路由配置文件（影响其原有路由） |
| `TRIAGE_MODEL_ID` / `AGENT_MODEL_ID` | Haiku 4.5 / Sonnet 4.6 | |
| `GROUNDING_GATE` | true | 回复中有无依据的陈述就转人工审核 |
| `BEDROCK_KB_ID` | 不设置 | 保留 setup_kb.sh 配置的知识库方式；填 KB ID 或 `none`（本地索引）可强制指定 |
| `SKIP_SMOKE` | false | 跳过自检 |
| `ALLOW_DOWNGRADE` | false | 允许在更高周之后执行更早周的脚本 |

### 重复执行（幂等）

四个脚本都可以重复执行，第二次执行不会新建或改动任何资源：

- **按名称查找后再创建**：存储、桶、邮箱、表、角色、Lambda、队列、路由配置文件、flow 都先查再建；同名资源有多个时直接报错退出，不会猜测或再建一个。
- **只在有差异时更新**：
  - Lambda 代码按内容哈希比较（打包已固定时间戳），配置和环境变量逐项比较；
  - flow 内容逐项比较；
  - 邮箱绑定、坐席的路由配置文件、Lambda 调用权限、队列关联都先检查当前值。
- **覆盖式写入**：IAM 策略、信任策略、异步重试设置每次写入同样内容，结果不变。
- **桶 CORS 只追加不覆盖**：只有确认“尚未配置 CORS”时才视为空；读取失败（如无权限）会报错退出，不会覆盖原有规则。
- **自检数据会清理**：自检写入的数据，包括第 3 周自检生成的测试订单，在脚本结束时（包括中途失败）全部删除。
- **防止降级**：已部署到第 N 周后，再执行更早周的脚本会报错退出，避免把能力降回去。确实要降级时设置 `ALLOW_DOWNGRADE=true`。

不处理的情况：
- 两个人同时执行同一实例的脚本；
- 已存在的队列被手工改了回复邮箱（脚本不会改回来）；
- 实例上已有同名但不是 PoC 创建的 flow（会被覆盖内容）。

### 更新知识库

执行 `./setup_kb.sh`（中文交互式），详见 `README_kb.md`。周脚本会保留它配置的知识库方式。

### 已知限制

- flow 原生 Send message 只接受 `text/plain`（`text/html` 被 CreateContactFlow 校验拒绝），所以自动回复是纯文本；HTML 版本存在报表行 `reply_html`。
- PMS / 示例会员俱乐部 CRM 未对接，`tools.py` 用 DynamoDB + POC 假数据模拟，预订返回结构按 Open API `reservation/detail` 设计。
- Dcode 列表是 PoC 自拟（`prompts.py` 的 `DCODES`），需替换为示例酒店正式 Dcode。
- 18 份 PDF（菜单、Spa、婚礼等）是纯图片，本地索引方式无法抽取文本，列在 `lambda/data/kb_gaps.json`；两种知识库方式都不收录（只处理脱敏后的文本），需 OCR 并脱敏后再加入（见 `README_kb.md`）。
- 坐席回复使用队列配置的发件邮箱（官方文档行为），转人工后从酒店邮箱来的邮件由 sl_reservations@ 回复，不是原酒店邮箱；如需按酒店回复，需为每个酒店建队列。
- flow JSON 已通过 Connect 服务端校验；周脚本的读操作已对真实实例执行、所有写命令的参数格式已校验，但完整运行和线上端到端收发邮件尚未在实例上跑过。
