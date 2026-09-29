# 技能证书互认申请

毕业生凭境外技能证书在培养项目中申请课程减免的完整后端：接收证书摘要、颁发机构验证、标准版本、成绩范围与有效期，依据已发布映射试算可申请权益并生成审核案件；覆盖补交材料、部分认可、撤销决定与跨项目使用约束，批准动作原子写入可消费权益并阻止重复领取，敏感证据只向本人与授权审核员开放，历史处理完整可追溯。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/recognition/`：互认后端（标准库实现，无第三方依赖）。
  - `db.py`：SQLite schema、部分唯一索引（防重复消费的最终闸门）与种子数据。
  - `models.py` / `enums.py` / `errors.py`：领域模型、状态枚举、业务错误。
  - `service.py`：全部业务规则，写操作均在 `BEGIN IMMEDIATE` 事务内。
  - `repo.py`：只读查询与序列化。
  - `app.py`：HTTP API（`http.server`）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归测试与后端业务/HTTP 测试。

## 验证

```bash
python3 -m unittest discover -s tests -v     # 全部测试（契约 + 16 个后端用例）
python3 -m compileall -q src tools tests      # 编译检查
python3 tools/check_contract.py domain/contract.json
```

## 运行

```bash
python3 -m src.recognition.app --db recognition.sqlite3 --port 8080
# 首启自动建表并写入种子数据（项目、课程、机构、标准版本、映射、身份密钥）
```

鉴权统一使用 `Authorization: Bearer <api-key>`，种子密钥：

| 密钥 | 身份 |
| --- | --- |
| `sk-applicant-li` / `sk-applicant-wang` | 毕业生 |
| `sk-reviewer-chen` | 教务审核员 |
| `sk-registrar-zhao` | 学籍记账岗 |
| `sk-admin` | 教务处管理员 |

接口清单与状态流转见 [docs/API.md](docs/API.md)。

## 关键规则如何落地

- **映射权益**：申请时按「标准版本 + 证书标题 + 项目 + 成绩下限」选出达标映射并冻结为案件条目；批准前再次校验映射未停用/未变更，否则拒绝批准、要求按新版本重新立案。
- **证书验证**：核验状态为 `VERIFIED/PENDING/FAILED`，只有 `VERIFIED` 才能进入评估；机构回执编号必填。
- **有效期**：提交与批准两个时点都校验 `valid_until`，过期即拒。
- **补交材料**：审核员发起补交并设 1–30 天截止期，案件进入 `SUPPLEMENTING`；只有本人能补交，超期通道关闭，审核员也可直接驳回结案。
- **部分认可**：逐课程给 `APPROVED/REJECTED`，认可学分不得超过映射学分；全认可=批准、部分=部分批准、全驳回须填原因且不产生权益。
- **防重复消费（含跨项目）**：批准在同一事务写入权益与证书占用；
  `ux_entitlement_active`（同证书同课程唯一有效权益）和 `ux_credential_active_use`
  （一张证书全校仅一条占用）两个部分唯一索引在数据库层兜底，并发批准也只有一个成功。
- **记账领取**：记账岗把权益原子置为 `CONSUMED`、案件进入 `POSTED`，不可重复记账。
- **撤销决定**：批准/部分批准/已记账均可撤销，权益墓碑化（`REVOKED`）并释放占用，允许重新申请；已记账学分在事件中列明，供人工冲账。
- **隐私隔离**：证据正文只存在独立接口，仅本人与被分配的审核员可取（无权与不存在统一返回 404）；列表与事件只含证据元数据，审计日志不记录正文。
- **完整追溯**：`case_events` 仅追加、案件内序号连续，记录每次状态变化、决定明细、权益授予、记账与撤销。
