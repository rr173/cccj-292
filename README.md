# 匿名学术评审后端

一个基于 **Python + FastAPI + SQLite** 的后端服务，聚焦“编辑把稿件评审结果安全交给作者”这一条工作流。
不包含通用权限管理页面、邮件通知、自动匹配或前端；账号在本地预置，所有请求通过 `X-API-Key` 认证。

## 1. 启动

依赖已安装在仓库内的 `.pylibs/`（不污染系统 site-packages）：

```bash
./run.sh                       # 监听 http://127.0.0.1:8000，数据在 data/review.db
# 交互式文档： http://127.0.0.1:8000/docs
```

也可以自定义数据库位置：

```bash
REVIEW_DB_PATH=/tmp/review.db ./run.sh
```

会议差旅报销总额（整数分）同样通过环境变量固定，默认 1,000,000 分：

```bash
REVIEW_REIMBURSEMENT_BUDGET_CENTS=500000 ./run.sh
```

### 预置账号（本地准备好，不提供注册/管理接口）

| 角色     | 用户名      | API Key          |
|----------|-------------|------------------|
| 编辑     | editor1     | key-editor-1     |
| 作者     | author1     | key-author-1     |
| 作者     | author2     | key-author-2     |
| 评审人   | reviewer1   | key-reviewer-1   |
| 评审人   | reviewer2   | key-reviewer-2   |
| 评审人   | reviewer3   | key-reviewer-3   |

## 2. 测试

```bash
PYTHONPATH=/workspace/.pylibs/lib/python3.11/site-packages:/workspace \
  python3 -m pytest -q tests
```

全部测试通过，覆盖：角色边界与越权读取、改派后的评审归属、稿件修订隔离、
匿名编号字段、评审不可变、冲突全生命周期、发布撤回与重新发布、20 轮
“冲突登记 vs 决议发布”并发交错不变量，以及会议报告排期的完整规则：

- 编辑为入选稿件安排报告、作者分别确认、整组一次性生效；
- 两份报告交换时段的联动改期（新时段允许与本组将替换的旧时段重叠）；
- 截止时间早于所有相关新/旧场次开始、会场与作者档期重叠判定（首尾相接不冲突）；
- 预留时段对其他组的占用拦截、一份稿件至多一个有效场次/一组待确认安排；
- 拒绝、截止过期、重复/迟到答复不可复活、冲突撤回联动作废与整组取消；
- “最后一次同意 vs 撤回”并发竞争不变量（`tests/test_schedule_race.py`）。

报告人差旅报销（`tests/test_reimbursement.py`，全部金额为整数分）：

- 额度需已确认场次、受益人即稿件作者、同稿同决议仅一笔；确认改期沿用原决议不影响；
- 凭证号唯一与票据归属、同一票据可拆给该作者的多笔额度；
- 额度/票据/会议总额三处独立上限，分别约束“未交财务 + 已交未明 + 已付”之和；
- 分批交付与整批原子占定、请求号 + 内容幂等重放、同号异内容拒绝；
- 分次回执、零额/部分付款、差额释放不回流、相同回执幂等、异额不可覆盖；
- 撤回决议仅释放未交部分，已交付仍可核销、未明额继续占款、已付仍计支出；
- 审批 / 交付 / 撤回 / 核销并发下不超限、不留错分类（`test_concurrent_*`）。

听众席位预约（`tests/test_audience.py`）：

- 会场听众容量默认零、设置后按报名先后递补；重复报名不改顺序、取消再报排到最后；
- 重叠报告可候补但不可同时持席位（首尾相接不算重叠），空位递补可跳过撞期者，
  同一听众在多场的递补机会也按报名先后裁决；
- 已确认席位不被后来合格的更早候补挤掉；确认改期（含同组多变更、换会场）与
  容量变动后按原报名先后保留名单，其余转回候补且保留原顺序；
- 撤回决议终止未开场报告的报名且不随新决议转移；开场后名单冻结，不再接受/
  取消/递补，容量与决议变化不改写，冻结席位仍按原时段参与撞期判断；
- 并发报名不超容量、取消+报名/容量变更/决议撤回竞争下名单一致（`test_concurrent_*`）。

## 3. 角色调用样例

