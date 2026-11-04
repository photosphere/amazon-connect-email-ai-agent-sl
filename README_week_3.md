# 第 3 周：Area 2 写入类：下单、改单、取消（`deploy_week_3.sh`）

通用说明（目录、运行时序、通用变量、已知限制）见 `README_week_1.md`。

## 本周启用的场景

`ENABLED_CAPABILITIES=KB,RSV_READ,RSV_WRITE`（在第 2 周基础上新增下单、改期改房、取消）

| 场景 | 需求文档对应 | 定制难易度 |
|---|---|---|
| 预订：查房 + 下单 + 回顾（Check Available Book） | Area 2: Make Reservation；General Logic #1 | 中 |
| 改单 | Area 2（修改系统记录） | 中 |
| 取消订单（Cancel Booking FIT） | Area 2（修改系统记录） | 中 |

示例会员俱乐部（第 4 周）仍由 AI 起草、转人工。

## 前置条件

- 第 1、2 周已执行。
- `POC_NOW` 已设置为 2026 年 12 月之前（重跑时不设置会沿用 Lambda 上的值）；shell 和 Lambda 上都没有时，下单自检会被跳过。
- 确认取消政策、罚金规则、取消函模板与 POC Testing Data 一致：
  - Flexible Rate：到店前一日 18:00 后取消，收一晚房费；
  - Advance Purchase：不退款、不可修改。

## 执行

```bash
export CONNECT_INSTANCE_ARN='arn:aws:connect:us-west-2:<acct>:instance/<id>'
export POC_NOW='2026-11-15T10:00:00+08:00'
./deploy_week_3.sh
```

## 脚本做了什么

1. 更新两个 Lambda（`POC_WEEK=3`，`ENABLED_CAPABILITIES=KB,RSV_READ,RSV_WRITE`）。本周不新建 Connect 资源。
2. 重新确认队列、路由配置文件和 flow，并重新发布 flow。
3. 自检：
   - 下单：为客人 “Smoke Test”（smoke.test@example.com）真实创建一张订单，确认返回确认号；
   - 示例会员俱乐部问题：因会员能力未启用而转人工。

## 本周人工验收

- [ ] Sample 1，分两封邮件：
  - 第一封缺孩子年龄，AI 追问，并解答加床、提前入住、接机、儿童活动；
  - 第二封补齐年龄后，生成 3 张订单（每间一个 11 位确认号），回顾含税总价，并说明 48 小时内完成信用卡担保。
- [ ] 下单后同一客人再来信改单或取消：AI 能找到这张订单（General Logic #1）。
- [ ] Sample 5 例 1（订单 12312312312，从 megan.lim@example.com 发）：免费取消，返回取消号，说明取消函将另行发送。
- [ ] Sample 5 例 2（Fliggy 订单、无确认号）：说明 OTA 订单需在原平台处理，并请客人提供订单信息。
- [ ] 有罚金的取消：先说明罚金，请客人确认；客人要求减免罚金时转人工。
- [ ] 订单 11100000003（Advance Purchase，从 charlie.white@gmail.com 发）要求改期：拒绝改期，订单不变。
- [ ] 订单 11100000001（客人在住）要求取消：请客人联系酒店前台。

## 注意

- 下单和取消会真实写入 DynamoDB 里的模拟订单。比如 Sample 5 会把种子订单 12312312312 改为已取消。要重测，删除表中 `pk=RES#12312312312` 这一项，即恢复为种子数据。
- 要对接真实的 PMS 建单 / 改单 / 取消 API，替换 `lambda/tools.py` 中的 `_save_reservation` 和相关函数。这需要额外 1–2 周联调，不在本周计划内。
