# 接收保护日报重算与更正 `radio-protection` v1

面向活动频率协调员：活动期间临时台站会搬迁，接收站保护半径会复测。测绘单位和现场值班单位**各自报送**台站资料，后到的修订有时改变的是昨天的事实。本命令在同一份发射计划上重建**两个发布视图**：

- `published`：按原发布时刻 `published_as_of` 已知的台站资料；
- `revised`：按本次重算时刻 `revised_as_of` 已知的台站资料。

两者给出同一份计划时段的接收保护日报，并对照出“哪一段的保护结论需要更正”的清单。若台站资料互相矛盾或尚未报齐，**绝不沿用旧计划里的坐标**给出无干扰结论，而是把该段列为 `undetermined`。

时间、频段与几何完全沿用原模型（`spectrum.model` / `spectrum.interference` / `spectrum.service`）：有向保护圈、大圆距离（地球半径 6371008.8m）、UTC 半开区间、精确半 Hz 频段、距离三位小数。只使用 Python 标准库；离线运行，不联网、不依赖外部服务，也**不按秒或微秒展开窗口**。不声称测到了实际干扰，也不作真实无线电许可或传播安全判断。

## 入口与退出约定

```sh
radio-protection --source PLAN.json --station-facts FACTS.json --output REPORT.json
```

未安装时可在源码树运行 `PYTHONPATH=src python3 -m spectrum.protection_cli ...`。

- 成功（包括资料缺失、站点有争议、更正清单为空）：退出 0，stdout 打印报告 `summary`，报告经临时文件**原子新建**于 `--output`。
- 输入契约错误（任一文件格式错误、悬空引用、重复键、非法时段、无效坐标/半径、布尔值冒充整数、超限）：退出 2，stderr 一行 `radio-protection: <原因>`，**不发布报告**；目标已存在同样退出 2 并保留原目标，绝不覆盖，也不留下半份报告。
- 两份输入各自沿用 **8 MB** 上限。
- 原 `radio-coordinate`（含 `--observations`）、`spectrum.service.run`、`radio-workbench`、`radio-arrange` 的用法与契约均不变。

公开纯函数 `spectrum.protection.build_protection_report(plan, facts)` 不读写文件，可独立测试；契约违例抛 `ValueError`。

## 计划输入

沿用原数据契约（见[数据契约](contract.md)）：`stations` 1..500 个、`transmissions` ≤300 条，端点、时间与频率整体按原口径校验。计划的**站点 ID 是台站身份清单**；计划里的坐标和保护半径只用于通过原契约，**保护结论一律由本次台站资料决定**，不把计划坐标当作任何视图的资料。

## 台站资料契约（根对象恰含六键）

```
window_start, window_end, published_as_of, revised_as_of, sources, events
```

- `window_start`、`window_end`：要重算的半开时段 `[start, end)`，长度 >0 且 ≤7 天；报告只覆盖该时段与任务时段的交集。
- `published_as_of`、`revised_as_of`：原发布与重算各自认知资料的截止时刻，必须 `published_as_of < revised_as_of`。两者含义不同：今天才收到、覆盖昨天上午的更正进入 `revised`，不改变 `published`。
- 所有时间必须带明确偏移；小数秒最多六位；按 UTC 比较。等价偏移写法不改变语义。

### sources（报送来源）

每项恰含 `id`、非负整数 `priority`；ID 唯一，`priority` 数值越小越权威，允许并列。两个视图使用**同一套**来源优先级。

### events（≤20,000 条）

每项恰含 `source_id`、`station_id`、`fact_id`、`known_at`、`op`：

- `source_id` 必须引用存在的来源；`station_id` 必须引用计划台站；三者共同标识一份资料。
- 每份资料在同一 UTC `known_at` 最多有一个版本（不同偏移写法的同一 UTC 视为同一时刻）。
- `op = "assert"` 时另含 `effective_start`、`effective_end`、`lat`、`lon`、`protection_radius_m`；有效半开时段须有正长度；坐标与半径沿用原验证口径。
- `op = "withdraw"` 时**不带**上述字段；它撤销整份资料，而非只撤销生效于该时刻之后的部分。
- 资料必须从声明（`assert`）开始；按认知时间 `known_at` 排列时，只有尚未撤回的资料可以撤回；撤回后可以再次声明。
- 数组顺序不改变语义；ID 保留原始字符串（非空，不去空格、不折大小写）。
- 整份文档都严格校验，**包括晚于重算截止的记录**。晚于两个截止时刻的有效记录不影响报告。

## 每个发布视图的计算

