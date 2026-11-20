# 城市公厕服务履约采录台

本地虚构的公厕服务证据工程。设施开放与故障、承包商班次履约、公众投诉补救是三套独立账本。设施停水会暂停保洁班次裁定，设施故障会重开先前关闭的投诉；每次裁定与原始输入持久化。没有真实投诉者、位置或外部服务。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m toiletservice demo --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：`python -m toiletservice facility-import --db state.sqlite --file fixtures/facility-ok.json`；`shift-import` 与 `complaint-import` 同法。各有 `*-bad.json` 反例。`report` 命令返回设施可用厕位、班次状态、投诉与补救、证据 SHA 和裁定日志；`audit` 从原始文件字节在全新内存库重放，并与当前状态逐表比对。边界是离线承包服务核验，不对接真实传感器或公共投诉平台。
