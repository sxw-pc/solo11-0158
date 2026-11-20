# 浏览器采录原件（真实 CDP 采录）

本目录每个子目录都是一次 `node scripts/capture-notice.mjs` 的完整输出，包含
`capture.json` 清单、每个纪元的截图 PNG、渲染 DOM、无障碍树 AX JSON 以及采录时复制的
`notice-source.json`。这些文件由真实浏览器经 CDP 生成，不是手工编写的夹具；截图与文件
SHA-256 均为运行时浏览器字节实算，换机重采摘要可能不同，测试不假定跨机器固定摘要。

| 目录 | 模式 | 持久化纪元 | 缺口 | 用途 |
| --- | --- | --- | --- | --- |
| `notice-wc-a-full` | observer | 1,2,3,4 | 无 | 四纪元全程同版证据；最终合同通过、持续合同确认违约 |
| `notice-wc-a-busy` | fixed `--shutter-ms=400,2100` | 1,4 | 2,3 | 繁忙机器漏采；纪元 1 错误画面仍在，缺口不能抹去违约 |
| `notice-wc-a-scheduled-gaps` | observer `--schedule=2,4` | 2,4 | 1,3 | 仅有缺口、无可证错误；持续合同保持待核，不得自动判一致 |

采录环境（arm64 Linux 容器，无 root、无官方 Microsoft Edge arm64 包）：
`EDGE_BIN` 指向原生 arm64 Chromium（Chrome for Testing，经同一 CDP 协议驱动），
中文字体由本地 Noto CJK 提供。Windows/macOS 上设置 `EDGE_BIN` 为真实 Edge 即可，
脚本默认路径仍是 Windows Edge 安装位置。

重新采录（示例）：

```bash
EDGE_BIN=/path/to/edge node scripts/capture-notice.mjs evidence/notice-wc-a-full
EDGE_BIN=/path/to/edge node scripts/capture-notice.mjs --mode=fixed --shutter-ms=400,2100 evidence/notice-wc-a-busy
EDGE_BIN=/path/to/edge node scripts/capture-notice.mjs --schedule=2,4 evidence/notice-wc-a-scheduled-gaps
```

无障碍结论只引用浏览器可访问性树中的 StaticText，不声称屏幕阅读器实际发声。
