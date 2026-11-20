# 离线频道编排 `radio-arrange` v1

面向活动协调员：把**待安排接收任务（requests）**与**不可变固定发射（fixed_transmissions）**一起编排成一份**建议**——每个 request 是否选中、选中时用哪个频道，以及每台现场接收设备的调谐日程。工具只产出建议与日程：**不创建提案、不取得接收确认、不修改台账**。原 `radio-coordinate` 与 `radio-workbench` 的命令和公开接口保持不变。

冲突几何完全复用原模型（`spectrum.model` / `spectrum.interference` / `spectrum.service`）：接收站声明保护圈、大圆距离（地球半径 6371008.8m）、UTC 半开时间区间、精确半 Hz 频段、**有向**冲突。只使用 Python 标准库；离线运行，不联网、不依赖外部求解器或服务。

## 入口与退出约定

```sh
radio-arrange --source INPUT.json --output REPORT.json
```

未安装时可在源码树运行 `PYTHONPATH=src python3 -m spectrum.arrange_cli ...`。

- 成功（业务上无论 `optimal` 还是 `infeasible`）：退出 0，stdout 打印报告 `summary`，报告经临时文件原子**新建**于 `--output`；目标已存在则拒绝（退出 2）并保留原目标，绝不覆盖。
- 输入契约错误（悬空引用、非法时间、转换表不完整、超出上限、布尔值冒名整数等）：退出 2，stderr 一行 `radio-arrange: <原因>`，不发布报告。
- 业务无解（无法同时执行全部固定发射与 required request）不是输入错误：退出 0，发布 `infeasible` 报告。

## 输入契约

根对象恰好含五个键：`stations`、`fixed_transmissions`、`centers_hz`、`requests`、`receivers`。

### stations

沿用原数据契约：唯一 `id`、有限十进制度 `lat`/`lon`、正整数 `protection_radius_m`；1..500 个。

### centers_hz

全局可用中心频率：1..6 个**互不相同的正整数 Hz**。所有固定发射中心、request 的允许频道、设备初始频道与转换表频道都必须取自其中。

### fixed_transmissions（固定发射，不可改）

每项沿用原发射记录七字段（`id`、`station_id`、`receiver_id`、`center_hz`、`bandwidth_hz`、`start`、`end`），并额外满足：

- 中心必须在 `centers_hz` 内；最多 300 条，按原契约整体校验；
- 收发端点不同且都引用存在的站点；正时长且 ≤7 天；频段不得跨过 0Hz；
- 编排**必须全部保留**：不能改时段、频宽、端点或频道，不能丢弃。

### requests（待安排接收任务，最多 14 个）

每项含原发射记录除 `center_hz` 外的六个字段（`id`、`station_id`、`receiver_id`、`bandwidth_hz`、`start`、`end`），加上：

| 字段 | 约束 |
| --- | --- |
| `allowed_centers_hz` | `centers_hz` 的非空、不重复子集，1..3 个正整数 |
| `required` | 布尔；`true` 时该任务必须选中 |
| `utility` | 1..1000000 的整数（`bool` 不算整数） |

- request ID 全局唯一，且不得与任何固定发射 ID 重名。
- 时段、带宽固定；选中时只能从 `allowed_centers_hz` 选**一个**中心。
- 任一候选频道都会使占用频段跨过 0Hz 的 request 非法。
- 非 required 的 request 允许不安排（报告频道为 `null`，不附单一拒绝原因）。
- `requests` 为空数组合法（此时只校验固定发射与设备基线）。

所有整数字段拒绝 JSON 布尔值；所有站点/接收端/频道引用不得悬空。

### receivers（设备配置，恰好覆盖被使用的 receiver_id）

对固定发射和 request 中出现过的**每个** `receiver_id`，提供且仅提供一项：

| 字段 | 约束 |
| --- | --- |
| `id` | 引用一个存在的站点（一个接收站只有一台设备） |
| `ready_at` | 带显式偏移的 ISO-8601 时间，归一到 UTC |
| `initial_center_hz` | 属于 `centers_hz`；设备在 `ready_at` 已处于该频道 |
| `switches` | 转换表，见下 |

`switches` 是对象数组，每项恰好含 `from_hz`、`to_hz`、`seconds`：

- 必须**恰好覆盖全局频道间每一对不同频道的有向转换一次**：不缺、不重、不多余；同频道转换耗时固定为 0 且不出现在表中；
- `seconds` 为 0..3600 的整数；表由设备负责人声明，**可能不对称**，也不保证三角不等式。

## 设备可行性规则（每台接收设备独立）

把该设备的**全部固定发射接收任务**与**选中的 request 接收任务**按实际 UTC 时刻排列：