- `examples/walkthrough.py`：httpx 编写的完整端到端脚本（投稿 → 邀请 → 评审 →
  发布 → 冲突撤回 → 重新发布）。先 `./run.sh`，再：
  ```bash
  PYTHONPATH=/workspace/.pylibs/lib/python3.11/site-packages python3 examples/walkthrough.py
  ```
- `examples/schedule_walkthrough.py`：排期端到端脚本（两份入选稿件先各自确认
  场次 → 编辑发起交换前后时段的联动改期 → 两位作者分别确认 → 再次提议被拒绝，
  编辑查询进度与结束原因）。可用独立库启动：
  ```bash
  REVIEW_DB_PATH=/tmp/schedule-demo.db ./run.sh --port 8010
  PYTHONPATH=/workspace/.pylibs/lib/python3.11/site-packages \
    python3 examples/schedule_walkthrough.py http://127.0.0.1:8010
  ```
- `examples/curl_examples.sh`：可复制的 curl 命令集（含排期与报销段落）。
- `examples/reimbursement_walkthrough.py`：报销端到端脚本（额度 → 票据 → 审批 →
  交付批次与幂等重放 → 分次财务回执 → 撤回决议后释放/核销 → 作者自查）。
  ```bash
  REVIEW_DB_PATH=/tmp/reimb-demo.db ./run.sh --port 8011
  PYTHONPATH=/workspace/.pylibs/lib/python3.11/site-packages \
    python3 examples/reimbursement_walkthrough.py http://127.0.0.1:8011
  ```
- `examples/audience_walkthrough.py`：听众席位端到端脚本（设容量 → 报名/候补 →
  重叠撞期跳过 → 取消递补 → 确认改期保留名单并联动递补 → 容量收缩降级 →
  编辑查询入场名单）。
  ```bash
  REVIEW_DB_PATH=/tmp/audience-demo.db ./run.sh --port 8012
  PYTHONPATH=/workspace/.pylibs/lib/python3.11/site-packages \
    python3 examples/audience_walkthrough.py http://127.0.0.1:8012
  ```

## 4. HTTP 接口一览

所有请求头携带 `X-API-Key: <key>`；错误统一为 `{"error": {"code": ..., "detail": ...}}`。

### 作者 `/author`
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/author/manuscripts` | 投稿，自动产生修订 1 |
| POST | `/author/manuscripts/{id}/revisions` | 追加新修订（旧修订只追加、永不更新） |
| GET  | `/author/manuscripts` | 自己的稿件列表 |
| GET  | `/author/manuscripts/{id}` | 自己的稿件与全部修订正文（不含任何评审信息） |
| GET  | `/author/decisions` | 收到的决议；撤回版本只返回撤回元数据 |
| GET  | `/author/decisions/{id}` | 固定发布版本；撤回后返回 **410**，正文不可再取 |
| GET  | `/author/schedule`、`/author/schedule/grouped` | 自己的报告场次与待确认/历史安排（含截止时间、本人答复、可否答复、结束原因） |
| POST | `/author/schedule/items/{item_id}/respond` | 对自己稿件的那一条安排答复 `{"accept": true/false}`；他人条目返回 404 |
| GET  | `/author/reimbursements` | 自己的报销总额、各笔额度/审批/付款进度（他人数据不可见） |
| GET  | `/author/reimbursements/allowances/{id}` | 自己某笔额度的审批明细；他人额度 404 |

（另提供兼容别名：`/author/schedules`、`/author/arrangements`、`/author/talks`，
以及 `/author/schedule-items/{id}/respond|confirm`、`/author/arrangements/{id}/respond|confirm`。）

### 评审人 `/reviewer`
| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/reviewer/invitations` | 仅自己被邀请的稿件与自己提交的评审 |
| POST | `/reviewer/invitations/{id}/respond` | `{"accept": true/false}`，仅接受后可评审 |
| POST | `/reviewer/invitations/{id}/reviews` | 提交评语 + `score`（**StrictInt 1..5**），一次性不可替换 |

没有“按 id 查评审”的接口：无法借稿件 ID / 评审 ID 读到他人的未发布评审；
访问不属于自己的邀请 ID 返回 404（不泄漏存在性）。

