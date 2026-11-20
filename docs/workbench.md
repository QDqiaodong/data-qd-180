# 计划变更工作台 `radio-workbench` v1

面向活动频率协调员：在**一份本地台账**上保存固定站点、修订式发射计划和不可变提案，支持关闭程序后继续、两个命令进程相遇时逐个生效。只使用 Python 标准库；不联网、不登录、不依赖后台服务或共享文件系统。

冲突模型完全复用 `radio-coordinate` 的原模型（`spectrum.model` / `spectrum.interference` / `spectrum.service`）：有向保护圈、半开时间、半 Hz 频段、UTC 归一、距离三位小数。工作台不把有向冲突合并为无向边，也不计算真实传播安全。

## 安装与入口

`pyproject.toml` 同时提供原命令与 `radio-workbench = "spectrum.workbench_cli:main"`。未安装时可在源码树运行：

```sh
PYTHONPATH=src python3 -m spectrum.workbench_cli <子命令> ...
```

所有子命令成功时在 stdout 输出**恰好一个 JSON 对象**并退出 0。失败时退出 2，stderr 输出一个 JSON 对象：

```json
{"error": "<可区分原因>", "message": "<说明>", ...}
```

失败不改变台账（写入失败时只能看到完整旧状态）。错误码：

| error | 含义 |
| --- | --- |
| `invalid_input` | 参数/输入 JSON 或字段不合法（含参数解析错误） |
| `ledger_exists` | `init` 目标已存在 |
| `ledger_not_found` | 台账不存在 |
| `ledger_io_error` | 台账读/写/锁失败（如磁盘错误、权限） |
| `ledger_corrupt` | 台账不是可读的完整本格式 |
| `revision_not_found` | `--revision` 或 `base_revision` 超出已有修订 |
| `proposal_not_found` | 提案 ID 不存在于本台账 |
| `proposal_conflict` | 提案 ID 已用于不同内容 |
| `write_conflict` | 涉改 ID 的当前值与提案固定的旧前提不同 |
| `invalid_candidate` | 候选整份计划不满足原契约（如超 300 次发射、7 天、坐标等） |
| `approval_required` | 当前仍需接收站确认（`missing` 给出 receiver 与新身份） |
| `approval_not_required` | 该接收站此刻无需确认 |
| `review_mismatch` | 传入 `--review-id` 不是此刻应审身份（`expected` 给出正确身份） |
| `proposal_already_applied` | 已提交提案不再接受新的 `approve` |

## 数据契约

### 发射记录（严格七字段）

`id`、`station_id`、`receiver_id`、`center_hz`、`bandwidth_hz`、`start`、`end`。

- 多余或缺失字段拒绝；字段值必须是对象。
- 三个 ID 为非空字符串。**不去空格、不改大小写**，按原样比较与保存（仅全空白视为非法）。
- `center_hz`、`bandwidth_hz` 必须是 JSON 整数（`bool` 不算整数），`center_hz >= 1`、`bandwidth_hz >= 1`，且带宽不得使占用频段跨过 0Hz（沿用原契约 `bandwidth_hz <= 2*center_hz`）。
- `start`/`end` 为带显式偏移的 ISO-8601 时间；保存和比较前统一转为 UTC。`2026-08-01T10:00:00+00:00` 与 `2026-08-01T12:00:00+02:00` 是同一业务值。正时长且不超过 7 天。
- 业务身份是七字段的**规范值**：时间 UTC、整数原值、ID 原字符串。

### 计划

`stations` 沿用原输入（唯一 `id`、有限 lat/lon、正整数 `protection_radius_m`），每个台账**只接受一次**并在 `init` 时固定；后续修订只改发射计划。计划按 ID 排序保存，数组排列不影响任何身份。整份计划每次都要通过原模型校验（1..500 站、≤300 次发射、引用存在、收发不同、时长/频段限制等）。

### 提案（不可变）

```json
{
  "proposal_id": "proposal-a",
  "base_revision": 0,
  "upserts": [ {"id": "v", "...": "完整七字段记录"} ],
  "remove_ids": ["old-id"]
}
```

