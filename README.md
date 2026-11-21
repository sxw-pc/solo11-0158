# 城市公厕服务履约采录台

本地虚构的公厕服务证据工程。设施开放与故障、承包商班次履约、公众投诉补救是三套独立账本。设施停水会暂停保洁班次裁定，设施故障会重开先前关闭的投诉；每次裁定与原始输入持久化。没有真实投诉者、位置或外部服务。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m toiletservice demo --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：`python -m toiletservice facility-import --db state.sqlite --file fixtures/facility-ok.json`；`shift-import` 与 `complaint-import` 同法。各有 `*-bad.json` 反例。`report` 命令返回设施可用厕位、班次状态、投诉与补救、证据 SHA 和裁定日志；`audit` 从原始文件字节在全新内存库重放，并与当前状态逐表比对。边界是离线承包服务核验，不对接真实传感器或公共投诉平台。

## 离线水表证据与故障归属（water-import）

两处设施共用一只总表时，总表多用的水量本身无法决定关闭哪处分支。`water-import` 只消费本地合成回执，在既有 `recompute`/`reconcile_shifts` 联动之前先做证据采录与归属核证：

- 覆盖版本：`calibration`（表计校准）、`meter`（总表/分表及其覆盖时段、校准期）、`baseline`（服务基线，可比较时段与单位对齐）、`pipeline`（共享管路与分支清单）。
- `anomaly` 打开总表异常案：基线合计 4、总表 7、超额 3 时，因两个分支都能解释总量，只保留两份 `compatible` 见证，不写任何水故障、不关闭设施。
- 接待时段只接收 `branch_read` 分表读取（不暂停服务）：读数必须落在接待时段、覆盖异常段且与总表处于同一校准期；两份分表按守恒关系合计等于总表且只有一个分支超基线时，才把水故障交给现有 `defect` 入口，`recompute` 关闭该设施并暂停其 `pending_patrol` 班次。守恒失败或两个分支都超量时输出 `shared_scope` 待查范围，不替两个设施分别制造故障。
- 维护窗口接收 `isolation_test` 模拟隔离试验回执：四份记录（空载起读、仅 A、仅 B、空载末读）必须引用同一实际试验 `test_ref` 与阶段版次 `stage_revision`，禁止跨试验拼曲线；同一校准、空载首末一致、现场关闭令未被撤销、且守恒到总表才有效。无效回执原样保留并要求重新核证，不签发故障。晚到的同试验新版次会让旧结论留痕改版（旧见证置 `superseded`、旧故障置 `withdrawn` 并关闭对应 defect），暂停历史仍可回查。
- `report` 的 `water_coverage` 逐设施给出证据覆盖、已签故障或两份相容解释，以及故障实际影响的班次；同一合成文件重复导入为 `file_replayed`，不产生第二条试验曲线。

验收夹具：`fixtures/water-*.json`，测试 `tests/test_water_witness.py`。

