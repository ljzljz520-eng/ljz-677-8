# 门诊处方数据报送台

把门诊处方（处方号、患者编号、药品、数量、费用）导入本地库，**分批**提交到监管接口，
监管端的受理/拒绝结果**逐行回写**；用户可以只重送失败行，**成功行绝不重复报送**。

零第三方依赖，Python 3.10+ 标准库 + SQLite 即可运行。

## 快速开始

```bash
# 1. 初始化数据库
python3 -m rx_report init

# 2. 导入处方 CSV（表头支持中文别名）
python3 -m rx_report import samples/prescriptions.csv

# 3. 分批报送（每批 50 行；可调整）
python3 -m rx_report submit --batch-size 50

# 4. 只重送失败行 / 传输失败行（成功行不会被选中）
python3 -m rx_report retry

# 5. 查询与导出
python3 -m rx_report stats
python3 -m rx_report list --status FAILED
python3 -m rx_report export result.csv
```

也可以直接跑端到端演示：`bash scripts/demo.sh`

### 故障模拟（验证用）

```bash
# 每批 30% 概率发生整批传输失败（503/超时），固定随机种子可复现
python3 -m rx_report submit --flaky 0.3 --seed 42
# 指定处方第一次经过时经历一次瞬时传输故障，重送即恢复
python3 -m rx_report submit --flaky-rx RX0009
```

## CSV 格式

`处方号,患者编号,药品,数量,费用`（支持处方编号、患者ID、药品名称、金额 等表头别名）。
- 文件内重复处方号、非整数数量、负数费用等脏数据行会被**拒绝并列出**，不影响其他行；
- 已在库的处方再次导入会被跳过（duplicate），历史报送状态/回执不会被覆盖。

## 行状态与结果回写

| 状态 | 含义 | 后续动作 |
|---|---|---|
| PENDING 待报送 | 刚导入 | submit 自动报送 |
| SUCCESS 成功 | 监管端已受理，记录监管回执 ACK | **冻结，永不再送** |
| FAILED 失败 | 监管端逐行业务拒绝（错误码+原因已回写该行） | 人工修正数据后 `retry` 重送该行 |
| TRANSPORT_FAILED 传输失败 | 整批未确认受理（503/超时） | `retry` 可安全重送 |

每行落库：监管回执、错误码（如 E_PATIENT_ID / E_DRUG_UNKNOWN / E_QTY_INVALID /
E_FEE_LIMIT）、错误信息、批次号、尝试次数，`export` 全部带回导出（utf-8-sig，Excel 直开）。

## 防止重复报送的三层保障

1. **选数层**：报送 SQL 只取 PENDING / FAILED / TRANSPORT_FAILED，
   SUCCESS 行在查询层就不可能被选出，因此 `submit` / `retry` 都无法整批重传成功行；
   本次运行已处理的行也会排除，避免同轮循环重送。
2. **回写层**：结果 UPDATE 带 SQL 护栏，SUCCESS 行的状态、回执、错误、尝试次数全部冻结，
   任何代码路径都无法把它改回失败或覆盖回执。
3. **接口层**：监管端以**处方号为幂等键**，重复送达返回原回执（幂等），不产生重复监管记录。

整批传输失败与逐行业务拒绝严格区分：传输失败时监管端没有受理、也没有逐行结果，
只置 TRANSPORT_FAILED 等待重送，绝不会把未送达的行误判成业务失败。

## 代码结构

```
rx_report/
  models.py     # 状态常量、Prescription、RowResult、TransportError
  db.py         # SQLite 仓储：导入去重、按状态选数、逐行回写（含成功行护栏）
  importer.py   # CSV 解析与行级校验
  regulator.py  # 监管接口 Mock：分批受理、逐行校验、幂等、传输故障模拟
  service.py    # 报送编排：分批提交、结果回写、failed_only 重送
  exporter.py   # 逐行结果导出 CSV
  cli.py        # 命令行
tests/          # 端到端测试（python3 -m unittest discover -s tests）
samples/        # 示例数据（含各类异常行）
scripts/demo.sh # 全流程演示
```

## 接真实监管接口

实现一个与 `RegulatorClient.submit_batch(batch: list[dict]) -> list[RowResult]`
同形的 HTTP 客户端即可：整批 5xx/超时抛 `TransportError`，2xx 时按监管端返回的
逐行受理标志构造 `RowResult`。建议请求中携带处方号作为幂等键，使第 3 层保障在真实网络下成立。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：导入校验与重复跳过、混合批次逐行回写、瞬时传输故障恢复、只重送失败行、
成功行全程只送达一次、整批故障无业务误判、导出含回执与错误码、成功行不可被覆盖。
