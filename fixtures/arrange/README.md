# radio-arrange 离线合成样例

本目录的站点、发射与设备数据均为原创合成值（MIT 许可），离线可运行，不对应真实台站。地理坐标与转换耗时只用于演示声明式筛查与设备调谐规则，不代表真实传播或许可。

## 共同背景：A/B/C 三任务（`abc*.json`）

接收站 `R`=(0,0)，保护圈 1000m，10:00Z 在 **100Hz** 就绪；发射端 `TA`/`TB`/`TC` 均在远处（无相互无线电冲突）。全局频道 100/200/300Hz，转换表声明：

- `100→200` = `200→300` = 600 秒；
- 其余不同频道的有向转换默认 600 秒；
- `100→300` 在 `abc.json` / `abc-infeasible.json` 中为 **3600 秒**，在 `abc-short-switch.json` 中改为 **1800 秒**。

| request | 频道 | 时段 (UTC) | required | utility |
| --- | --- | --- | --- | --- |
| A | 仅 100Hz | 10:00–10:10 | 是 | 100 |
| B | 仅 200Hz | 10:20–10:30 | 否 | 50 |
| C | 仅 300Hz | 10:40–10:50 | 是 | 100 |

### `abc.json` —— 可舍弃任务反而让必选任务可执行

B 作为中间接收任务，把 A→C 拆成相邻的 `100→200`、`200→300` 两次 600 秒转换，恰好填满两个 10 分钟空隙。结果 `optimal`，分配向量 `[100, 200, 300]`，utility 250，换频 2 次。**去掉 B 后** A、C 变成相邻任务，需在 30 分钟空隙内完成声明的 `100→300`=3600 秒直接转换，无解——工具不会对非相邻的 A/C 强加直接转换，也不会插入没有接收任务的“幻影调谐”。

### `abc-infeasible.json` —— 固定干扰使 B 不可安排，连带必选集无解

新增固定发射 `F`：发射端 `TI` 位于 R 的保护圈内，接收端为远处的 `U`（其自身接收站远离 A/B/C 的发射端）。F 仅在 **200Hz、10:20–10:30** 与 B 的时段/频道内对 R 形成有向冲突，B 因此无法安排。剩下 A、C 相邻又无法完成 3600 秒直接转换：报告 `infeasible`（退出 0），`plan=null`，不发布任何部分安排；`conflict_report` 仍给出仅固定发射计划的完整证据。该例说明频率筛查与设备准备不能各自求完再拼接。

### `abc-short-switch.json` —— 仅改一个表值后的对照

与上一文件唯一差别是 `100→300` 改为 **1800 秒**。B 仍被 F 阻挡，但 A→C 的 30 分钟空隙现在容纳得下 1800 秒直接转换：结果 `optimal`，分配向量 `[100, null, 300]`，utility 200，换频 1 次（A 结束后调谐，紧接 C 开始前完成）。

## 其他样例

- `empty-requests.json`：`requests` 为空。固定发射 `f0` 与设备初始频道相同，结果 `optimal`、零 request、零换频，`plan` 只含固定发射。
- `fixed-fixed-conflict.json`：两条固定发射在**不同接收站**上互为有向冲突（保护圈相互覆盖）。设备排程可行，冲突被原样保留在 `conflict_report`（2 条有向冲突），编排仍为 `optimal`，不因为既有固定间冲突而失败。

## 演示

输出路径必须不存在；重复演示请换新路径（`run/` 已被忽略）。

```sh
radio-arrange --source fixtures/arrange/abc.json                 --output run/abc-report.json
radio-arrange --source fixtures/arrange/abc-infeasible.json      --output run/infeasible-report.json   # 退出 0
radio-arrange --source fixtures/arrange/abc-short-switch.json    --output run/short-report.json
radio-arrange --source fixtures/arrange/empty-requests.json      --output run/empty-report.json
radio-arrange --source fixtures/arrange/fixed-fixed-conflict.json --output run/fixed-conflict-report.json
```

源码树内可直接 `PYTHONPATH=src python3 -m spectrum.arrange_cli ...`。`optimal` 报告的 `plan` 是完整 `stations`/`transmissions` 对象，可直接作为原 `radio-coordinate` 的输入（先取出 `plan` 存为文件再运行即可），其输出应与报告里的 `conflict_report` 一致。
