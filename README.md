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

## 计划变更工作台

`radio-workbench` 在一份本地独立台账上保存固定站点、修订式发射计划和不可变提案，可跨进程/重启继续，两个命令进程相遇时逐个生效。原 `radio-coordinate` 与 `spectrum.service.run` 行为不变。

```sh
.venv/bin/radio-workbench init    --ledger run/ledger.json --source fixtures/workbench/plan-v0.json
.venv/bin/radio-workbench propose --ledger run/ledger.json --source fixtures/workbench/proposal-a.json
.venv/bin/radio-workbench preview --ledger run/ledger.json --proposal proposal-a
```

子命令：`init`、`propose`、`preview`、`approve`、`apply`、`show`；成功向 stdout 输出一个 JSON 对象，错误退出 2 并在 stderr 给出可区分原因。详见[工作台契约](docs/workbench.md)与[合成样例](fixtures/workbench/README.md)。

## 契约与边界

详见[数据契约](docs/contract.md)。保护圈为输入声明的筛查模型，不计算功率、天线或地形传播；不作真实无线电许可或安全判断。

样例均为本仓原创合成数据，来源见[说明](fixtures/SOURCES.md)，代码/样例MIT许可。业务演示和测试不访问网络，不需要容器或外部服务。
