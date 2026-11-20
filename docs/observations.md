# 活动后执行观测复核 `radio-coordinate --observations` v1

面向活动频率协调员的**第二份证据**。原计划冲突报告回答“按排定时间和频道会发生什么”；现场设备日志按各自重启纪元（epoch）的本地计时器记录，校时只有带误差范围的证据，实测频道也可能偏离计划。本复核在**不随意挑选某一个校时结果**的前提下，判断实际执行冲突的确定性，并回答“一组可能冲突中，最少、最多究竟有几条能在**同一套时钟取值**下发生”。

复核完全复用原模型（`spectrum.model` / `spectrum.interference` / `spectrum.service`）：有向保护圈、大圆距离（地球半径 6371008.8m）、UTC 半开时间区间、精确半 Hz 频段；不新增传播模型，不做频道重新分配，也不改动 `radio-workbench` / `radio-arrange`。只用 Python 标准库，离线、确定性、无外部求解服务。

## 入口与退出约定

```sh
radio-coordinate --source PLAN.json --observations OBS.json --output REPORT.json
```

- 不传 `--observations` 时，行为、输出结构与失败语义与原命令**完全一致**（原计划报告、退出 2、原子新建）。
- 传入 `--observations` 时进入观测模式：报告先内嵌**完整原计划冲突报告**（`plan_report`，逐字保留，不被实测结论删改），再附复核结果（`review`）。
- 时钟证据相互矛盾（不存在满足全部约束的整数 offset 向量）属于**业务结果而非输入错误**：退出 0，发布 `inconsistent_clocks` 报告，保留原计划报告，复核表/校时范围/极值见证为 `null`，**不丢弃任何一条校时证据后继续**。
- 格式错误、悬空引用、重复身份、越界等输入契约错误：退出 2，stderr 一行 `radio-coordinate: <原因>`，**不发布报告**；输出目标已存在同样退出 2 并保留原目标（原子新建，绝不覆盖）。

## 公开函数（可独立测试）

模块 `spectrum.observations` 提供两个纯函数（不读文件、不写报告）：

| 函数 | 作用 |
| --- | --- |
| `review_executions(plan, observations)` | 校验观测文档并返回复核结果（`status` 为 `reviewed` 或 `inconsistent_clocks`）；契约违例抛 `ValueError` |
| `build_review_report(plan, observations)` | 先调用未改动的 `spectrum.service.run(plan)` 得到原计划报告，再附 `review_executions` 的结果，组装 CLI 发布的完整观测报告 |

`spectrum.service.run` 的原契约保持不变；工作台与编排器的接口均未改动。

## 时钟模型：一套全局 offset 向量

每个纪元是一次设备重启；同一台设备的不同重启使用不同纪元，**不自动沿用上一次偏移**。纪元的真实 UTC 时刻：

```
true_utc = utc_origin + local_tick_microseconds + offset_microseconds
```

其中 `offset` 是该纪元的一个**未知整数**，在整个纪元内共用，包含端点地取值于声明区间。所有纪元的声明区间与全部 `relative_syncs` 同时约束**同一套全局 offset 取值**；不能为每条记录、每个待查任务独立挑选偏移。

`relative_syncs` 约束 `offset[right] − offset[left] ∈ [min_us, max_us]`（含端点）。不同 ID 可约束同一对纪元；图不要求连通。实现把全部约束建成一个**差分约束系统**（difference constraints），用全源最短路径闭包求解：

- 出现负环 ⇒ 无整数可行向量 ⇒ `inconsistent_clocks`；
- 否则每个纪元在联合约束下真实可达的最小/最大 offset、以及任意两纪元差值的紧边界都可直接从闭包读出，而**不是回显原始声明区间**，也不会把收紧后的各纪元边界误当成彼此独立。

全程整数微秒，无浮点容差、无随机采样、无区间中点、无超时近似。

## 输入契约（OBS 根对象恰含四键）

根对象恰好含 `clock_epochs`、`relative_syncs`、`executions`、`groups`；任何对象多余或缺失字段都拒绝。所有 ID 为非空字符串，**保持原始拼写**（不去空格、不折大小写）；所有整数字段拒绝 JSON 布尔值；所有引用必须存在。数组排列不影响业务结果。

### clock_epochs（1..8 项，ID 唯一）

每项恰含 `id`、`utc_origin`、`offset_min_us`、`offset_max_us`：

- `utc_origin` 为带显式时区的 ISO-8601 时刻；
- 两个 offset 边界均为 ±3,600,000,000 内整数，下界 ≤ 上界。

### relative_syncs（0..32 项，ID 唯一）

每项恰含 `id`、`left_epoch_id`、`right_epoch_id`、`min_us`、`max_us`：两纪元必须不同且都存在；边界为 ±7,200,000,000 内整数，下界 ≤ 上界。

### executions（最多 300 项）

每项恰含 `transmission_id`、`epoch_id`、`start_tick_us`、`end_tick_us`、`center_hz`、`bandwidth_hz`：