1. 一台设备每次只能执行一个接收任务，在任务的整个**半开区间** `[start, end)` 内被占用；同频道的两个时间重叠任务也不能同时接收。任务不能截短、移动或切片。
2. **第一项任务前**：从 `ready_at` 的初始频道转换到首项频道，必须有足够时间；等号允许（`ready_at + 转换耗时 ≤ 首项开始`）。
3. **后续只检查相邻两项实际接收任务**：前一项结束到后一项开始的空隙不得短于转换表中 `前频道→后频道` 的声明耗时；等号允许。
4. 存在中间接收任务时，只检查实际相邻转换，**不对非相邻任务另加直接转换约束**；不推导传递耗时；**不允许插入没有对应接收任务的中间调谐步骤**来绕过表值。
5. 调谐可以提前完成后等待。为使日程确定，报告把每次调谐安排在该任务开始前**紧接的 `seconds` 时间内**（初始转换同理）；相同频道不生成调谐动作。调谐间隔本身不是发射，不产生无线电冲突。

## 无线电规则

沿用原有向保护圈模型。对最终计划中的每一条有向冲突（受害接收 v、干扰发射 s）：

- 只要 v 或 s 中**任一来自选中的 request**，该计划就不允许——优化器不会选出这样的方案；
- 固定任务之间**原本已存在**的冲突予以保留，在报告的 `conflict_report` 中照常列明，**不使整次编排失败**；
- 不能把单向关系合并成无向对，也不能只检查新任务之间：固定→新、新→固定、新→新三个方向都筛查。

最终发射计划仍受原 **300 条发射上限**约束（固定 + 选中 request）。

## 优化口径（三层字典序，无权重混算）

在所有同时满足上述无线电与设备规则的完整计划中，依次比较：

1. **选中 request 的 utility 总和最大**；
2. 总和并列时，**全部接收站的换频次数之和最小**——包含初始频道到首项任务的转换，也包含固定任务导致的转换；同频道转换不计；
3. 仍并列时，把全部 request 按 ID 的**原始字符串顺序**排列，比较分配向量，取**字典序最小**者：选中项为其整数中心频率，未选项为 `null`；比较时任何整数都小于 `null`，整数按数值比较。

前一层目标严格优先于后一层。求解是**全局最优**：不按输入顺序贪心、不把单任务局部最优当全局结果、不用超时当前方案冒充最优。实现是对 ≤14 个 request 的有限分配空间做带剪枝的穷举回溯（每个 request 的选择为“不选”或其 ≤3 个允许频道之一），无外部求解器。

## 报告结构

### status = "optimal"

| 键 | 内容 |
| --- | --- |
| `schema_version` / `tool` / `model` | 报告版本、工具名、复用的模型标识 |
| `status` / `status_detail` | `optimal` 与口径说明 |
| `objective` | `selected_utility`、`total_switch_count`、`assignment_vector`（按 request ID 字符串序） |
| `requests` | 每个 request 的 `request_id`、`selected`、`center_hz`（未选为 `null`）、`required`、`utility`、`allowed_centers_hz` |
| `plan` | 完整最终 `stations` 与 `transmissions`（固定 + 选中 request 的完整七字段记录，按 ID 排序），**可直接作为原 `radio-coordinate` 的输入** |
| `conflict_report` | 在 `plan` 上运行原模型得到的**完整冲突报告**（含 `conflicts`、`clear_transmission_ids`、`summary`） |
| `receiver_schedules` | 每个接收站一项：`receiver_id`、`ready_at`、`initial_center_hz`、按时间排列的 `timeline` |
| `summary` | 状态、计数与目标值，含 `retained_fixed_conflicts` |

`timeline` 含两类条目，均带 UTC 时间：

- `switch`：`receiver_id`、`from_hz`、`to_hz`、`seconds`、`start`、`end`；
- `reception`：`transmission_id`、`source`（`fixed` 或 `request`，可追溯来源）、`station_id`、`receiver_id`、`center_hz`、`bandwidth_hz`、`start`、`end`。

### status = "infeasible"

当不存在能执行**全部固定发射与全部 required request** 的计划时（例如固定任务自身抢占同一设备、初始准备来不及、required 任务互相干扰），仍退出 0 并发布报告：`status="infeasible"`、明确的 `status_detail`、`plan=null`、`objective=null`、`receiver_schedules=null`；每个 request 的 `center_hz` 都是 `null`（不冒充部分安排，也不给未经证明的单一拒绝原因）。`conflict_report` 仍给出**仅固定发射**计划的完整原模型报告作为证据，`summary` 保留固定间冲突计数。

## 确定性

输出只取决于业务值：输入数组重排、等价的时区偏移写法不改变报告。时间统一 UTC ISO-8601；`stations`/`transmissions` 按 ID 排序；request 按 ID 原始字符串序排列；接收站日程按接收站 ID 排序、时间线按 UTC 时刻排序。

## 边界声明

保护圈、大圆距离、半开时间与半 Hz 频段都是**使用者声明的几何/设备筛查模型**：没有地形、功率、天线、接收选择性或真实传播预算，转换表也只是设备负责人的声明。本工具的“最优”仅指在这些声明规则与三层字典序目标下的最优，**不表示真实无线电许可或传播安全**。

## 样例与测试

见 `fixtures/arrange/README.md` 的离线合成样例与 `tests/test_arrange.py`。后者包含一个**独立穷举预言机**（不复用优化器内部逻辑，仅依据本契约文本重新枚举全部分配），在大量随机实例上对照全局最优，并覆盖任务书指定的全部情景。
