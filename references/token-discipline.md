# Token 纪律（大数据量操作前读取）

哪些内容**可以**进对话：claims 和 warnings 的文字、五项摘要、diff 变更块、脚本 stdout 的统计（fetched/failed 计数和失败原因）。

哪些内容**禁止**进对话：`geometry` 字段（单段几百上千坐标点）、渲染出的 HTML 全文（100KB+）、MCP/REST 的原始返回体、itinerary.json 全文。

操作规则：
- **改行程 = 改 JSON 里受影响的对象，再重跑渲染**。永远不要直接编辑产出的 HTML——它是 itinerary.json 的投影，改它会让事实源失真且白烧 token。
- 批量取数（几何、海拔）一律走脚本直连 REST，模型只看 stdout 摘要；逐段在对话里讨论坐标是事故。
- 需要检查渲染结果时，用无头浏览器截图或 `grep` 取关键片段（标题/警告数/横幅存在性），不读 HTML 全文。
- 写 JSON 用 `ensure_ascii=False`；大文件场景用紧凑分隔符 `separators=(',',':')`。
