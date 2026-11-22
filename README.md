# data-qd-180 · 无线电频率协调

对已定发射计划，按接收站保护圈、半开时间和频段交集，产生有方向的冲突证据。

## 独立安装与测试

Python >=3.11，本批实测3.12.14。运行与unittest仅用标准库，构建工具精确固定。每仓独立venv，可复用公共下载缓存。

```sh
uv venv --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
```

以上离线命令需要缓存已有setuptools==80.9.0和wheel==0.45.1；无缓存时先准备这两个公共构建工具，不声称空缓存可离线安装。修改源码后重装本仓。

## 端到端演示

在仓库根目录执行，输出路径必须不存在。重复演示请换新路径。

```sh
.venv/bin/radio-coordinate --source fixtures/input.json --output run/report.json
```

预期：3次发射、1条有向冲突、1个受影响接收任务；频率交集5000Hz。

输入或业务规则无效、输出已存在时退出2，保留原目标；成功退出0。报告/数据库先完整生成再原子新建，无覆盖。run/为忽略目录。

## 活动后执行观测复核

`radio-coordinate` 增加可选参数 `--observations`：现场设备日志按各自重启纪元的计时器记录，校时只有带误差范围的证据。复核在**同一套全局 offset 取值**上穷尽判断实际有向冲突是 `certain`/`possible`/`absent`/`unobserved`，并对每组冲突给出同一时钟世界下的最少/最多发生数及可由原 `spectrum.service.run` 核对的完整见证。时钟证据联立矛盾时退出 0 并发布 `inconsistent_clocks`，保留原计划报告；契约错误仍退出 2、不发布报告。不传 `--observations` 时输出与失败语义保持原样。

```sh
.venv/bin/radio-coordinate --source fixtures/observations/rtu-plan.json \
  --observations fixtures/observations/rtu-obs-exclusion.json \
  --output run/rtu-review.json
```

公开纯函数 `spectrum.observations.review_executions` / `build_review_report` 可独立测试。详见[执行观测复核契约](docs/observations.md)与[合成样例](fixtures/observations/README.md)。原工作台、编排器接口与 `spectrum.service.run` 契约不变。

## 计划变更工作台

`radio-workbench` 在一份本地独立台账上保存固定站点、修订式发射计划和不可变提案，可跨进程/重启继续，两个命令进程相遇时逐个生效。原 `radio-coordinate` 与 `spectrum.service.run` 行为不变。

```sh
.venv/bin/radio-workbench init    --ledger run/ledger.json --source fixtures/workbench/plan-v0.json
.venv/bin/radio-workbench propose --ledger run/ledger.json --source fixtures/workbench/proposal-a.json
.venv/bin/radio-workbench preview --ledger run/ledger.json --proposal proposal-a
```

子命令：`init`、`propose`、`preview`、`approve`、`apply`、`show`；成功向 stdout 输出一个 JSON 对象，错误退出 2 并在 stderr 给出可区分原因。详见[工作台契约](docs/workbench.md)与[合成样例](fixtures/workbench/README.md)。

## 离线频道编排

`radio-arrange` 把待安排接收任务与不可变固定发射编排成一份**建议**：每个 request 是否选中、选中时用哪个频道，以及每台接收设备的调谐日程。只生成建议，**不创建提案、不取得接收确认、不修改台账**；原有两个命令和公开接口保持兼容。无外部求解器，对最多 14 个 request 做带剪枝的穷举，按“utility 总和 → 换频次数 → 分配向量”的字典序求全局最优。

```sh
.venv/bin/radio-arrange --source fixtures/arrange/abc.json --output run/abc-report.json
```

业务无解时仍退出 0 并发布 `infeasible` 报告；输入契约错误或输出已存在退出 2，不发布报告、不覆盖已有目标。详见[编排契约](docs/arrange.md)与[合成样例](fixtures/arrange/README.md)。

## 契约与边界

详见[数据契约](docs/contract.md)。保护圈为输入声明的筛查模型，不计算功率、天线或地形传播；不作真实无线电许可或安全判断。

## 接收保护日报重算与更正

`radio-protection` 按原发布与本次重算两个资料截止时刻，各自生成同一份计划的接收保护日报，并对照出“哪一段的保护结论需要更正”的清单。台站坐标与保护半径一律由台站资料决定，绝不用计划旧坐标代填；资料缺失或同级争议的时段标为未决。

```sh
.venv/bin/radio-protection --source fixtures/protection/plan.json \
  --station-facts fixtures/protection/recompute-facts.json \
  --output run/protection-recompute.json
```

资料缺失、有争议或没有更正都是有效结果，退出 0；契约错误退出 2，不发布报告、不覆盖已有目标。详见[重算与更正契约](docs/protection.md)与[合成样例](fixtures/protection/README.md)。原 `radio-coordinate`（含 `--observations`）、`spectrum.service.run`、工作台与编排器用法不变。

## 活动后计划损失结算草案

`radio-settle` 按确认的固定计划与结算条款，起草频道履约补偿：对每份承诺把落在承诺区域内的受干扰时频块按「恰好覆盖它的责任经营方集合」分组精确计量（1 微秒 × 0.5 Hz 为一单位），再在经营方额度约束下按「全场总量 → 各承诺实付 → 各承诺内经营方发票」三层字典序最优给出补偿分配，并附逐承诺、逐经营方的对账合计。这里结算的是约定模型下的计划损失，不是对现场实测或实际传播的认定；命令不改计划、不执行付款。

```sh
.venv/bin/radio-settle --source fixtures/settle/plan.json \
  --terms fixtures/settle/terms.json --output run/settle-report.json
```

零损失、零额度、没有可补偿责任方或没有承诺都是正常结果，退出 0；无效输入或已有目标退出 2，沿用原子新建且不覆盖约定。详见[结算契约](docs/settle.md)与[合成样例](fixtures/settle/README.md)。原 `radio-coordinate`（含 `--observations`）、`spectrum.service.run`、工作台、编排器与 `radio-protection` 用法不变。

样例均为本仓原创合成数据，来源见[说明](fixtures/SOURCES.md)，代码/样例MIT许可。业务演示和测试不访问网络，不需要容器或外部服务。

## 可公开接收统计公告

`radio-bulletin` 在同一版冻结计划上，把与统计窗口正交的发射按接收站场馆与中心频率频道组归入唯一单元格，计量整数微秒交集，再按明确的**公众所知模型**审查每个统计口径（单格、整行、整列、全表合计）：只有当公开该口径的**每一种仍可能取值**后，每个受保护格都保有规定 `gap_us` 的取值跨度时才准许，否则整格扣留。准入决定本身也是公开信息，因此真实值安全但某个可能值不安全时照样扣留。判定用下界环流（确定性 Dinic 最大流，完全幺模保证整数可行）加 O(log U) 二分端点，全程整数、不逐微秒/逐可能值/逐矩阵枚举。公告带版本与版次、按请求顺序列明 `published`/`granted`/`withheld`，累计 `publications` 与 `prior` 可直接作为下次审查输入。

```sh
.venv/bin/radio-bulletin --source fixtures/bulletin/plan.json \
  --rules fixtures/bulletin/rules-n-then-h.json --output run/bulletin.json
```

无效输入、历史与冻结计划不符或历史已泄露时退出 2，保留已有目标；有扣留仍是正常结果（退出 0），沿用原子新建、不得覆盖。详见[接收统计公告契约](docs/bulletin.md)与[合成样例](fixtures/bulletin/README.md)。原有各命令用法与公共接口不变。