- `proposal_id` 非空字符串；`base_revision` 非负整数（`bool` 不合法），且必须是本台账**已有修订**。
- `upserts` 是**完整**发射记录数组（不是补丁）；`remove_ids` 是发射 ID 数组。
- 两组 ID 各自不可重复，不可交叉；`remove_ids` 必须存在于基线；单条 upsert 不得与基线同 ID 记录语义相同；两组不能同时为空。
- **提案身份**：`base_revision` + 排序后的 upserts 规范值 + 排序后的 remove_ids。相同 ID 与相同身份重送成功且 `created=false`、不产生第二份；相同 ID 不同身份为 `proposal_conflict`。时间偏移写法或数组排列不同不算不同内容。
- 创建时把所有操作**同时**作用于 `base_revision`，整份候选计划必须有效；否则 `invalid_candidate`，不登记提案。
- 创建时固定每个涉及 ID 在 `base_revision` 的旧业务值作为前提；基线不存在该 ID 记为 `null`（“原来不存在”也是一种前提）。

### 修订与提交

- `init` 建立修订 0（源计划）。每次成功 `apply` **恰好**新增一个修订，编号连续（0,1,2,…）。
- 修订保存完整计划、该计划的原模型报告（`spectrum.service.run` 的输出，含 `conflicts`/`clear_transmission_ids`/`summary`）与来源提案 ID。
- `submitted` 标记、新修订、固定结果与提案在同一份台账 JSON 里一次原子替换，因此不会出现“计划已变但提案未提交”或“提案已提交但修订不可读”。

## 命令

### init

```sh
radio-workbench init --ledger PATH --source PLAN.json
```

用原 stations/transmissions 输入建立修订 0。源计划须满足原契约（工作台额外要求发射记录只有七字段）。目标已存在（含悬空符号链接）→ `ledger_exists`。成功输出 `revision=0`、`current_revision=0`、`ledger_id`、`summary`。

### propose

```sh
radio-workbench propose --ledger PATH --source CHANGE.json
```

登记不可变提案（校验如上）。成功输出 `proposal_id`、`base_revision`、`created`（重送为 `false`）、`upsert_ids`、`remove_ids`。

### preview

```sh
radio-workbench preview --ledger PATH --proposal ID
```

**总是针对当前已提交计划**重新推演，不推进修订。输出：

- `current_revision`：本次推演所依据的当前修订。
- `base_revision`：提案登记时的基线（仅信息）。
- `status`：`mergeable` / `approval_required` / `write_conflict` / `invalid_candidate`（已提交提案为 `already_applied`）。
- `can_merge`：`status == "mergeable"`。
- `write_conflicts`：前提不符的 ID 列表，每项含 `transmission_id`、`expected`、`current`。
- `validation_error`：候选无效的原因（否则 `null`）。
- `candidate_report`：**候选完整冲突报告**（与 `show`/`apply` 的报告同结构），前提冲突或候选无效时为 `null`。
- `reviews`：逐接收站的待确认事项。每项 `receiver_id`、非空 `review_id`、`evidence`（该站**完整候选证据行**）、`approved`（当前保存的确认是否恰好满足此刻身份）。

状态优先级：`write_conflict` → `invalid_candidate` → `approval_required` / `mergeable`。确认判断永远在前提与有效性之后。

### approve

```sh
radio-workbench approve --ledger PATH --proposal ID --receiver ID --review-id VALUE
```

在**实际执行的当前修订**上重新推演后记录确认；这是本地人工声明，不需要登录、签名或联网。拒绝（均不改变台账）且退出 2 的情形：

- 提案已提交 → `proposal_already_applied`
- 写冲突 → `write_conflict`
- 候选无效 → `invalid_candidate`
- 该站此刻无需确认（候选集合为空，或与当前集合完全相同）→ `approval_not_required`
- `VALUE` 不是此刻应审身份 → `review_mismatch`（附 `expected`）

相同有效确认重复登记成功（幂等）。成功输出 `receiver_id`、`review_id`、当时的 `revision`、`approved=true` 与 `evidence`。

### apply

```sh
radio-workbench apply --ledger PATH --proposal ID
```

在同一把锁内以**同一份当前修订**完成：前提检查 → 将操作同时作用到**当前计划**（不是还原到基线）→ 整份重新校验 → 重新计算所需确认并与保存的确认核对 → 发布一个新修订。任何一步失败都不留部分提交，当前修订不变。