### 编辑 `/editor`
| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/editor/accounts` | 预置账号目录（不含 API key） |
| GET  | `/editor/manuscripts`、`/editor/manuscripts/{id}` | 全部稿件、真实评审人、评审与决议 |
| POST | `/editor/invitations` | 邀请明确绑定 `revision_id`；有冲突时 409 |
| GET/POST | `/editor/conflicts` | 查看/登记利益冲突 |
| POST | `/editor/decisions` | 选修订 + 至少 2 份该修订的当前无冲突评审 + 结论 |
| GET  | `/editor/decisions`、`/editor/decisions/{id}` | 含撤回版本的原始冻结内容与撤回原因 |
| GET  | `/editor/rooms` | 本地配置的会场清单（来自 `review_system/rooms.json`，可用 `REVIEW_ROOMS_CONFIG` 覆盖），含各会场听众容量 |
| PUT  | `/editor/rooms/{room}/audience-capacity` | 设置会场听众容量（默认 0，id/code/名称/别名均可寻址）；同事务内重算受影响名单 |
| GET  | `/editor/talks/{manuscript_id}/admissions` | 该报告的当前入场名单（席位 + 候补，按报名先后；已开场则冻结名单） |
| GET  | `/editor/sessions/{session_id}/admissions` | 按场次查其决议代的入场名单 |
| POST | `/editor/schedule/groups` | 提交一组安排（每份稿件一次）：指定未撤回的已发布决议/会场/UTC 起止 + 整组确认截止 |
| GET  | `/editor/schedule` | 已确认日程；`?history=true` 另含被替换/作废场次及结束原因 |
| GET  | `/editor/schedule/occupancy` | 当前占用：已确认场次 + 待确认组预留的新时段 |
| GET  | `/editor/schedule/groups`、`/editor/schedule/groups/{id}` | 每组进度（总数/已同意/已拒绝/待答复）、状态与结束原因 |
| POST | `/editor/reimbursements/allowances` | 给已确认报告、决议未撤回的稿件设一笔报销额度（受益人=稿件作者） |
| GET  | `/editor/reimbursements/allowances[/{id}]` | 额度及其未交/已交未明/已付/释放金额 |
| POST | `/editor/reimbursements/receipts` | 登记票据（唯一凭证号、归属作者、票面额） |
| GET  | `/editor/reimbursements/receipts[/{id}]` | 票据及其三桶金额与已审批合计 |
| POST | `/editor/reimbursements/claims` | 按「额度、同一作者票据、金额」审批费用 |
| GET  | `/editor/reimbursements/claims[/{id}]` | 审批明细：未交、已交未明、已付、释放 |
| POST | `/editor/reimbursements/batches` | 把多笔审批的未交款项全部/部分交付财务（幂等、整批原子） |
| GET  | `/editor/reimbursements/batches[/{id}]` | 批次付款清单、逐笔付款编号与核销进度 |
| POST | `/editor/reimbursements/batches/{id}/results` | 财务分次回执，逐付款编号记录实际支付额 |
| GET  | `/editor/reimbursements/summary` | 会议总额及每笔额度/票据的占款与支出总览 |

`POST /editor/schedule/groups` 的请求体：

```json
{
  "confirmation_deadline": "2030-05-01T00:00:00Z",
  "items": [
    {"manuscript_id": 1, "decision_id": 1, "room": "hall-a",
     "starts_at": "2030-06-03T10:00:00Z", "ends_at": "2030-06-03T11:00:00Z"},
    {"manuscript_id": 2, "room": "hall-a",
     "starts_at": "2030-06-03T11:00:00Z", "ends_at": "2030-06-03T12:00:00Z"}
  ]
}
```

已有已确认安排的稿件可省略 `decision_id`：服务端强制沿用原决议，改传其他决议
会得到 `409 decision_locked`。时间接受带时区 ISO-8601（统一换算 UTC），不带
时区的字符串按 UTC 解释。另提供别名 `POST /editor/schedule`（也接受单条裸安排）、
`POST /editor/schedules`、`POST /editor/arrangements`、`GET /editor/schedules`。

组状态：`pending / confirmed / declined / expired / cancelled`；结束原因：
`all_accepted / author_declined / deadline_expired / decision_withdrawn`。
场次状态：`confirmed / superseded / voided`。

### 听众 `/audience`

所有预置账号（作者、评审人、编辑）都可作为听众；接口只返回本人状态与
各场计数，**绝不暴露其他听众的报名名单或任何评审资料**。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/audience/talks` | 已确认报告列表：会场/时间、听众容量、已占席位、剩余席位、候补数、是否开放报名、我的报名状态 |
| POST | `/audience/talks/{manuscript_id}/register` | 报名（201）；已持有效申请时原样返回（200，顺序不变）；开场后 409 `session_started`，未开放 409 `not_open` |
| POST | `/audience/talks/{manuscript_id}/cancel` | 取消自己的有效报名（无有效报名 404；开场后 409） |
| GET  | `/audience/registrations` | 自己的全部报名：席位/候补（含候补位次）/已取消/已终止及原因 |

