# 计划损失结算草案 `radio-settle` v1

活动结束后，财务按确认的固定计划起草频道履约补偿。原有向冲突行只说明“某发射妨碍了某接收任务”：把每条冲突的时长乘带宽再相加，会把同时被多家干扰的同一块频道重复计损；只算一个总损失，又无法证明某责任方确实能为某份承诺补偿，也无法防止它在不同承诺之间超用额度。本命令交付**可对账**的两部分：

1. **损失计量**：对每份承诺，把落在承诺区域内的受干扰时频块，按“恰好覆盖该块的责任经营方集合”分组计量；
2. **补偿指令草案**：在经营方额度约束下，按三层字典序最优给出每承诺、每经营方的补偿分配。

这里结算的是**约定模型下的计划损失**，不是对现场实测或实际传播的认定；命令**不改计划、不执行付款**，只产出草案。几何与冲突判定完全复用原模型（`spectrum.interference` / `spectrum.service`）：接收站声明保护圈、大圆距离（地球半径 6371008.8m）、半径含边界、UTC 半开时间区间、精确半 Hz 频段、**有向**冲突。只用 Python 标准库；离线运行；计量与补偿都**不按微秒、半 Hz 或补偿单位枚举**（计量是坐标压缩扫描，补偿是确定性最大流序列，代价只取决于损失组数量，与数值大小无关）。原 `radio-coordinate`（含 `--observations`）、`spectrum.service.run`、`radio-workbench`、`radio-arrange`、`radio-protection` 的用法与契约均不变。

## 入口与退出约定

```sh
radio-settle --source PLAN.json --terms TERMS.json --output REPORT.json
```

未安装时可在源码树运行 `PYTHONPATH=src python3 -m spectrum.settle_cli ...`。

- 成功（包括零损失、零额度、没有可补偿责任方、没有承诺）：退出 0，stdout 打印报告 `summary`，报告经临时文件**原子新建**于 `--output`；目标已存在则拒绝（退出 2）并保留原目标，绝不覆盖。
- 任一输入契约错误：退出 2，stderr 一行 `radio-settle: <原因>`，不发布报告。
- 两份输入各自沿用 **8 MB** 上限，重复 JSON 键与非有限常量一律拒绝。

公开纯函数 `spectrum.settle.build_settlement_report(plan, terms)` 不读写文件，可独立测试；契约违例抛 `ValueError`。`spectrum.settle.solve_settlement(commitments, credit_capacities)` 在抽象损失组上执行补偿分配，供独立核对。

## 计划输入（PLAN）

沿用原 `stations`、`transmissions` 结构和限制（见[数据契约](contract.md)）：`stations` 1..500 个、`transmissions` ≤300 条，台站 `id,lat,lon,protection_radius_m`，任务 `id,station_id,receiver_id,center_hz,bandwidth_hz,start,end`。新命令对两份文件**严格校验结构和值，包括不参与结算的记录**：拒绝未知或缺失字段、重复身份、悬空引用、布尔值冒充整数（坐标拒绝布尔值）。计划时间按原契约解析（`spectrum.model.stamp`）。

## 条款输入（TERMS，根对象恰含三键）

`operators`、`station_operators`、`commitments`。

### operators（≤32 项）

每项恰含 `id`、`credit_capacity`。`id` 非空字符串、唯一、保留原始拼写；`credit_capacity` 为 0..10^24（含端点）的整数，是本次结算可冲抵的总整数单位。额度为 0 的经营方仍参与计量（保留在责任方集合中），只是不能付款。

### station_operators（恰好覆盖每个计划台站）

每项恰含 `station_id`、`operator_id`：`station_id` 必须引用计划台站且每台站恰好出现一次，`operator_id` 必须引用已登记经营方；不得遗漏、不得重复、不得引用计划外台站。

- 一次发射的**责任方**取其**发射站**（`station_id`）的经营方；
- 一份承诺的**受益方**取其任务**接收站**（`receiver_id`）的经营方，与该承诺记录自身的发射站归属无关。

### commitments（≤64 项，可以为空）

每项恰含 `id,transmission_id,start,end,low_twice_hz,high_twice_hz,priority`：

- `id` 唯一；`transmission_id` 必须引用计划任务，且**一个任务最多一份承诺**；
- `start`、`end` 为带显式偏移的 ISO-8601 时间，小数秒最多六位（保证微秒整数）；`start < end` 且整个半开区间 `[start,end)` 完全落在该任务原时段内；
- `low_twice_hz`、`high_twice_hz` 为整数（0.5 Hz 单位的位置），`low < high` 且整个半开频段 `[low,high)` 完全落在该任务占用频段内；
- `priority` 为 0..999 的整数，**越小越优先**。

不同任务的承诺分别履约，即便同属一个接收站；不跨承诺合并损失，只跨承诺合计责任方额度。

## 损失计量

对每份承诺，以其任务为受保护接收任务、其余每一任务为候选干扰方，按原有向保护圈规则（大圆距离 ≤ 接收站保护半径，含边界）取得有向冲突证据，再把每条证据的时段与频段**一起裁剪**到承诺区域，只保留两维都有正交集的部分。报告逐承诺列出裁剪后的证据（干扰任务、干扰站、经营方、起止、频段端点、单位数），核账人可据此追溯每组计量。

