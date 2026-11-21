# radio-protection 重算与更正离线合成样例

本目录的站点、来源、资料与计时全部为原创合成值（MIT 许可），离线可运行，不对应真实台站、测绘记录或值班日志。坐标与半径只用于演示“两个发布视图 + 更正清单”，不代表真实传播或许可。

## 共同背景（`plan.json`）

- 站点 `R=(0,0)`、`T=(10,10)`、`A=(0,0.002)`、`B=(0,0.001)`、`U=(20,20)`。计划坐标只用于通过原契约，**不参与保护结论**。
- 任务 `v`：`T→R`，10:00–11:00；`a`：`A→U`，10:00–10:40；`b`：`B→U`，10:20–11:00。三者中心 100Hz、带宽 10Hz。
- 窗口 10:00–11:00；`published_as_of = 09:00`，`revised_as_of = 12:00`。来源 `duty`（priority 5，低）与 `survey`（priority 1，高）。
- 所有 `*-facts.json` 都先由 `duty` 在 08:00 声明全天资料：`R` 半径 300m，`T/A/B/U` 为 1m。因此 `v` 的接收站到 `a`、`b` 发射站的距离（222.390m、111.195m）都落在 300m 内。

## `recompute-facts.json` —— 任务指定的发布更正

`survey` 在 **11:30** 才报来一份覆盖 **10:15–10:35** 的资料，把 `R` 的半径改为 150m。它进入 `revised`，不改变 `published`。

- **原日报**：`v` 受影响 **60 分钟**（10:00–10:40 受 `a`，10:20–11:00 受 `b`）。
- **重算后**：`v` 受影响 **55 分钟**（10:15–10:35 半径缩到 150m，`a` 距离 222m 落在圈外）。
- **更正清单**恰为两条：
  - `[10:15,10:20)`：“受 `a` 影响” → 无干扰；
  - `[10:20,10:35)`：“受 `a`、`b` 影响” → “只受 `b` 影响”。

## 其它样例

| 文件 | 演示 |
| --- | --- |
| `dispute-facts.json` | 同级 `survey` 两份资料给出 150m 与 160m，覆盖低优先级 300m 层；10:15–10:35 为 `disputed`，`v` 该段未决，不靠接收先后裁决 |
| `withdraw-facts.json` | `survey` 在 08:30 声明 150m，11:30 撤回。`published` 用 150m，`revised` 无该声明，**低优先级 300m 资料恢复**（不是“无资料”） |
| `shorten-facts.json` | 同一份资料被缩短到 10:20–10:30；被取代版本的 10:30–10:35 尾段**不再使用**，`v` 重算后受影响 60 分钟 |
| `version-only-facts.json` | 11:30 报来一份**数值相同**（300m）的新版本：依据变了，结论不变，更正清单为空 |
| `mixed-facts.json` | `B` 在 10:20–10:45 有同级争议；`v` 在 10:20–10:40 仍确定受 `a` 影响，同时保留未决集合 `[b]`；10:40–10:45 为未决 |
| `boundary-plan.json` + `boundary-facts.json` | `a` 延长到 11:00；10:30 同一时刻 `A` 移出保护圈、`B` 移入。10:30–11:00 合并为一条“`[a]` → `[b]`”更正 |
| `missing-plan.json` + `missing-facts.json` | 任务对无正频段交集；所有站点缺资料，两个视图都不产生冲突/未决证据，接收时间线全为 `clear`，更正为空 |

## 运行

输出路径必须不存在；重复演示请换新路径（`run/` 已被忽略）。

```sh
radio-protection --source fixtures/protection/plan.json \
  --station-facts fixtures/protection/recompute-facts.json \
  --output run/protection-recompute.json
radio-protection --source fixtures/protection/boundary-plan.json \
  --station-facts fixtures/protection/boundary-facts.json \
  --output run/protection-boundary.json
```

源码树内可直接 `PYTHONPATH=src python3 -m spectrum.protection_cli ...`。

## 接近上限的离线用例

`generate_large.py` 生成 500 个站点、300 条任务、恰好 20,000 条资料的确定性文档；其中 19,500 条来自**低优先级**来源且从不改变生效层，另有一条高优先级资料落在**不被任何任务引用**的站点上：

```sh
PYTHONPATH=src python3 fixtures/protection/generate_large.py run/protection-large
radio-protection --source run/protection-large/plan.json \
  --station-facts run/protection-large/facts.json \
  --output run/protection-large/report.json
```

`facts-baseline.json` 只保留权威层；用它与 `facts.json` 得到的两个视图日报和更正清单应完全相同，而 `report.json` 中低优先级站点的天线时间线仍是**整窗一段**（噪声未逐条展开）。`tests/test_protection.py::LargeScaleTests` 自动运行该对照并检查文件小于 8 MB。