（另提供别名：`POST /audience/talks/{id}/registration`、`DELETE /audience/talks/{id}/registration`。）

### 报告人差旅报销 `/editor/reimbursements`、`/author/reimbursements`

**所有金额为整数分（cents）**，不传图片、不接银行，只交付付款所需信息。
会议有一个固定报销总额 `REVIEW_REIMBURSEMENT_BUDGET_CENTS`（默认 1,000,000 分，
与会场配置一样属于部署配置，无管理接口）。

请求体示例：

```json
POST /editor/reimbursements/allowances {"manuscript_id": 1, "amount_cents": 120000}
POST /editor/reimbursements/receipts   {"voucher_no": "VOC-001", "author_id": 2, "amount_cents": 80000}
POST /editor/reimbursements/claims     {"allowance_id": 1, "receipt_id": 1, "amount_cents": 60000}
POST /editor/reimbursements/batches    {"idempotency_key": "batch-0001",
                                        "items": [{"claim_id": 1, "amount_cents": 60000}]}
POST /editor/reimbursements/batches/1/results
     {"results": [{"payment_no": "PMT-000001-0001", "paid_cents": 55000, "note": "fee"}]}
```

关键语义：

- **额度**：仅对“已有已确认场次 + 该场次决议未撤回”的稿件可设，受益人为稿件作者；
  `UNIQUE(manuscript_id, decision_id)`。已确认改期只换时间、沿用原决议，额度不变；
  改传其他决议 → `409 decision_mismatch`。
- **审批三桶上限**：就会议、每笔额度、每张票据分别计算
  “未交财务额 + 已交财务未明额 + 实际已付额”，各自不得超过本身上限；
  违反分别返回 `budget_exceeded / allowance_exceeded / receipt_exceeded`。
- **票据**：凭证号全局唯一；审批时票据归属作者必须等于额度受益人
  （`receipt_owner_mismatch`）；同一票据可拆给该作者的多笔额度。
- **批次原子性**：请求内同一审批不得重复；任一明细超过该审批未交余额即
  `409 claim_amount_exceeded`，整批不落库。一份审批可跨多个批次分批交付。
- **幂等**：`idempotency_key` + 规范化内容指纹。同号同内容重放 → 返回原付款清单与
  逐笔付款编号（即使决议已撤回或财务已回执）；同号异内容 → `409 idempotency_conflict`。
- **财务回执**：每次可只报部分付款编号；`paid_cents` 必须 ≤ 指令金额
  （`paid_exceeds_instruction`）。每笔付款编号只会有一个最终结果：重复相同金额
  视为 `unchanged` 无副作用；不同金额 → `409 result_conflict`。未回执的付款项继续
  全额占款；已回执的差额 `released_cents` 释放且**不会**自动回到未交款项。
- **撤回决议**：`POST /editor/conflicts` 的响应额外带
  `released_allowance_ids`、`released_cents`。额度置 `released`：不再接受新审批，
  未交部分释放；已交付指令仍可核销，未明额不提前释放，已付金额仍计实际支出；
  新决议不会恢复旧额度（必须重新建额度）。
- **可见性**：编辑可查 `summary`（会议总额 + 每笔额度/票据占款支出）与批次进度；
  作者仅能查自己的 `GET /author/reimbursements`，按 id 查他人额度返回 404。

## 5. 关键规则与实现位置

