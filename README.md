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

24 个测试全部通过，覆盖：角色边界与越权读取、改派后的评审归属、稿件修订隔离、
匿名编号字段、评审不可变、冲突全生命周期、发布撤回与重新发布、以及 20 轮
“冲突登记 vs 决议发布”并发交错不变量。

## 3. 角色调用样例

- `examples/walkthrough.py`：httpx 编写的完整端到端脚本（投稿 → 邀请 → 评审 →
  发布 → 冲突撤回 → 重新发布）。先 `./run.sh`，再：
  ```bash
  PYTHONPATH=/workspace/.pylibs/lib/python3.11/site-packages python3 examples/walkthrough.py
  ```
- `examples/curl_examples.sh`：可复制的 curl 命令集。

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
```

## 7. 范围之外（按需求明确不做）

邮件通知、自动匹配算法、前端页面、用户自助注册与通用权限管理页面。
评语正文里评审人自行写入的身份信息不做识别脱敏（仅编号层面匿名）。