- 时间按 UTC 微秒、频率按整数两倍 Hz，**不用浮点算面积**；一个计量单位是 1 微秒 × 0.5 Hz（例如 3 微秒 × 1.5 Hz = 9 单位）。
- 承诺区域中被至少一项干扰覆盖的部分才是损失。对该区域每一部分，其**可付款经营方集合** = 覆盖该部分的干扰任务的经营方集合**去掉受益方本身**；同一经营方的多个任务覆盖同一区域只出现一次；零额度经营方仍保留在集合中；仅被本方干扰的区域仍计损失，归到**空集合**。
- 报告把损失按「恰好有哪些可付款方」分组：给出每组的完整经营方 ID 集合与精确单位数；不同位置有同一集合时合并。未被干扰的区域不计入；没有损失的承诺仍须有记录。
- 经营方额度与承诺优先级**不能改变计量结果**。

## 补偿分配

每组可分配不超过本组计量单位的整数补偿（每 1 单位损失最多补偿 1 单位）；一组的款可由其集合中的多个经营方拆分承担；**空集合不能产生补偿**。经营方在整份报告里承担的总量不得超过自身额度。不得因某经营方也出现在同一承诺的其他组，就让它为缺少其 ID 的组付款。发票不必指出组内具体哪一微秒或半 Hz 由谁承担，但每一笔补偿必须能对回承诺、损失组与责任方。

在同时满足以上约束的全部整数分配中，按以下顺序确定结算结果：

1. **最大化全场补偿总单位数**；
2. 总量最优前提下，按 `(priority, commitment id)` 升序比较各承诺拿到的补偿量，依次取**字典序最大**的向量；
3. 前两项均相同时，仍按该承诺顺序，对每份承诺依经营方 ID 升序排列其各经营方支付量（含零），依次取该向量的**字典序最大**值。

承诺优先级与经营方排序都不能让全场少补偿；不按输入顺序局部贪心、不用含舍入的近似、不以超时方案冒充最优。实现把三层目标化为同一残余网络上的**确定性最大流序列**（环流网络：源→承诺计量→损失组→(承诺,经营方)计量→经营方额度→汇→源；每确定一个计量边就把它锁定再优化下一个），整数容量、精确大整数，不逐单位增广。

**组内拆分规则**：发票（每承诺每经营方支付量）确定后，同一承诺内任一满足「各组不超过其计量、各经营方合计等于发票」的拆分都等价可行。本实现对每份承诺单独运行一次确定性 Dinic 最大流（损失组按报告顺序、经营方按 ID 升序建图）取其结果；等价输入必得同一拆分。

## 报告结构

| 键 | 内容 |
| --- | --- |
| `schema_version` / `tool` / `model` / `mode` | `1` / `radio-settle` / 原模型标识 / `planned_loss_settlement` |
| `commitments[]` | 按 `(priority, commitment id)` 升序；每项含 `commitment_id`、`transmission_id`、`priority`、`beneficiary_operator_id`、`region`（起止 UTC 与两倍 Hz 端点）、`commitment_units`（承诺总量）、`loss_units`（损失量）、`own_only_loss_units`（仅本方造成的损失量）、`compensable_loss_units`（可补偿损失量＝非空责任方集合各组计量之和）、`paid_units`（实付量）、`unpaid_units`（因额度或竞争未补量＝可补偿损失量−实付量）、`evidence`（裁剪后的有向证据）、`loss_groups`（各组 `operator_ids`、`units`、`allocated_units` 与组内 `payments`） |
| `operators[]` | 按经营方 ID 升序；每项含 `operator_id`、`credit_capacity`（原额度）、`payments`（按受偿承诺拆分的支付量，按承诺结算顺序）、`paid_units`（支付总量）、`remaining_credit`（剩余额度）、`receivable_units`（应收总量＝其作为受益方的所有承诺实付之和；没有收付也可查） |
| `totals` | 全场合计：`commitment_units`、`loss_units`、`own_only_loss_units`、`compensable_loss_units`、`paid_units`、`unpaid_units`，以及对账字段 `sum_commitment_paid_units`、`sum_operator_paid_units` |
| `summary` | 站点/任务/经营方/承诺计数、有损失承诺数与各项总量 |

**对账不变量**：`totals.paid_units` ＝ 所有承诺 `paid_units` 之和 ＝ 所有经营方 `paid_units` 之和（报告内 `sum_commitment_paid_units` 与 `sum_operator_paid_units` 分别给出两个合计）；每承诺 `paid_units` ＝ 其各组 `allocated_units` 之和 ＝ 其发票支付量之和；每经营方 `paid_units` ＝ 其 `payments` 之和且 ≤ `credit_capacity`；受益方 `receivable_units` 与支付方合计交叉一致。

## 确定性

输出只取决于业务值：输入数组重排、同一时刻换一种等价偏移写法，报告逐字不变。承诺按 `(priority, id)`、经营方按 ID、组按集合 ID 升序、证据按干扰任务 ID 排列；时间一律 UTC ISO-8601。

## 边界声明

保护圈、大圆距离、半开时间与半 Hz 频段都是**使用者声明的几何筛查模型**：没有地形、功率、天线、接收选择性或真实传播预算。本命令的“损失”与“补偿”仅是这些声明规则与三层字典序目标下的**计划量草案**，不表示现场实测、实际传播认定、真实无线电许可或付款执行。

## 样例与测试

见[离线合成样例](fixtures/settle/README.md)（相邻两个 1 微秒窗口、甲乙两种额度对照）。`tests/test_settle.py` 含两个**独立预言机**（补偿分配的整数枚举、按微秒×半 Hz 逐格计量的枚举），在大量随机小实例上对照三层最优结果与组内拆分守恒；另覆盖二维部分交叠、半 Hz 边界与端点接触、同一经营方多个源叠加、仅本方与外方共同干扰、不同责任组共享额度上限、总量与承诺实付并列而发票分摊不同、宽时段宽频段的精确大整数、坏输入、排序稳定、原子发布与原命令兼容。