| 需求 | 实现 |
|---|---|
| 修订只追加，旧版不可覆盖 | `revisions` 表无 UPDATE 路径；`UNIQUE(manuscript_id, revision_no)`，见 `services.add_revision` |
| 邀请针对具体修订 | `invitations.revision_id`，`UNIQUE(revision_id, reviewer_id)` |
| 接受后才能提交，提交后不可替换 | `respond_invitation` / `submit_review` 的状态机 + `reviews.invitation_id UNIQUE` |
| 改派不把旧评审记到新评审人名下 | 评审行永久绑定原 `invitation_id`/`reviewer_id`；新邀请是新行，测试 `test_reassignment_keeps_old_review_with_original_reviewer` |
| 作者在发布前看不到评审 | 作者路由不返回任何邀请/评审字段，也没有按 id 取评审的入口 |
| 冲突存在不得邀请；邀请后登记冲突 | `register_conflict` 把未产出评审的邀请置 `blocked`，接受/提交处重新校验冲突 |
| 已提交评审保留编辑可见但不能进决议 | 评审行不删除；`publish_decision` 对每份评审做当前冲突校验（`conflict_blocks_review`） |
| 新修订不继承旧评审、禁止混修订发布 | 评审绑定 `revision_id`；发布时逐条校验，跨修订返回 `revision_mismatch` |
| 发布至少 2 份同修订且无冲突的评审 | Pydantic `min_length=2` + 去重校验 + 事务内冲突复查 |
| 作者只看到稿件内稳定匿名编号 | `anonymous_numbers` 按稿件分配（每稿独立从 1 开始，同一评审人跨稿编号不同）；发布快照只存 `anon_number/content/score`，不含账号、邀请 ID、评审 ID |
| 发布后入选评审涉及新冲突 → 撤回 | `register_conflict` 在同一事务内把相关有效决议置 `withdrawn`；作者旧链接 410，编辑仍可查冻结原文和撤回原因 |
| 重新发布必须重选合格评审 | 冲突评审永久被拒；每个修订至多一个有效决议（部分唯一索引 `ux_active_decision`），撤回后才能再发 |
| 冲突与发布竞争不出错序状态 | 所有写操作取进程锁并 `BEGIN IMMEDIATE`，读-校验-写原子串行；并发测试 20 轮验证二选一不变量 |
| 编辑自行挑选入选稿件，不解析结论 | 排期只校验决议存在、属于该稿且 `published`，从不读取/匹配 `conclusion` 文字 |
| 一组安排每份稿件仅一次 | 请求内去重 → `400 duplicate_manuscript`；待确认组参与资格查 `schedule_groups.status='pending'` |
| 已有安排沿用原决议，只改会场/时间 | 存在 `confirmed` 场次时以其 `decision_id` 为准，异值 → `409 decision_locked`，省略则继承 |
| 截止早于所有新、旧场次开始 | `deadline < min(所有新开始, 本组稿件当前 confirmed 场次开始)`，严格小于，相等也拒绝 |
| 会场/作者档期不可重叠，首尾相接允许 | 半开区间 `start_a < end_b and start_b < end_a`；同会场或同作者任一命中即 `409 schedule_conflict` |
| 发出即预留、旧安排仍有效 | 占用集合 = `sessions(status=confirmed)` ∪ 待确认组的 `schedule_items`；其他组撞预留/已确认即拒绝 |
| 本组新时段可与将替换的旧时段重叠 | 冲突检查排除“本组稿件”的全部 confirmed 场次，因此支持两份报告交叉交换前后时段 |
| 一稿至多一个有效场次 | 部分唯一索引 `ux_confirmed_session ... WHERE status='confirmed'`；生效时旧场次置 `superseded` |
| 一稿至多参与一组待确认安排 | 创建时联表查 pending 组 → `409 manuscript_pending`；终态组不再占用该资格 |
| 作者只看/答复自己的条目 | 条目经 `manuscripts.author_id` 鉴权，非本人统一 `404 not_found`，列表只含本人稿件 |
| 全员截止前同意才一次性生效 | 最后一次 accepted 在同一事务内作废旧场次、插入新场次、组置 `confirmed`；否则任何改动都不落地 |
| 拒绝/过期整组失败并释放预留 | 拒绝立即置 `declined/author_declined`；惰性扫描把超时组置 `expired/deadline_expired`；预留源自 pending 状态，终态即释放，旧场次不动 |
| 结束的安排不能被迟到/重复答复复活 | 答复先扫过期再判组状态：非 pending → `confirmation_expired/group_declined/group_cancelled/group_confirmed`，已答复 → `already_responded`，均 409 |
| 撤回决议联动排期 | `register_conflict` 同事务调用 `scheduling.decisions_withdrawn_hook`：基于该决议的 confirmed 场次置 `voided/decision_withdrawn`，涉事 pending 组整组 `cancelled`；其他稿件既有场次不受影响 |
| 同意与撤回竞争无残留 | 同一把写锁串行：同意先生效则撤回随后 void 新场次；撤回先生效则组 cancelled、答复 409；`tests/test_schedule_race.py` 多轮验证 |
| 新决议不复活旧场次/旧同意 | 撤回只写终态，重新发布产生新 `decisions` 行，与旧 `sessions/items` 无任何联动；必须由编辑重新发起安排 |

