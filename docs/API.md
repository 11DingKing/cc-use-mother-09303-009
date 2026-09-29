# HTTP 接口契约

基址 `http://127.0.0.1:8080`，全部请求/响应为 JSON（UTF-8）。
鉴权：`Authorization: Bearer <api-key>`。

错误响应统一为：

```json
{ "error": "duplicate_entitlement", "message": "……", "details": { } }
```

## 案件状态机

```
SUBMITTED ──核验通过──► VERIFIED ──开始评估──► UNDER_EVALUATION
   ▲                       │                         │
   │（PENDING/FAILED）     │（机构撤回确认）          ├─ 全部认可 ─► APPROVED
   └───────────────────────┘                         ├─ 部分认可 ─► PARTIALLY_APPROVED ─┐
                                                     └─ 全部驳回 ─► REJECTED             │
SUPPLEMENTING ◄── 审核员发起补交（1–30天截止）◄── SUBMITTED/VERIFIED/UNDER_EVALUATION     │
                                                                                          ▼
                              APPROVED / PARTIALLY_APPROVED ──记账──► POSTED
                                       │
                                       └──撤销（含已记账）──► REVOKED（权益墓碑化，释放占用）
```

## 接口

### 身份
- `GET /me` — 当前身份。

### 毕业生
- `POST /eligibility/calculate` — 依据已发布映射试算可申请权益（不落库），返回 `eligible`、`ineligible_below_min_score`、`cross_project_active_use`、`approvable`。
- `POST /cases` — 提交申请并生成案件（同时接收证书摘要、核验结论、标准版本、成绩范围、有效期）。成功 `201`。
- `POST /cases/{id}/supplement/submit` — 本人补交（说明和证据编号至少一项），超期返回 `supplement_deadline_passed`。
- `GET /cases` — 本人案件列表；审核员/记账岗为全部案件。
- `GET /cases/{id}` — 案件详情（含条目、权益、证据元数据、事件链）。他人案件返回 404。
- `GET /entitlements/mine` — 本人可消费/已消费权益。

### 教务审核员
- `POST /cases/{id}/verification` — 登记/更新颁发机构核验结论（`VERIFIED` 须带 `reference`）。
- `POST /cases/{id}/supplement/request` — 要求补交：`{"reason","due_days":1..30}`。
- `POST /cases/{id}/evaluation/start` — 核验通过后进入评估。
- `POST /cases/{id}/evaluation/decision` — 逐项决定，见下。
- `POST /cases/{id}/reject` — 直接驳回结案（核验失败、材料不实、补交超期等），`{"reason"}`。
- `POST /cases/{id}/revoke` — 撤销已生效决定，`{"reason"}`。

### 学籍记账岗
- `POST /cases/{id}/ledger/post` — 原子记账，权益 GRANTED→CONSUMED，案件→POSTED。

### 敏感证据（授权由服务层二次校验）
- `POST /cases/{id}/evidence` — 本人或被分配审核员上传（≤1 MiB）。
- `GET /evidence/{id}` — 仅本人或被分配审核员可读取正文，其余一律 404。

### 基础数据
- `GET /reference/mappings` — 已发布映射目录。

## 申请体示例

```json
{
  "program_id": "PRG-SE",
  "version_id": "VER-IDSB-2024",
  "certificate": {
    "holder_name": "李娜",
    "serial_number": "SWE-2026-0001",
    "title": "国际软件工程师认证",
    "issued_at": "2025-06-01",
    "valid_from": "2025-06-01",
    "valid_until": "2028-06-01"
  },
  "verification": {
    "status": "VERIFIED",
    "reference": "AUTH-RCP-001",
    "method": "API",
    "verified_at": "2026-09-20"
  },
  "score": { "achieved": 88, "scale_min": 0, "scale_max": 100 }
}
```

## 决定体示例

```json
{
  "comment": "实践模块学分不足",
  "items": [
    { "course_id": "C-DS", "verdict": "APPROVED", "decided_credits": 2.0 },
    { "course_id": "C-DB", "verdict": "REJECTED" }
  ]
}
```

规则：所有条目必须给结论；认可学分 `0 < decided_credits ≤ 映射学分`；
全驳回 `comment` 必填且不写权益；全认可→APPROVED，其余→PARTIALLY_APPROVED。

## 错误码

| code | 含义 |
| --- | --- |
| `validation_error` / `no_eligible_benefit` | 输入或试算不合规 |
| `permission_denied` / `not_found` | 认证/授权失败（无权与不存在统一 404） |
| `state_conflict` | 状态不允许该动作 |
| `supplement_deadline_passed` | 补交超期 |
| `verification_invalid` | 核验未通过，阻塞评估/批准 |
| `credential_expired` | 提交或批准时证书过期 |
| `mapping_changed` | 映射停用/变更，需重新立案 |
| `duplicate_credential` | 证书指纹重复注册 |
| `duplicate_entitlement` | **防重复消费触发（含跨项目、并发）** |