- 前提不符 → `write_conflict`（不得覆盖当前值；其他提案已提交的不同 ID 原样保留）。
- 合并后超限/无效 → `invalid_candidate`（创建时有效不代表日后合并仍有效）。
- 缺任何当前所需确认 → `approval_required`（附 `missing`）。
- 两次改动后恢复到与基线语义相同的值视为前提满足。
- 成功输出 `revision`（新修订号）、`base_revision`、`report`（完整冲突报告）与本次采用的 `reviews`。
- **首次成功结果被固定**：以后重送返回完全相同的那份结果（同 `revision`、同 `report`），不重做前提/确认检查，不生成新修订，不回退当前计划——即使当前计划已经变化。这保证“实际已提交但 stdout 未送达”时重试可取回首次结果。已提交提案不再接受新的 `approve`。

### show

```sh
radio-workbench show --ledger PATH [--revision N]
```

返回指定已提交修订的完整计划与原模型冲突报告；不给修订号取当前修订。输出 `revision`、`current_revision`、`is_current`、`source_proposal_id`（修订 0 为 `null`）以区分修订身份，以及 `plan`、`report`。未知修订 → `revision_not_found`。

## 确认口径（业务规则）

对每个 `receiver_id`：

1. 分别用当前已提交计划与候选计划跑原模型，收集该站作为受害接收站的**全部冲突行**（有向）。
2. 候选集合**非空且与当前集合不同** → 该站需确认，需确认的是**完整候选证据集合**。
3. 候选为空（该站所有已知冲突解除）→ 不需确认。
4. 集合完全不变 → 不需确认。
5. 候选集合缩小但仍非空 → 仍需确认剩下的完整证据。

实现上不做二维风险增量或真实传播计算；只比较完整证据集合的业务身份。

### 证据行身份

证据行取自原冲突报告的全部字段：

`victim_transmission_id`、`interferer_transmission_id`、`receiver_id`、`interferer_station_id`、`overlap_start`、`overlap_end`、`overlap_low_hz`、`overlap_high_hz`、`overlap_bandwidth_hz`、`separation_m`。

规范化：时间转 UTC ISO-8601；频率用 Fraction 的最简字符串（如 `197/2`）；`separation_m` 按原报告三位小数口径格式化为字符串（`"222.390"`）。行的集合身份与行顺序无关。

`review_id` 的计算（v1）：

```text
payload = {
  "version": 1,
  "ledger_id": <台账随机 UUID>,
  "proposal_id": <提案>,
  "receiver_id": <接收站>,
  "evidence": [该站完整候选证据行，按规范 JSON 排序]
}
review_id = "review-" + sha256(UTF-8(canonical_json(payload))).hexdigest()
```

`canonical_json` 使用 `sort_keys=True`、`separators=(",", ":")`、`ensure_ascii=False`。身份刻意**不含**修订号、其他接收站、无关计划字段或冲突计数，因此：同一台账、同一提案、同一接收站、同一证据必定同身份；任何证据字段变化必定不同身份；无关修订推进不会迫使重新确认。

确认归属于该提案（不能从另一提案或另一台账借用）。保存的身份可保留：后续提交时只要当前所需证据与已确认身份完全相同即可复用；证据曾变又恢复原样，原确认也可复用。

## 跨进程与持久化

- 台账是单个 JSON 文件，格式 `{"format": "radio-workbench-ledger", "version": 1, "ledger_id": …, "stations": …, "revisions": […], "proposals": {…}, "approvals": {…}}`，以稳定键序保存。
- 旁路锁文件 `<ledger>.lock` 使用 POSIX `flock` 排他锁。每个命令在整段读-改-写期间持锁，因此同一台账上的两个命令进程表现为**逐个生效**：不丢确认、不产生重复修订号、不让两份违反前提的提案同时覆盖同一发射。
- 写入采用：同目录临时文件 → `fsync` → `os.replace` 原子替换 → `fsync` 目录。读取在锁内进行，只会看到完整旧状态或完整新状态。被中断的写入只留下 `.ledger.json.tmp-*` 临时文件，下次写入在锁内清扫；台账本身保持旧状态。
- 历史修订、不可变提案、成功确认、提交标记都只在台账里，进程重启后继续可用。
- 只需本地文件系统；不要求多机共享文件系统或后台服务。不适配网络文件系统的 `flock` 语义。

## 兼容性

原 `radio-coordinate --source … --output …` 与 `spectrum.service.run` 的行为、输出、退出码与原子新建语义保持不变；原测试继续通过。工作台只新增模块（`errors`、`records`、`ledger`、`workbench`、`workbench_cli`），不修改原模型的冲突计算。

## 样例

见 `fixtures/workbench/README.md`。全部为原创合成数据，离线可运行。