### 差旅报销关键规则与实现位置

| 需求 | 实现 |
|---|---|
| 额度绑定已确认场次且决议未撤回 | `reimbursement.create_allowance`：查 `sessions(status=confirmed)` 并以其 `decision_id` 为准 |
| 同稿同决议仅一笔额度、改期不影响 | `UNIQUE(manuscript_id, decision_id)`；改期沿用原决议故不产生新行 |
| 审批须同作者票据 | `create_claim` 比对 `receipt.author_id` 与 `allowance.beneficiary_id` |
| 三处独立上限 | `_assert_caps` 用同一 `_exposure` 就 allowance/receipt/会议预算分别校验 |
| 分批交付 + 整批原子占定 | `create_batch` 在一个 `BEGIN IMMEDIATE` 内校验全部明细后再插入指令 |
| 请求号幂等、同号异内容拒绝 | `payment_batches.idempotency_key UNIQUE` + 规范化 `request_hash` 比对 |
| 只交付款所需信息、不调付款服务 | `batch_payload` 仅含付款编号/收款人/凭证号/金额 |
| 每笔付款编号唯一终态 | `payment_results.instruction_id UNIQUE` + 已存在同额幂等、异额 409 |
| 未明额继续占款、差额释放不回流 | `_exposure` 把无 result 的指令全额计入；有 result 仅计 `paid_cents` |
| 撤回仅释放未交部分 | `reimbursement.decisions_withdrawn_hook` 在冲突事务内计算未交付余额并置 `released_at` |
| 已付仍计支出、已交付可核销 | 撤回只改 `reimbursement_allowances`，从不改 `payment_instructions/results` |
| 并发安全 | 复用全局写锁 + `BEGIN IMMEDIATE`；`test_concurrent_*` 验证不超限、无错分类 |

### 听众席位关键规则与实现位置

| 需求 | 实现 |
|---|---|
| 预置账号即听众 | `/audience` 路由只要求登录（`current_user`），不限角色 |
| 会场听众容量默认零、每场报告用所在会场容量 | `room_audience_capacities` 表按规范会场 code 存容量，缺省 0；评估时按场次当前会场解析 |
| 只有已确认且决议有效的报告开放报名 | `audience.register` 校验 confirmed 场次 + `decisions.status='published'` + 未开场 |
| 待确认的排期变更不影响报名 | 只有 `_finalize_group` 触发重算；pending 组从不触碰报名 |
| 同一报告至多一份有效申请；重复报名不改顺序 | 部分唯一索引 `ux_active_registration(decision_id, audience_id)`；命中已有申请直接原样返回 |
| 取消后再报名排在后面 | 取消置 `cancelled`，重新报名插入新行，获得更大的 id（即更晚的队列位置） |
| 重叠可候补、不可同时持席位；首尾相接不算重叠 | 半开区间重叠判定；`_promote_pass` 跳过与本人已持席位撞期的候补 |
| 已确认席位不被后来合格的候补挤掉 | 递补只填空位，从不降级；降级只发生在改期/容量变动后的 `_reevaluate` |
| 改期/容量变动后按原报名先后保留、其余转回候补且保留原顺序 | `_reevaluate` 以报名 id 顺序贪心保留（容量内且不撞期），其余置回 `waitlisted` 且不改 `registered_at`/id |
| 空位按报名先后递补、可跳过撞期者、多场同时递补同序 | `_promote_pass` 全局按报名 id 升序单趟扫描，确认能入座者 |
| 取消/报告取消/改期释放的机会算到其他受影响报告 | 每个变更入口都在同事务末尾跑全局 `_promote_pass` |
| 沿用原决议的确认改期保留报名 | 报名挂在（稿件， 决议代）上；改期只换场次行，报名自动跟随新场次 |
| 撤回决议终止未开场报告的报名、不随新决议转移 | `audience.decisions_withdrawn_hook` 在冲突事务内终止（`ended/decision_withdrawn`）；新决议产生新决议代，旧报名永不复活 |
| 开场即冻结名单 | `starts_at <= now` 的场次：报名/取消/递补/降级全部拒绝或跳过；冻结后容量与决议变化不改写 |
| 已开场席位仍按原时段参与撞期判断 | `_build_held_state` 把已开场场次的席位计入持位区间（即使之后被 void） |
| 名单与排期原子一致、无中间态 | 所有写路径复用全局写锁 + `BEGIN IMMEDIATE`；改期生效与决议撤回在同一事务内完成报名重算 |
| 不对其他听众公开名单/评审资料 | `/audience` 只返回计数与本人状态；入场名单仅 `/editor` 可见 |

