---
name: roadtrip-planner
description: 生成自驾旅行攻略并渲染为可视化 HTML 行程页，包含真实坐标、驾车距离/时长、海拔、路线合理性校验（单日驾驶时长、高反、补能间隔、封路 Plan B）。当用户提到自驾、环线、路书、多日驾车行程规划，或需要核查驾驶时长/海拔/补能合理性时使用，即使没有明说"攻略"或"Skill"。不适用于非自驾（跟团、纯步行城市游）或单点问答（只问一个地方的天气/开放时间）。
---

# roadtrip-planner

把"AI 生成攻略 → 真实数据核验 → 合理性校验 → HTML 可视化"这条流程固化下来。核心原则：**LLM 只做两件事——骨架规划（Phase 2）和判断修正（Phase 5）；其余全部是脚本和工具调用**。行程数据的唯一事实源是 `work/itinerary.json`，所有校验、统计、渲染都读写这个文件，不在对话里复述全文。

这是 v0（个人试用版）：跳过了正式分发所需的 fixtures/eval/CI 那套重验证，专注"能跑通、能看出问题、能快速调整"。

## Phase 0：环境检测

先尝试调用一次 `maps_geo`（查"北京"）探活高德 MCP。**不要只看工具列表里有没有——key 失效时工具仍在列表里，但调用会报错**。

- 成功 → 继续 Phase 1
- 失败/工具不存在 → 读 `references/amap-mcp-usage.md`，把里面的引导文案讲给用户听（他可能是完全没配过，也可能是配置只在别的项目目录生效）。问用户想现在配置，还是先在无核验模式下出一版（所有坐标标 `unverified`，最终 HTML 顶部会有醒目提示）。用户选哪个都行，但必须显式问，不要替他决定降级。

同样地，如果用户环境里有小红书类 UGC 检索能力（比如 rednote-skills），Phase 2a 会用到；没有就跳过，退化为通用搜索。这些都是软依赖，缺了不影响出结果，只影响结果的信息丰富度和实时性。

## Phase 1：需求采集

一轮问全，别挤牙膏。**必填 4 项**：
1. 目的地/环线（或起终点+途经点）
2. 出发日期 + 总天数
3. 车型 + 能源类型（油/电），电车问续航
4. 同行人数 + 特殊约束（高反史、老人小孩、驾驶员几个）

**可选项，缺省用默认值并告知**：单日驾驶上限（默认 480 分钟，双驾驶员放宽到 600）、节奏偏好（赶路/均衡/休闲，默认均衡）、必去/必避、过夜海拔上限（有高反史默认压到 3000m，否则 3500m）。

规整成这样的对象（就是 `schemas/trip_request.schema.json` 描述的结构），写入 `work/trip_request.json`，然后**回显一张确认卡**，用户确认后才继续：

> 确认一下：10.1 出发，9 天，青甘大环线，2 人 SUV（续航按 550km 算），同行者有高反史 → 过夜海拔会控制在 3000m 以下，单日驾驶上限 8 小时。没问题我就开始。

## Phase 2：骨架规划（LLM 唯一的创造性工作）

**不碰高德，不碰任何工具**，纯粹基于你的知识和判断：想出过夜点序列、每日候选景点（只出名字，不用先核实坐标）。这一步决定行程的"好不好玩、顺不顺路"，是整个 Skill 里唯一需要审美和常识的地方。

如果目的地涉及季节性限制道路（山区公路、边境路段），主动想到"这条路这个季节是不是通的"——但不要凭训练数据下结论，交给 Phase 3 核实。

## Phase 2a：情报采集（软依赖，可跳过）

如果环境里有 UGC 内容检索能力（小红书类），针对骨架里 4A/5A 景区、高海拔景区、边境县、必去点，搜 `{景点名} 自驾 攻略/避坑 {年份}`，拉近期真实游记，提炼：停车/换乘信息、预约要求、避坑提示。没有这类工具就用通用网页搜索代替，或者跳过这一步直接进 Phase 3（此时这些软事实会在 Phase 5 产 `DILIGENCE_MISSING` 提示，不阻塞）。

这些线索只是素材，标 `source: "rednote"` 或 `"web_search_generic"`，不是最终依据。

## Phase 3：取数

