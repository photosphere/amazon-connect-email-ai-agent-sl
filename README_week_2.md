# 第 2 周：Area 2 只读类 + 升级人工 + 附件（`deploy_week_2.sh`）

通用说明（目录、运行时序、通用变量、已知限制）见 `README_week_1.md`。

## 本周启用的场景

`ENABLED_CAPABILITIES=KB,RSV_READ`（在第 1 周基础上新增订单查询、报价、订单备注）

| 场景 | 需求文档对应 | 定制难易度 |
|---|---|---|
| 询价报价 / 暂不预订（No Book Pending Decision） | Area 2: Make Reservation | 中 |
| 订单确认与特殊要求（加床、早餐、提前入住、航班备注） | Area 2（实时查询客人订单） | 中 |
| 多问题邮件：能答先答，其余升级人工 | 功能评估准则；General Logic #2 | 低 |
| 投诉 / 情绪 / 要求人工 | 功能评估准则（无缝转人工） | 低 |
| 附件读取（PDF / 图片） | 非 Focus（Req 20、Req 28） | 低-中 |

下单、改期改房、取消（第 3 周）和示例会员俱乐部（第 4 周）仍由 AI 起草、转人工。本周可以给订单加备注和特殊要求，但不能改日期或房型。

## 前置条件

- 第 1 周已执行（脚本检测到未执行会给出提醒，并补做基础配置）。
- 首次部署时 `POC_NOW` 已设置为 2026 年 12 月之前，否则 2026 年 12 月至 2027 年 4 月的报价会被当作过去日期。重跑时不设置会沿用 Lambda 上的值；设为空（`POC_NOW=`）会删除它。
- 使用的数据已打包在 Lambda 里：
  - `lambda/data/hotels.json`：POC Testing Data 第 7–10 行的库存与房价；
  - `lambda/data/seed_data.json`：种子订单 12345678910、12345678909 等。

## 执行

```bash
export CONNECT_INSTANCE_ARN='arn:aws:connect:us-west-2:<acct>:instance/<id>'
export POC_NOW='2026-11-15T10:00:00+08:00'
./deploy_week_2.sh
```

## 脚本做了什么

1. 关联附件存储（ATTACHMENTS）。默认用邮件正文存储的同一个桶，可用 `ATTACHMENT_BUCKET_NAME` 指定其他桶。
2. 给该桶追加一条 CORS 规则（只追加，不覆盖已有规则），允许 `https://<实例别名>.my.connect.aws`，让坐席能在工作台打开附件。
3. 更新两个 Lambda（`POC_WEEK=2`，`ENABLED_CAPABILITIES=KB,RSV_READ`）。
4. 新建 Reservations 队列（坐席从 sl_reservations@ 回复），并加入路由配置文件 `SL-Email-AI-Agents`（前缀 sl 时）。
5. 重新发布 flow，使预订团队的分支指向新队列。
6. 自检（只读，不改任何订单）：
   - Lakeview 2 大 2 小询价：调用查房，并追问孩子年龄；
   - 下单请求：因写订单能力未启用而转人工。

## 本周人工验收

- [ ] Focus 2 示例：Lakeview 房 2 大 2 小，回复说明最大入住人数（2 大 1 小或 1 大 2 小），建议订 2 间并询问孩子年龄；补充年龄后给出含税报价，并说明尚未下单。
- [ ] Sample 2（订单 12345678910，从 test.kim@example.com 发）：加床备注写入订单；说明早餐只含 3 位成人，并按儿童餐政策询问孩子年龄。
- [ ] Sample 3（订单 12345678909，从 david.peeters@example.com 发）：航班和提前入住备注写入订单，并说明提前入住的分时段收费。
- [ ] 从其他邮箱查询同一订单：AI 不透露订单内容，要求提供登记的手机或邮箱。
- [ ] Sample 4（2027 年日期）：超出库存数据范围，转人工（HUMAN_REVIEW）进入 Reservations 队列。
- [ ] 一封邮件里有能答和不能答的问题：已答部分写进草稿，`open_items` 列出未答项，进入人工队列。
- [ ] 投诉邮件、要求人工的邮件：AI 只起草，进入人工队列。
- [ ] 带 PDF 或图片附件的邮件：AI 能读到附件内容；坐席能在工作台打开附件。

## 本周相关变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ATTACHMENT_BUCKET_NAME` | 邮件正文存储的桶 | 仅在实例未配置 ATTACHMENTS 时使用 |
| `QUEUE_RESERVATIONS` | `SL-Email-Reservations` | |

## 注意

- 加备注会真实写入 DynamoDB 里的模拟订单，重复测试同一订单时备注会累积。
- 要对接真实的 PMS / Open API 订单查询，替换 `lambda/tools.py` 中的 `_load_reservation`。
