# radio-workbench 合成样例

本站点/发射数据均为本项目原创合成值，MIT 许可，离线可运行。地理坐标只用于演示保护圈筛查，不对应真实台站。

固定关系：R=(0,0) 保护半径 1000m；T=(0,0.001)、S=(0,0.002) 半径 1m；U=(10,10)。

- `plan-v0.json`：修订 0。`v: T→R`（100Hz）与 `s: S→U`（200Hz），带宽 10Hz，2026-08-01 10:00Z–11:00Z。二者互不冲突。
- `proposal-a.json`：基于修订 0 把 `v` 改到 200Hz。候选证据 `s→v`，接收站 R 需确认。
- `proposal-b.json`：基于修订 0 新增 `x: S→U`（参数与 `s` 相同）。不改变任何接收站当前证据，无需确认。
- `proposal-w.json`：新增 400Hz 的 `w`，与 A 的确认身份无关，可跨修订验证确认复用。
- `proposal-reorder.json`：两个 upsert 的数组顺序与时间偏移写法不同；自动测试用乱序重送验证它仍是同一业务内容。

演示（替换 `$L` 为尚不存在的台账路径）：

```sh
radio-workbench init    --ledger $L --source fixtures/workbench/plan-v0.json
radio-workbench propose --ledger $L --source fixtures/workbench/proposal-a.json
radio-workbench propose --ledger $L --source fixtures/workbench/proposal-b.json
radio-workbench preview --ledger $L --proposal proposal-a   # R 需确认 s→v
radio-workbench apply   --ledger $L --proposal proposal-b   # 修订 1，无需确认
radio-workbench preview --ledger $L --proposal proposal-a   # R 现需确认 s→v 与 x→v，旧确认不足
```

源码树内可直接 `PYTHONPATH=src python3 -m spectrum.workbench_cli ...` 运行同一命令。
