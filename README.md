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
- `examples/curl_examples.sh`：可复制的 curl 命令集（含排期段落）。

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
| GET  | `/editor/rooms` | 本地配置的会场清单（来自 `review_system/rooms.json`，可用 `REVIEW_ROOMS_CONFIG` 覆盖） |
| POST | `/editor/schedule/groups` | 提交一组安排（每份稿件一次）：指定未撤回的已发布决议/会场/UTC 起止 + 整组确认截止 |
| GET  | `/editor/schedule` | 已确认日程；`?history=true` 另含被替换/作废场次及结束原因 |
| GET  | `/editor/schedule/occupancy` | 当前占用：已确认场次 + 待确认组预留的新时段 |
| GET  | `/editor/schedule/groups`、`/editor/schedule/groups/{id}` | 每组进度（总数/已同意/已拒绝/待答复）、状态与结束原因 |

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
```

会场不入库、不提供管理接口，只读本地配置 `review_system/rooms.json`
（环境变量 `REVIEW_ROOMS_CONFIG` 可指向别的文件）；id/code/名称/别名均可识别。
所有排期时间在接口层统一为 UTC（不带时区的输入按 UTC 解释）。

## 7. 范围之外（按需求明确不做）

邮件通知、自动匹配算法、前端页面、用户自助注册与通用权限管理页面。
评语正文里评审人自行写入的身份信息不做识别脱敏（仅编号层面匿名）。
