# 技能证书互认申请

毕业生凭境外技能证书在多个培养项目申请课程减免时，同一张证书的学分权益
只能被使用一次。本项目在领域契约（`domain/contract.json`）的基础上提供
一套**零外部依赖**的完整后端：接收证书摘要与颁发机构验证、依据已发布映射
计算可申请权益、生成审核案件，并以原子事务保证批准动作写入权益、阻止跨项目
重复领取；敏感证据仅向本人与授权审核员开放，全部处理过程追加式可追溯。

## 角色与边界

| 角色 | 能力边界 |
| --- | --- |
| 毕业生 `graduate` | 为本人提交申请、补交材料、查看本人案件与权益账户 |
| 教务审核员 `reviewer` | 仅能处理被逐案授权的案件：要求补交、批准（全额/部分）、拒绝、窗口内撤销 |
| 认证机构 `issuer` | 仅能对本机构颁发且回执编号一致的证书提交核验结果 |
| 映射发布管理员 `registry_admin` | 登记成绩等级表、发布映射；**发布即冻结**，不可修改 |

## 核心约束（与契约不变量对应）

- **证书验证**：申请只接收证书摘要（`alg:hex`）、颁发机构代码与验证回执、
  标准代码/版本、成绩、有效期；过期或格式非法直接拒绝。机构核验通过前
  不得批准。
- **映射权益**：申请提交时按当时**已发布**映射（成绩等级表 + 成绩区间）
  计算 `mapped_credits` 并固化在案件上，后续映射改版不影响在途案件。
  映射区间不得重叠，同一标准版本只能发布一次。
- **防重复消费**：权益以**证书摘要**为全局唯一账户
  （`entitlements.certificate_digest UNIQUE`），跨项目共享一个余额。
  批准在单个 `BEGIN IMMEDIATE` 事务内完成「建档 → 条件扣减余额
  （`available_credits >= ?`）→ 写消费明细（`case_id UNIQUE`）→ 结案」，
  余额不足或并发抢占时整体回滚。同一项目存在未结案申请时唯一索引拒绝重复申请。
- **部分认可**：`partial` 学分必须为正、小于映射上限且不超过申请学分，
  剩余余额可在其他项目使用。
- **撤销决定**：仅已批准案件、仅授权审核员、决定后 30 天窗口内、
  不可重复撤销；撤销原子退回已核销学分（可供其他项目使用），
  原始消费与冲账事件均保留。
- **隐私隔离**：证书验证回执、核验证据引用等敏感字段只出现在本人与
  授权审核员的详情视图中；列表视图和未授权访问一律裁剪或 403。
  权益余额/跨项目使用明细仅证书本人可查。
- **完整追溯**：所有动作写入追加式 `audit_events`（数据库触发器禁止
  UPDATE/DELETE），案件历史合并权益账户事件，按时间排序返回。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/recognition/`：后端实现
  - `models.py`：身份、证书、案件、权益、消费明细等模型；
  - `store.py`：SQLite schema、行映射与事务；
  - `registry.py`：成绩等级表与已发布映射（冻结、区间去重）；
  - `service.py`：领域用例（申请/补交/核验/决定/撤销/查询/授权）；
  - `app.py`：Bearer 令牌鉴权的 HTTP API；
  - `errors.py`：稳定错误码与 HTTP 状态映射。
- `tools/check_contract.py`：契约摘要检查。
- `tools/serve.py`：启动 HTTP 服务（`--seed` 写入演示映射与令牌）。
- `tests/`：契约回归、领域用例（含并发双花测试）、HTTP 端到端测试。

## 运行

```bash
python3 tools/serve.py --db data/recognition.db --seed --port 8080
```

种子令牌：`tok-graduate`（毕业生）、`tok-graduate-2`、`tok-reviewer`
（普通审核员）、`tok-supervisor`（教务主管）、`tok-issuer`（认证机构）、
`tok-registry`（映射管理员）。

## HTTP 接口

除登录性质外均需 `Authorization: Bearer <token>`，请求/响应为 JSON。

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/admin/grade-scales` | 管理员 | 登记成绩等级表 `{standard_code, standard_version, ordered_grades}` |
| POST | `/admin/mappings/publish` | 管理员 | 发布区间映射（冻结）`{standard_code, standard_version, rules:[{grade_min,grade_max,credits}]}` |
| GET | `/mappings` | 任意 | 已发布映射 |
| POST | `/applications` | 毕业生 | 提交申请，响应含计算出的 `mapped_credits` |
| GET | `/applications` | 毕业生/审核员 | 案件列表（不含敏感字段；审核员仅见授权案件） |
| GET | `/applications/{id}` | 本人/授权审核员 | 案件详情（含补交材料、消费明细） |
| POST | `/applications/{id}/supplement-requests` | 审核员 | 要求补交，案件进入 `pending_supplement` |
| POST | `/applications/{id}/supplements` | 本人 | 仅 `pending_supplement` 可补交，回到 `under_review` |
| POST | `/applications/{id}/verification` | 认证机构 | `{passed, evidence_ref, issuer_verification_ref}`，机构与回执须匹配 |
| POST | `/applications/{id}/decisions` | 审核员 | `{mode: full\|partial\|reject, credits?, reason?}` |
| POST | `/applications/{id}/revocation` | 审核员 | `{reason}`，窗口内撤销并冲账 |
| GET | `/applications/{id}/history` | 本人/授权审核员 | 完整领域事件流 |
| GET | `/entitlements/{digest}` | 本人 | 权益总额、余额、状态与跨项目使用记录 |
| POST | `/reviewers/access-grants` | 教务主管 | 向审核员逐案授权 `{reviewer_id, case_id}` |

典型错误码：`validation_error(400)`、`unauthorized(401)`、
`permission_denied(403)`、`not_found(404)`、`duplicate_application(409)`、
`illegal_state(409)`、`credit_already_consumed(409)`、
`mapping_conflict(409)`、`no_published_mapping(422)`、
`invalid_certificate(400/422)`。

### 头条场景：一张证书两个项目

```bash
# 同一证书分别在项目 A、B 提交两条申请（跨项目允许）
# 机构核验通过、主管授权后：
#   批准 A -> 12 学分全部核销，entitlement.available_credits = 0
#   批准 B -> 409 credit_already_consumed，案件保持 under_review，无消费明细
# 撤销 A（窗口内）-> 退回 12 学分；此后 B 可批准
```

## 验证

```bash
python3 -m unittest discover -s tests -v       # 33 个测试
python3 -m compileall -q src tools tests       # 编译检查
python3 tools/check_contract.py domain/contract.json
```