### 并发不变量（`tests/test_publish_conflict_race.py`）

冲突登记与决议发布并发时，只允许两种结局：

1. **冲突先提交** → 发布收到 409，不存在有效决议；
2. **发布先提交** → 冲突随后把该决议撤回。

绝不可能出现“冲突已生效而含该评审的决议仍是有效版本”。

## 6. 数据模型摘要

```
users(role, api_key)
manuscripts(author_id)
revisions(manuscript_id, revision_no, content)          -- 只追加
anonymous_numbers(manuscript_id, reviewer_id, anon_number) -- 每稿独立稳定编号
invitations(revision_id, reviewer_id, status)           -- invited/accepted/reviewed/declined/blocked
reviews(invitation_id UNIQUE, revision_id, reviewer_id, content, score 1..5)
conflicts(author_id, reviewer_id, reason)
decisions(revision_id, status, conclusion, withdrawal_reason)
decision_reviews(decision_id, review_id, anon_number, content, score)  -- 发布即冻结
schedule_groups(confirmation_deadline, status[pending/confirmed/declined/expired/cancelled], end_reason, ended_at)
schedule_items(group_id, manuscript_id, decision_id, room, starts_at, ends_at, response[waiting/accepted/declined])
sessions(manuscript_id, decision_id, group_id, room, starts_at, ends_at, status[confirmed/superseded/voided], end_reason)
-- ux_confirmed_session: UNIQUE(manuscript_id) WHERE status='confirmed'
reimbursement_allowances(manuscript_id, decision_id, beneficiary_id, amount_cents, released_at, release_reason)
-- UNIQUE(manuscript_id, decision_id)
reimbursement_receipts(voucher_no UNIQUE, author_id, amount_cents)
reimbursement_claims(allowance_id, receipt_id, amount_cents)
payment_batches(idempotency_key UNIQUE, request_hash)
payment_instructions(batch_id, claim_id, allowance_id, amount_cents, payee_id, voucher_no, payment_no UNIQUE)
payment_results(instruction_id UNIQUE, paid_cents, released_cents, note)
room_audience_capacities(room_code PRIMARY KEY, capacity >= 0)   -- 缺省即 0
audience_registrations(manuscript_id, decision_id, audience_id, status, registered_at, seated_at, ...)
-- ux_active_registration: UNIQUE(decision_id, audience_id) WHERE status IN ('waitlisted','seated')
-- 报名 id 即全局报名先后顺序；状态：waitlisted/seated/cancelled/ended
```

会场不入库、不提供管理接口，只读本地配置 `review_system/rooms.json`
（环境变量 `REVIEW_ROOMS_CONFIG` 可指向别的文件）；id/code/名称/别名均可识别。
所有排期时间在接口层统一为 UTC（不带时区的输入按 UTC 解释）。

## 7. 范围之外（按需求明确不做）

邮件通知、自动匹配算法、前端页面、用户自助注册与通用权限管理页面。
评语正文里评审人自行写入的身份信息不做识别脱敏（仅编号层面匿名）。
报销不接银行、不调用任何付款服务，也不接收/存储票据图片；只按凭证号登记票面额，
并把付款所需信息（收款作者、凭证号、金额、付款编号）交给会务财务。
听众席位不做签到、收费或通知；开场名单冻结后不再变化。
