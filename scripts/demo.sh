#!/usr/bin/env bash
# 端到端演示：导入 -> 报送（含瞬时传输故障）-> 只重送失败行 -> 导出
set -u
cd "$(dirname "$0")/.."
DB=/tmp/rx_demo.db
OUT=/tmp/rx_result.csv
rm -f "$DB" "$OUT"

echo "================ 1. 初始化 ================"
python3 -m rx_report --db "$DB" init

echo; echo "================ 2. 导入处方 CSV ================"
python3 -m rx_report --db "$DB" import samples/prescriptions.csv

echo; echo "================ 3. 分批报送（batch-size=4，RX0009 首传瞬时故障）================"
python3 -m rx_report --db "$DB" submit --batch-size 4 --flaky-rx RX0009

echo; echo "================ 4. 只重送失败行（成功行不动）================"
python3 -m rx_report --db "$DB" retry --batch-size 4

echo; echo "================ 5. 再点一次 retry，证明失败行才会重送 ================"
python3 -m rx_report --db "$DB" retry --batch-size 4

echo; echo "================ 6. 统计与失败明细 ================"
python3 -m rx_report --db "$DB" stats
python3 -m rx_report --db "$DB" list --status FAILED

echo; echo "================ 7. 导出逐行结果 ================"
python3 -m rx_report --db "$DB" export "$OUT"
echo "导出文件内容："
cat "$OUT"
