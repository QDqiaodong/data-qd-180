# radio-bulletin 离线合成样例

本目录的台站、发射、窗口、分组与公开规则均为原创合成值（MIT 许可），离线可运行，不对应真实场馆、台站或活动。所有时间为整数微秒、所有频率为整数 Hz；这里的“保护差距”只是约定模型下的公开审查演示，不代表真实保密分级。

## 共同背景（`plan.json`）

两个场馆 N、S，各有接收站 `RN`、`RS` 与远端发射站 `TN`、`TS`。两个 1 微秒短任务：

- `n-h`：`TN → RN`，中心 1000 Hz，落在高频组 H `[100, 3000)`；
- `s-h`：`TS → RS`，中心 2000 Hz，同样落在 H。

统计窗口覆盖当天，故真实矩阵为 N=(1, 0)、S=(1, 0)（H/L 两列），H 列合计 2、L 列合计 0、全表合计 2。

频道组 H `[100,3000)`、L `[3000,6000)`，半开区间、端点相接不重叠。RULES 的公开规则（各文件相同的部分）：

- 除固定为 0 的 S/L 外，每个单元格公开区间 `[0,2]`；S/L 固定 `[0,0]`；
- N 行合计公开区间 `[1,2]`；H 列合计公开区间 `[2,3]`；
- 受保护格 N/H，要求 `gap_us = 1`（公众可行矩阵中 N/H 取值跨度至少 1 微秒）。

## `rules-h-only.json` —— 空历史，只请求 H 列

公众在公开区间下能让 N/H 取 0 或 1（例如把 H 的需求量放到 S/H），保护差距仍在，故 H 列**准许**，公布精确值 **2**。

## `rules-n-then-h.json` —— 空历史，先请求 N 行再请求 H 列

1. N 行先获准，公布真实值 **1**；
2. 此后 H 列的可能取值仍是 2 或 3。真实 H=2 时 N/H 仍有跨度，但**假设 H=3**：在每格 `[0,2]` 上限与 S/L=0 下，N/H 被固定为 1，差距消失。准入决定本身公开，因此 H 列必须**扣留**，只留下 N=1 一条公开记录。

## 跨次审查（测试内构造，不另存文件）

把 `rules-n-then-h.json` 产出公告里的 `prior`（含 N=1、revision=1）原样作为下次 RULES 的 `prior`，再次请求 H：H 仍须**扣留**，revision 递增为 2，说明已发布事实会被持续带入审查。

## 改变私有需求后准入改变（测试内构造）

保持相同公开区间与请求顺序，只给 N/L 增加 1 微秒私有需求（新增一个中心 4000 Hz、落入 L 组的 1 微秒任务）：真实 N 行变为 2，先公布 **N=2**；此时 H=3 仍可由 S/H 承担而不固定 N/H，故随后的 **H=2 获准**。对照上一例可见，改变后续准入的是**已经发布的事实**（N=1 vs N=2），而非请求本身。

## 演示

输出路径必须不存在；重复演示请换新路径（`run/` 已被忽略）。

```sh
radio-bulletin --source fixtures/bulletin/plan.json \
  --rules fixtures/bulletin/rules-h-only.json   --output run/bulletin-h.json
radio-bulletin --source fixtures/bulletin/plan.json \
  --rules fixtures/bulletin/rules-n-then-h.json --output run/bulletin-nh.json
```

源码树内可直接 `PYTHONPATH=src python3 -m spectrum.bulletin_cli ...`。`plan.json` 同时是合法的原计划输入，可直接交给 `radio-coordinate` 对照。
