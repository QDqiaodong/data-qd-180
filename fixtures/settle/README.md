# radio-settle 离线合成样例

本目录的台站、发射、经营方与承诺数据均为原创合成值（MIT 许可），离线可运行，不对应真实台站、经营者或账务。坐标只用于演示声明式保护圈筛查，不代表真实传播或许可；这里的“补偿”是约定模型下的计划损失结算草案，不是付款执行。

## 共同背景：相邻两个 1 微秒窗口（`plan.json`）

接收站 `R`=(0,0)，保护圈 5000m。任务 `taskA`、`taskB` 都由远处台站 `TA` 发往 `R`，中心 1000Hz、带宽 2Hz（占用 [999,1000) Hz，即两倍赫兹 [1998,2000)），各占相邻的 1 微秒窗口：`taskA` 10:00:00.000000–10:00:00.000001，`taskB` 10:00:00.000001–10:00:00.000002（端点相接，互不重叠）。干扰发射 `iJia`（`TJ`→`Z`）覆盖两个窗口，`iYi`（`TY`→`Z`）只覆盖第一个窗口；`TJ`、`TY` 都在 `R` 的保护圈内。经营方：`R`、`TA` 属 `丙`，`TJ`、`Z` 属 `甲`，`TY` 属 `乙`；受益方是接收站 `R` 的经营方 `丙`，不属于甲、乙。

承诺（`terms.json` 与 `terms-tight.json` 相同）：`cA` 保 `taskA`（priority 0）、`cB` 保 `taskB`（priority 1），区域都是各自整个 1 微秒 × 1 Hz 窗口，各 2 个计量单位（1µs × 2 个半 Hz）。

- `cA` 的损失组为 `{甲, 乙}` = 2 单位（甲的 `iJia` 与乙的 `iYi` 同时覆盖）；
- `cB` 的损失组为 `{甲}` = 2 单位（只有 `iJia` 覆盖）。

## `terms.json` —— 甲、乙额度各 2

总量 4 可达；`cB` 只能由甲承担，用光甲的 2 单位额度，`cA` 由乙承担 2 单位。结果：`cA` 实付 2（乙→cA 2），`cB` 实付 2（甲→cB 2），全场总补偿 4；丙应收 4（自己额内无支付）。若按经营方 ID 贪心先用甲，甲会被 `cA` 耗尽、`cB` 落空——本例说明三层字典序（总量 → 承诺 → 发票）不会让 ID 顺序造成全场少补。

## `terms-tight.json` —— 甲额度 1、乙额度 0

计量完全不变（零额度的乙仍留在 `cA` 的 `{甲, 乙}` 组里）。可兑付的只有甲的 1 单位；按 `(priority, id)` 顺序优先的 `cA` 实付 1（甲→cA 1），`cB` 实付 0，总量 1；丙应收 1。

## 演示

输出路径必须不存在；重复演示请换新路径（`run/` 已被忽略）。

```sh
radio-settle --source fixtures/settle/plan.json --terms fixtures/settle/terms.json       --output run/settle-report.json
radio-settle --source fixtures/settle/plan.json --terms fixtures/settle/terms-tight.json --output run/settle-tight-report.json
```

源码树内可直接 `PYTHONPATH=src python3 -m spectrum.settle_cli ...`。`plan.json` 同时是合法的原计划输入，可直接交给 `radio-coordinate` 对照有向冲突证据。
