# radio-coordinate 执行观测复核离线合成样例

本目录的站点、纪元与设备日志均为原创合成值（MIT 许可），离线可运行，不对应真实台站或设备。坐标、误差范围与计时器读数只用于演示联合校时与有向冲突复核，不代表真实传播或许可。

## 共同背景：R/T/S/U 三任务（`rtu-*.json`）

站点 R=(0,0) 保护半径 1000m；T=(0,0.001)、S=(0,0.002) 保护半径 1m；U=(10,10) 保护半径 1m。计划任务：

- `v`：T→R；`a`、`b`：S→U；三者计划与实测中心均 100Hz、带宽 10Hz（另有改频对照）。
- 纪元 E0/E1 的 `utc_origin` 均为 `2026-08-01T10:00:00Z`。E0 偏移固定 0；E1 为带误差范围的未知整数。
- `v` 用 E0 的 tick `[10000000, 11000000)`；`a` 用 E1 的 `[9000000, 10000000)`；`b` 用 E1 的 `[11000000, 12000000)`。

### `rtu-obs-exclusion.json` —— “单条可能”不等于“能一起发生”

E1 ∈ [-1000000, 1000000]。组 `g-exclusion` 含 `a→v`、`b→v` 两条有向冲突：

- 单条看两者都是 `possible`；但它们竞争同一个 E1 偏移，互相排斥——E1 ≥ 1 时只有 `a→v` 发生，E1 ≤ -1 时只有 `b→v` 发生，E1 = 0 时两者都只与 v 的时间窗端点相触（半开区间，不算冲突）。
- 因此组 **min=0、max=1**；按字典序平局规则，最小见证 E1=0（无冲突），最大见证 E1=-1000000（发生 `b→v`）。

### `rtu-obs-fixed.json` —— 收紧为固定偏移后的确定性

E1 固定 500000 后，`a→v` 为 `certain`、`b→v` 为 `absent`，组 min=max=1，两个见证的 E1 都是 500000。

### `rtu-obs-retuned.json` —— 实测改频改变实际交付

仅把 `a` 的实测中心改为 200Hz（计划与原计划报告都不变）：频段不再与 v 相交，`a→v` 必须为 `absent`。展示实测身份（频段）与校时事实共同决定实际冲突，而原计划冲突报告原样保留。

### `rtu-obs-empty.json` —— 空观测合法

`executions` 为空：所有有向复核行均为 `unobserved`，不把缺数据解释成没有发射；无组可评估。

### `rtu-obs-wide.json` —— 宽 offset 域不按微秒展开

E1 域取到端点 ±3,600,000,000 微秒（±1 小时），结论仍为组 min=0、max=1；割点搜索的分支数与 ±1 秒域相同。

## 联合矛盾与校时链（`chain-*.json`）

`chain-plan.json` 复用 R/T/S/U 站点与三任务。`chain-obs-inconsistent.json` 有三个纪元 E0/E1/E2 与三条相对证据：

- E1−E0 ∈ [100,200]，E2−E1 ∈ [100,200]，单独都可满足；
- 闭环证据 E2−E0 ∈ [0,100] 与前两条联合后形成负环（前两条要求 E2−E0 ≥ 200）。

每条记录单独都成立，只有联立整套全局 offset 才能发现矛盾：结果 `inconsistent_clocks`，退出 0，保留原计划报告，复核表/校时范围/见证为 `null`，不丢弃任何证据。

## 演示

输出路径必须不存在；重复演示请换新路径（`run/` 已被忽略）。

```sh
radio-coordinate --source fixtures/observations/rtu-plan.json \
  --observations fixtures/observations/rtu-obs-exclusion.json \
  --output run/rtu-review.json
radio-coordinate --source fixtures/observations/chain-plan.json \
  --observations fixtures/observations/chain-obs-inconsistent.json \
  --output run/chain-review.json   # 退出 0，status=inconsistent_clocks
```

源码树内可直接 `PYTHONPATH=src python3 -m spectrum.cli ...`。见证里的 `observed_plan` 是完整 `stations`/`transmissions` 对象，可直接作为无观测 `radio-coordinate` 的输入，其输出冲突数应与见证的 `conflict_count`（仅计该组 pair）一致。