1. 对每份资料取 `known_at ≤ 截止时刻` 的最新版本。该版本若为 `withdraw`，这份资料在此视图没有有效声明；若 `assert`，用它的有效时段。**被取代版本的尾段不能继续使用**（缩短或移动有效时段会一并撤销旧尾段）。
2. 在窗口内按台站和实际生效时段比较所有覆盖该时刻的有效声明：
   - 无声明 ⇒ `missing`；
   - 只取**最权威的一层**（`priority` 最小者）：该层给出的（坐标, 半径）数值集合唯一 ⇒ `resolved`，保留这一层的**全部**资料版本作为依据；
   - 该层数值不一致 ⇒ `disputed`，保留争议的**全部**版本及各自数值；不按接收先后裁决，也不降级到较低优先级。
3. 每个计划台站都有一条覆盖整个窗口的连续时间线（即使它没有造成任何保护变化也可独立查到）。相邻且**状态、取值、依据三者均相同**的段合并。依据列出 `source_id`、`fact_id`、`known_at`、`priority` 和该版本的数值。

## 两份接收保护日报

对每对**不同**发射任务，仍以有向“受保护接收任务 / 另一项干扰发射任务”区分方向，仅当：

- 两任务与窗口有正长度时间交集；
- 占用频段有正宽度交集

才判断。此时取**受保护任务的接收站**和**干扰任务的发射站**在该段的台站资料，**不使用受保护任务自身发射站的资料替代**。两端都 `resolved` 时，按原大圆距离、接收站保护半径含边界、精确半 Hz 口径判定；任一端 `missing` 或 `disputed` 时该段这对任务为未决。没有正时间或正频段交集的任务对不产生未决证据。

`report.conflicts` 给出冲突的有向时段（两个任务 ID、起止、交集频段、三位小数距离）；`report.undetermined` 给出未决有向时段（两个任务 ID、起止、交集频段）以及所缺或有争议的端点 `endpoints`（角色、站点 ID、状态）。相邻且证据字段一致的时段合并。

### 每任务接收时间线

`report.reception_timelines` 按任务给出窗口内完整接收时间线。对任务与窗口的交集：

- 任一有向干扰确定发生 ⇒ `affected`；
- 否则任一干扰未决 ⇒ `undetermined`；
- 否则 ⇒ `clear`。

每段保留完整的**确定干扰任务 ID 集合**与**未决任务 ID 集合**（即使已经 `affected` 也保留未决集合）；仅当状态和两个集合均相同才合并邻段。`durations_us` 按三种状态分别汇总为整数微秒，重叠干扰不重复计时。任务完全在窗口外时时间线为空、三种时长均为零。

## 更正清单

`corrections` 逐接收任务对照 `published`/`revised`，列出**状态或任一干扰 ID 集合**有变化的确切子区间及变更前后的值。仅距离、资料来源或未决缘由变化而状态和两个集合都相同，不单独列更正（这些细节仍可从两份日报核对）。合并相邻且前后值各自相同的更正；不按总时长增减猜测明细，也不把变化横跨到未变时间。

## 报告结构

| 键 | 内容 |
| --- | --- |
| `schema_version` / `tool` / `model` / `mode` | `1` / `radio-protection` / 原模型标识 / `station_facts_review` |
| `window` | `{start, end}`，UTC ISO-8601 |
| `published_as_of` / `revised_as_of` | 两个截止时刻，UTC |
| `views.published` / `views.revised` | 各自 `as_of`、`stations`（每站 `station_id` + `segments`）、`report` |
| `views.*.stations[].segments[]` | `start`、`end`、`status`、`values`、`evidence` |
| `views.*.report.conflicts[]` | 有向冲突时段与证据 |
| `views.*.report.undetermined[]` | 有向未决时段与端点状态 |
| `views.*.report.reception_timelines[]` | 每任务 `transmission_id`、`segments`、`durations_us` |
| `corrections[]` | `transmission_id`、`start`、`end`、`before`、`after`（各含 `status` 与两个 ID 集合） |
| `summary` | 站点/任务数、两个视图的冲突与未决计数、更正条数 |

所有数组按 ID（及时间）确定排序；时间一律 UTC ISO-8601。重排输入数组、改用等价偏移写法、或增加有效但晚于两个截止时刻的记录，报告逐字不变。

## 样例与测试

见[离线合成样例](fixtures/protection/README.md)。`tests/test_protection.py` 含一个**独立探针预言机**（只按本契约文本重推每站状态、每对有向关系与每任务接收状态，不导入 `spectrum.protection` 内部逻辑），在全部变化边界及其间中点对照报告；另含任务指定的 R/v/a/b 更正组、同级争议、撤回/缩短、确定与未决并存、同边界多项变化、仅版本变化、无候选但缺资料、输入错误、原命令兼容，以及接近上限且大量资料变化与活跃任务无关的用例。
