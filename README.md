# 药物警戒案例处理系统

使用 Python 标准库实现的独立原型，覆盖多渠道案例接入、去重、随访更正、严重性医学裁定、分国家报告、逾期升级、跨区域权限和案例合并审计。

## 运行

要求 Python 3.11+。

```bash
python3 app.py --db pharmacovigilance.db
```

默认监听 `127.0.0.1:8201`。首页为 `http://127.0.0.1:8201/`，健康检查为 `/health`。

所有接口使用请求头 `X-User-Id`、`X-Role` 和区域角色必需的 `X-Region`。角色为 `reporter`、`regional_lead`、`medical_reviewer`、`global_admin`。

## 主要接口

- `POST /api/cases`：录入案例，`dedupe_key` 相同则返回已存在案例。
- `GET /api/cases`、`GET /api/cases/{id}`：按权限查询。
- `POST /api/cases/{id}/followups`：用 `expected_revision` 防止覆盖随访。
- `POST /api/cases/{id}/medical-review`：医学审核员更新严重性、死亡和关联性。
- `POST /api/cases/{id}/reports`、`POST /api/reports/{id}/submit`：生成并提交分国家报告。
- `POST /api/cases/{id}/merge`：全局管理员合并重复案例。
- `GET /api/followup-tasks`：查询随访计划任务，可按 `status`、`case_id` 过滤。
- `POST /api/escalate-overdue`、`GET /api/overdue`：逾期检查与升级。

## 随访计划

医学裁定后自动为案例排随访任务：严重或死亡案例按入库时间 30 天后到期，非严重 90 天后到期。同一案例只保留一个未关闭任务；提交随访会关闭当前任务并按当前严重性续排下一次，再次裁定会取代旧任务。案例合并后，源案例的原任务保留仅供查询、不能再提交随访；若目标案例已有更早的待随访任务则沿用，否则采用来源案例的排期。报告逾期升级逻辑不受影响。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

该实现使用请求头模拟身份，不含生产级登录、签名和密钥管理；SQLite 与标准库 HTTP 服务适合单机原型。分国家规则采用内置严重 15 天、死亡 7 天、非严重 90 天规则，接入真实监管网关前需按当地法规扩展。
