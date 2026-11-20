# 城市公厕服务履约采录台

本地虚构的公厕服务证据工程。设施开放与故障、承包商班次履约、公众投诉补救是三套独立账本。设施停水会暂停保洁班次裁定，设施故障会重开先前关闭的投诉；每次裁定与原始输入持久化。没有真实投诉者、位置或外部服务。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m toiletservice demo --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：`python -m toiletservice facility-import --db state.sqlite --file fixtures/facility-ok.json`；`shift-import` 与 `complaint-import` 同法。各有 `*-bad.json` 反例。`report` 命令返回设施可用厕位、班次状态、投诉与补救、证据 SHA 和裁定日志；`audit` 从原始文件字节在全新内存库重放，并与当前状态逐表比对。边界是离线承包服务核验，不对接真实传感器或公共投诉平台。

此增强副本另带一个本地合成公告样页 `mock/notice.html` 和预定公告 `mock/notice-source.json`，原仓没有网页。样页在同一 `data-version=v2` 下经历四次客户端更新，视觉文案两次短暂显示“正常开放”，无障碍树始终保留“暂停开放”；公告源文件规定 v2 应显示“暂停开放”，声明的更新纪元为 1 至 4。`node scripts/capture-notice.mjs <输出目录>` 用本机 Edge 的无头浏览器和 CDP 在同一会话保存四帧截图、渲染 DOM、无障碍树及摘要清单；如 Edge 安装位置不同，可设置 `EDGE_BIN`。采录脚本只按固定时刻保存原件，不保证捕获任意网页的所有渲染帧，也不判断连续公告是否合格或把网页内容写入设施账。
