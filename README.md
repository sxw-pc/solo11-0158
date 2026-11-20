# 城市公厕服务履约采录台

本地虚构的公厕服务证据工程。设施开放与故障、承包商班次履约、公众投诉补救是三套独立账本。设施停水会暂停保洁班次裁定，设施故障会重开先前关闭的投诉；每次裁定与原始输入持久化。没有真实投诉者、位置或外部服务。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m toiletservice demo --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：`python -m toiletservice facility-import --db state.sqlite --file fixtures/facility-ok.json`；`shift-import` 与 `complaint-import` 同法。各有 `*-bad.json` 反例。`report` 命令返回设施可用厕位、班次状态、投诉与补救、证据 SHA 和裁定日志；`audit` 从原始文件字节在全新内存库重放，并与当前状态逐表比对。边界是离线承包服务核验，不对接真实传感器或公共投诉平台。

此增强副本另带一个本地合成公告样页 `mock/notice.html` 和预定公告 `mock/notice-source.json`，原仓没有网页。样页在同一 `data-version=v2` 下经历四次客户端更新，视觉文案两次短暂显示“正常开放”，无障碍树始终保留“暂停开放”；公告源文件规定 v2 应显示“暂停开放”，声明的更新纪元为 1 至 4。`node scripts/capture-notice.mjs <输出目录>` 用本机 Edge 的无头浏览器和 CDP 在同一会话保存截图、渲染 DOM、无障碍树及摘要清单；如 Edge 安装位置不同，可设置 `EDGE_BIN`。采录脚本只保存原件并核验其完整性，不判断连续公告是否合格，也不把网页内容写入设施账。

## 纪元观察式采录（在页面脚本运行前监听）

采录脚本默认 `--mode=observer`：导航前通过 `Page.addScriptToEvaluateOnNewDocument` 注入记录器，先于页面自身脚本挂 `MutationObserver`，单调监听 `#notice` 的 `data-epoch`/`data-version`。宿主侧每 25ms 轮询一次，发现声明范围内的新纪元即原子采集一组原件；采集前后各读一次状态，纪元或版本在采集中途变化则该组判为撕裂（torn），整组丢弃并按缺口登记，绝不把两个纪元拼接成一组。每个被采纳的纪元在同一页面版本下绑定：截图 PNG、渲染 DOM、无障碍树、浏览器 `performance.now()` 时间与采录主机时间，以及对三类原件字节实算的 SHA-256。`capture.json` 记录页内转换序列与单调违例；非单调的采录不能为页面作证。

- 仅对 `mock/notice-source.json` 声明的本地页面纪元作证，不声称通用网页的每一合成帧都被捕获。
- `--mode=fixed` 保留按固定时刻拍四次的旧行为，可配 `--shutter-ms=400,2100` 复现繁忙机器只拍到纪元 1、4 而漏掉 2、3；页内记录器仍见过全部转换，缺口按声明纪元 1-4 推导，最终纪元 4 不会被写成全窗通过，已捕获的纪元 1 错误画面也不会被缺口抹去。
- `--schedule=2,4` 只允许持久化指定纪元的完整原件（转换仍全量记录），用于构造真实的“仅缺口”采录。
- 无障碍结论只引用浏览器可访问性树中的文本，不声称屏幕阅读器实际发声。
- 无 root 的 Linux/arm64 容器没有官方 Edge 包时，可把 `EDGE_BIN` 指向原生 arm64 Chromium/Chrome 并自行准备字体与依赖库；截图哈希由运行时浏览器字节实算，不预设跨机器固定摘要。仓库 `evidence/` 下的三份采录即由真实浏览器按上述流程生成。

## 浏览器证据核证与跨账入口

`notice-import` 在人工 `notice_review` 之外新增两类事件，接入同一 `report`/`audit`/CLI 通道：

- `notice_evidence`：登记一个采录目录。`toiletservice/evidence.py` 先从磁盘重算截图、DOM、AX 树与源文件哈希，再从原始 DOM 派生视觉文案、从原始 AX 树派生 StaticText，并与清单逐项对账；哈希不符、DOM/AX 与标签不一致、缺口清单无法与声明纪元对账、纪元非单调等情况一律拒绝，整个事件事务回滚。同事件同摘要重放幂等；同一事件 id 绑定不同采录按不可变冲突处理。
- `notice_certify`：对已登记采录按案件合同种类裁定，并写入带原件摘要的核证版本，再调用 `complaint.reconcile_notice` 驱动关联投诉。

两份签约公告合同的规则不同，同一批页面原件下结论可以不同，互不复制：

- **最终状态合同（final）**：只看窗口结束时最后一个完整纪元；其视觉文案与无障碍树文本都等于后台 v2 的“暂停开放”即交付正确（`mismatch=false`，投诉 rejected）。更早纪元的缺口不阻断，但已捕获的错误纪元仍留痕。
- **持续一致合同（continuous）**：v2 发布到窗口结束的每个声明纪元都须有同版完整证据且两种文字均为“暂停开放”。任一可证错误纪元即确认不一致（`mismatch=true`，投诉 confirmed，`due_at` 按覆盖合同的 `remedy_minutes`，如 C1 的 120 分钟）；仅有证据缺口、没有可证错误时保持 `pending_gap`，投诉维持 pending 并列明缺失纪元，不得自动写成一致。后到的完整重采可把待核案件改为确认；后到的“撤证”（最新核证变为非结论性）会把投诉退回 pending，旧回执仍可在 `decision_log` 查询。

人工 `notice_review` 完整保留为回归路径；人工复核与浏览器核证中时间最新的结论生效。网页最后显示正确不解除 `facility-bad.json` 的真实停水：`facility.recompute` 仍给出 `opening=closed`，待巡查班次仍由 `shift.reconcile_shifts` 暂停。`report` 的 `notice_summary` 派生视图并列两份合同各版状态、关联投诉、每纪元原件哈希、漏采纪元与后台 opening 版本；`audit` 从原始事件字节在内存库重放并逐表比对。