- 每个计划任务至多一项执行观测；源任务、纪元必须存在；
- 两端站点与接收站身份**继承计划**，但时间与频段使用实测值，允许偏离原时段/原频道；
- tick 为 0..604,800,000,000 内整数，且 `start < end`；
- `center_hz`、`bandwidth_hz` 为正整数，沿用半 Hz 频段口径（双倍整数 Hz），频段不得跨 0Hz；
- 对每个执行的每个起止点，按该纪元**声明的原始 offset 上下界**换算都必须落在 Python `datetime` 可表示范围内，否则输入无效；
- **无观测表示缺数据，不能解释成没有发射**；空 `executions` 合法。

### groups（0..8 项，ID 唯一）

每项恰含 `id` 和 1..12 个不重复的 `pairs`；每个 pair 恰含 `victim_transmission_id`、`interferer_transmission_id`，两者不同且**都有执行观测**。不同组允许复用相同 pair。

## 复核口径

### 逐对有向复核（execution_review.rows）

对计划中每一对**不同任务**的有向“受害接收任务 / 干扰发射任务”组合输出恰一行（与原模型同样的排序，不合并方向）：

| status | 含义 |
| --- | --- |
| `unobserved` | 任一任务缺观测；此状态优先，绝不用原计划时间/频率代填 |
| `certain` | 两者均有观测，且在**每一个**可行全局 offset 向量下冲突都发生 |
| `possible` | 至少一个向量发生、至少一个向量不发生 |
| `absent` | 没有任何向量发生（保护圈/频段不相交自然落入此类；时间端点相触也不冲突） |

同一纪元的两个任务共用同一个 offset：单个纪元的宽误差范围**不**使它们的相对时间不确定。报告同时给出 `unobserved_transmission_ids`，避免把“只观测了子集”误读成“全场无干扰”。

### 联合分组极值（group_review）

组的问题是：在**同一套全局校时取值**下，该组这些有向冲突里有多少条发生——不是问它们是否在同一绝对时刻发生。每组给出全局可达的 `min_conflicts`、`max_conflicts`，以及各一个达到极值的见证（witness）。

- **不能**把 `possible` 行数相加当最大值，也**不能**把 `certain` 行数直接当最小值：不同冲突可能竞争同一个纪元偏移，互相排斥，或保证至少其一发生。
- 极值搜索对每个纪元只在“offset 割点”上分支（当前可达边界 + 与该纪元相关的时间谓词边界），割点之间的整段区间内所有时间谓词真值不变，因此复杂度与 offset 域的微秒宽度无关——宽达 ±3.6e9 微秒的域与宽度为 1 的域开销相同，绝不按微秒展开世界。
- 若多个向量达到同一极值，取“按纪元 ID 排序后的整数 offset 向量”字典序最小者；每个见证是**单一完整世界**，不为各 pair 拼接不同世界。`min == max` 时也分别提供合法见证。

### 见证内容

每个极值见证含：

1. `epoch_offsets_us`：**全部纪元**的 offset；
2. `occurring_pairs`：该世界中该组实际发生的 pair；
3. `observed_plan`：一份完整可运行的观测计划——保留原 `stations`，仅包含已有观测的 transmissions，沿用原 ID 与收发端点，时间替换为该向量换算的实测 UTC 时间、频段替换为实测值。

把见证里的 `observed_plan` 交给原 `spectrum.run`（即 `radio-coordinate` 无观测路径）即可核对该组在该世界中的实际冲突数。时间精确到微秒并以稳定 UTC ISO-8601 输出；频率保持原模型精确语义（半 Hz 用 Fraction 字符串）。

## 报告结构

观测模式报告在原报告字段外增加：

| 键 | 内容 |
| --- | --- |
| `mode` | 固定 `"execution_review"` |
| `plan_report` | 未改动的原 `spectrum.service.run` 完整报告 |
| `review.status` | `reviewed` / `inconsistent_clocks` |
| `review.clock_review` | 每纪元联合可达 min/max offset、UTC 换算口径与零 tick 边界 UTC；矛盾时为 `null` |
| `review.execution_review` | 逐对有向复核行 + `unobserved_transmission_ids`；矛盾时为 `null` |
| `review.group_review` | 每组极值与两个完整见证；矛盾时为 `null` |
| `summary` | 状态、计划/已观测任务数、原计划有向冲突数 |

## 确定性与边界

输出只取决于业务值：输入数组重排、等价时区写法不改变报告；纪元按 ID 排序，复核行按任务 ID 排序，见证按字典序平局规则唯一确定。保护圈/大圆距离/半开时间/半 Hz 频段仍是使用者声明的筛查模型，本复核不声明真实无线电许可或传播安全。

## 样例与测试

见 `fixtures/observations/README.md` 的原创离线合成样例与 `tests/test_observations.py`。后者含一个**独立整数穷举预言机**（不导入求解器内部逻辑，仅依据本契约文本逐微秒枚举全部可行 offset 向量），在大量小范围实例上对照分类、联合极值与完整见证，并含宽 offset 域用例验证实现不按微秒展开。
