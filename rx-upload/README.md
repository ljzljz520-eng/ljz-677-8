# 门诊处方数据报送台

零依赖（Python 3 标准库），导入处方明细 → 分批报送监管接口 → 异常逐行回写 → 仅失败行可重送。

## 运行

```bash
cd rx-upload
python3 server.py 8000        # 打开 http://127.0.0.1:8000
```

## 功能与防重复设计

| 需求 | 实现 |
|---|---|
| 导入处方号/患者编号/药品/数量/费用 | `POST /api/import`，CSV（中文表头）或 JSON 行 |
| 分批提交监管接口 | `POST /api/batches {line_ids}`，每批生成批次号并落库 |
| 异常回写到每一行 | 每行记录 `status / last_code / last_error / batch_no / attempts` |
| 只能重送失败行 | ① 前端：已受理/报送中行的复选框禁用；② 服务端：仅 `PENDING/FAILED` 可入批，其余拒绝并计入 `skipped`；③ 认领用条件 UPDATE 原子完成，防并发重复 |
| 整批重传不重复 | 三重幂等：导入侧幂等键 `处方号#行号` 唯一约束去重；报送侧成功行锁定；监管侧台账按幂等键去重（重复报送返回"幂等受理"，不重复入账） |

## 接口

- `GET  /api/lines?status=ALL|PENDING|SUBMITTING|SUCCESS|FAILED` 明细行
- `GET  /api/stats` 各状态计数
- `GET  /api/batches` 批次记录
- `POST /api/import` `{csv: "..."}` 或 `{rows: [...]}`
- `POST /api/batches` `{line_ids: [1,2,3]}`（待报送与失败行都走此入口）

## 说明

- `regulatory.py` 是模拟监管接口，内含：永久性校验错误（数量≤0、费用<0、患者编号缺失）、
  模拟瞬时故障（冷链药品首报超时、重试即成功）、监管侧幂等台账。
  接入真实监管平台时，把 `submit_batch` 换成 HTTP 调用即可，出入参结构不变。
- 数据存于 `rx_upload.db`（SQLite，WAL 模式）。