- **[模型调用 MCP]** 对骨架里每个地点用 `maps_geo`/`maps_text_search` 找到真实坐标和 `poi_id`；相邻点之间用 `maps_direction_driving` 拿真实 `distance`(米)`duration`(秒)。**换算成 km/分钟再写入 JSON，别用模型估算的数字。**
- **[模型调用 MCP]** 途中需要补能的，用 `maps_around_search`(关键词"加油站"/"充电站") 沿途分段查（没有"沿路线搜索"这个工具，只能以几个采样点为中心分段查）。
- **[脚本]** 跑 `python3 scripts/enrich.py --input work/itinerary.json` 批量补海拔（Open-Elevation，免费无需 key，中国大陆坐标可能有几十米误差，属正常范围）。
- 高德 MCP 的天气工具（`maps_weather`）覆盖未来 3-4 天，超出预报窗口的日期直接不填天气，不是缺陷。

模型看到的 MCP 原始返回**不要复述进对话**，提取完关键字段立刻写文件。

## Phase 4：组装

把 Phase 2/2a/3 的结果写成完整的 `work/itinerary.json`（结构见 `schemas/itinerary.schema.json`），落盘。**不要在对话里输出这个 JSON 的全文**。

## Phase 5：校验循环（LLM 第二处需要判断的地方）

```bash
python3 scripts/validate_route.py --input work/itinerary.json --request work/trip_request.json
```

看返回的 `warnings`（含 `level: error/warning`），针对每条思考怎么改行程能消掉它——比如 `LONG_DRIVE` 就减少当日 stop 或挪一个过夜点，`FUEL_GAP` 就在长距离段插一个补给点。改完直接编辑 `work/itinerary.json` 里对应的字段，**不要删除或修改 `warnings` 数组本身，那是脚本的输出，只能靠改行程本身让它在下一轮消失**。重跑校验，最多循环 3 轮，剩下改不掉的 `warning` 级直接保留呈现，`error` 级要么改掉要么必须生成 Plan B（下一步）。

## Phase 6：Plan B

如果某天的路段命中你判断为季节性风险的道路（独库公路这类），生成一段 `plan_b`（结构同 `days`，标注 `trigger` 和 `affected_days`），同样过一遍 Phase 5 校验。

## Phase 7：渲染交付

```bash
python3 scripts/render_html.py --input work/itinerary.json --out outputs/<行程名>.html
python3 scripts/summarize.py --input work/itinerary.json
```

用 `summarize.py` 的输出组织成固定的五项摘要回复用户：总里程、总驾驶时长、最高海拔、警告数、Plan B 有无。附上 HTML 文件路径，末尾加一句"想调整哪天直接说，我会重排受影响的部分"。

## Phase 8：动态调整

用户提出改动时，先说清楚波及范围（哪几天会变），范围判断依据是行程结构本身而不是凭感觉——比如换掉第 3 天的过夜点，第 4 天的出发点必然跟着变。确认后只改受影响的 `days[]` 里对应的对象,不要重写整个 JSON,改完从 Phase 5 重新跑一轮校验和 Phase 7 重新渲染。

---

## 目录

```
roadtrip-planner/
├── SKILL.md
├── schemas/trip_request.schema.json   # Phase 1 输出结构（供人工参考，v0 未接 jsonschema 强校验）
├── schemas/itinerary.schema.json      # Phase 4 输出结构
├── scripts/
│   ├── enrich.py           # 批量海拔回写
│   ├── validate_route.py   # 7 条校验规则，产出 warnings（含 level）
│   ├── render_html.py      # itinerary.json → 独立 HTML
│   └── summarize.py        # 里程/时长/海拔/警告数统计
├── references/amap-mcp-usage.md   # Phase 0 失败时的引导文案
└── work/    # 存放 trip_request.json / itinerary.json（本地工作区，不提交）
```

## v0 已知局限（先用起来，遇到再改）

- 校验规则只有 7 条（驾驶时长/高反/爬升/补能/坐标核验/数据缺失），没有票务预约、边防证这类尽调规则——遇到需要就加。
- 没有 fixtures/回放测试，每次都是真实调用高德，会消耗免费配额。
- 没有 `patch_day.py`，Phase 8 目前靠模型直接编辑 JSON 里对应字段，行程很长时改动量可能偏大——如果实测发现这是个真问题再补。
