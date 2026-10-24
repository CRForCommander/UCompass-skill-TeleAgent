# 数据契约与子命令 API

> 工程契约文档：compass.json Schema v1、compass_store.py 全部子命令的调用语法与输出格式、timetable.json 字段集、日志 frontmatter 规范、render 数据注入契约。
> 所有子命令输出为 **LLM 友好紧凑摘要**（纯文本，非原始 JSON 全文）。LLM 只调命令读摘要，禁止读原始数据文件。

## 目录

1. 数据目录结构与发现
2. compass.json Schema v1
3. compass_store.py 子命令 API
4. timetable.json 最小字段集
5. 日志 frontmatter 规范
6. render 数据注入契约（window.__UCOMPASS_DATA__）

---

## 1. 数据目录结构与发现

```
ucompass-data/                     ← 学生数据根目录
├── compass.json                   ← 主状态（唯一权威数据源）
├── compass.json.bak               ← 自动备份（每次写入前滚动）
├── dashboard.html                 ← render 生成物（可随时删除重建）
├── timetable/
│   ├── timetable.json             ← 课表解析结果
│   └── 原始课表文件（xlsx/csv，口述录入则无）
└── logs/
    ├── daily/   YYYY-MM-DD.md
    ├── weekly/  YYYY-Www.md
    ├── monthly/ YYYY-MM.md
    ├── yearly/  YYYY-semester.md
    └── archive/ Wxx.json          ← 周归档
```

**发现优先级**（LLM 执行）：学生明确指定 > USER.md 司南指针 > 平台持久化/云端目录下 ucompass-data/ > 当前工作目录 ucompass-data/。采用前校验 compass.json 合法性（version/phase 字段有效才算数），不合法继续按优先级找。首次创建时告知学生位置，并写入 USER.md 最小指针一次。

**脚本口径**：`--data-dir` 参数 > `UCOMPASS_DATA_DIR` 环境变量 > `./ucompass-data`。LLM 解析出的最终路径显式传给脚本。

## 2. compass.json Schema v1

```jsonc
{
  "version": 1,
  "phase": "voyaging",              // anchoring | voyaging | calibrating | archived
  "semester": {
    "id": "2026S1", "start": "2026-09-07", "end": "2027-01-18",
    "exam_weeks": [["2027-01-04", "2027-01-17"]]
  },
  "goals": [{
    "id": "g1", "name": "英语六级 520",
    "metric": "12月真题模考 ≥520", "evidence": "模考成绩单截图",
    "weekly_budget_h": 6, "urgency": "high",
    "version": 2, "status": "active",   // active|dormant|done|abandoned
    "history": [/* 旧版 goal 对象 */], "dormant_weeks": 0
  }],
  "milestones": [{
    "id": "m1", "goal_id": "g1", "name": "真题一轮完成",
    "due": "2026-10-16", "criterion": "近5年真题做完并订正",
    "status": "in_progress",            // pending|in_progress|achieved|delayed|dropped
    "verified_at": null, "new_due": null, "task_refs": ["t101"]
  }],
  "weekly_plans": [{
    "week_id": "2026-W43",
    "goal_time_alloc": {"g1": 6, "g2": 3, "g3": 5},
    "days": [{ "date": "2026-10-19", "tasks": [/* task 对象 */] }]
  }],
  "checkins": [{
    "id": "c1", "date": "2026-10-19", "task_id": "t101",
    "note": "背了30个", "timestamp": "2026-10-19T22:31:00",
    "makeup": false, "amount": 30
  }],
  "stats": {
    "streak_days": 12, "last_encourage_idx": 3,
    "recent_quotes": ["..."],                        // 最近10条金句文本，去重用
    "weekly_rates": [{"week_id": "2026-W42", "rate": 0.71, "level": "green"}],
    "borrow_log": [{"week": "2026-W43", "from": "g2", "to": "g1", "hours": 2, "repaid": false}]
  }
}
```

**task 对象**（weekly_plans.days[].tasks[] 元素）：

```jsonc
{
  "id": "t101", "goal_id": "g1", "title": "六级真题精读 1 套",
  "type": "deadline",                  // pinned|deadline|floating
  "granularity": "block",              // block(整块≥1h)|fragment(可碎片)
  "time": "19:00-20:30",               // pinned/deadline 排定时段；floating 为 null
  "deadline": "2026-10-21",            // deadline 型必有；其余 null
  "quota_standard": 1,                 // 单位随任务（套/页/词/题/h）
  "quota_minimum": 0.6,                // [internal] 内部判卷线，默认=标准×60%，永不出现在学生可见输出
  "urgency": "high", "rough": false,
  "rollover": 0,                       // 累计滚动量
  "adjustment_log": [{"date": "...", "action": "moved", "reason": "..."}],
  "status": "done"                     // pending|done|partial|missed|pending_confirm|rolled
}
```

**写入纪律**：所有变更经脚本（原子写：临时文件→替换；写前滚动 .bak）；LLM 不直接编辑 compass.json；每次变更前后打印变更摘要。

## 3. compass_store.py 子命令 API

统一语法：`python compass_store.py <子命令> [参数] [--data-dir <路径>]`

### status — 会话开头状态摘要
`python compass_store.py status`
输出 5~10 行：phase / 学期与剩余周数 / 各目标一行（名+状态+本周时长）/ 本周概览（x/y 完成、n 项待确认）/ 欠结算标记 / 欠月总结标记 / 临期里程碑。

### init — 定锚落盘 / 结构化更新
`python compass_store.py init --payload '<json>'`（或 `--payload @file.json`）
payload 含 semester + goals（name/metric/evidence/weekly_budget_h/urgency）。脚本补 id/version/status，phase=anchoring→voyaging，原子写入。输出：目标清单 + 总预算 + 数据目录路径。
`init --update --payload '<json>'` 为增量更新通道，payload 可含：goals（按 id/名称匹配，旧版入 history，版本号+1）、milestones（新增或改状态，验收三态由此写入）、semester、timetable（口述课表落盘）、exceptions（追加考试/活动事件）。

### checkin — 打卡处理
`python compass_store.py checkin --text "背了30个词" [--task "六级背词" --amount 30|50% --date 2026-10-19 --makeup]`
脚本：模糊匹配当日计划项 → 提取量（--amount 显式优先；内置数量词/程度词解析）→ 判卷标记 → 追加 daily 日志 → 更新 streak → 输出：判卷结果（本次量+当日累计）/ 剩余待完成 / 近期摘要（streak、周完成率、昨日概况、偏离标记）/ 占位鼓励语（内置语料库随机一条，LLM 可换生成版）。
同任务多次打卡量自动累计（判卷按当日累计）。未匹配 → 记"计划外完成"。0~5 点自动归前一日。

### settle — 周结算（结算规则唯一定义处）
`python compass_store.py settle [--week 2026-W43]`（默认上一完成周）
**对账闸门**：有待确认项（无打卡记录）时结算暂缓——输出待对账清单，不判失约不归档；补账（做了的 checkin --date --makeup，确认没做的 checkin --amount 0）后重跑即完成。
正常完成输出：完成率与三级诊断色 / 达成·部分·失约清单 / rollover 铺开明细 / 归档路径。完成后自动 render。

### rollover — 滚动补载
`python compass_store.py rollover --date 2026-10-20`（或 `--week`）
差额=标准−实际，铺入本周负荷 <80% 空闲日（就近分散），封顶 ≤单日标准×2，超顶输出转重排信号。输出逐日变更（"10-22 读书 +5 页"）。

### schedule-query — 排程查询
`python compass_store.py schedule-query --week 2026-W43`
输出五块：①空档表（按日，标 block/fragment+时长）②各日负荷 ③各目标近 3 天排程史 ④课表冲突 ⑤期限任务清单（deadline+剩余天数）。

### schedule-validate — 排程校验与落盘
`python compass_store.py schedule-validate --plan '<json>' [--write]`
plan 为拟写入的 days/tasks 结构。校验：时段冲突 / 每日 pinned ≤4 / 连续学习块 ≤2h / granularity 匹配 / rollover 封顶。违规项标 `[RED]` 逐条返回；全过输出 `PASS`。
`--write`：PASS 后写入 weekly_plans[]（同周覆盖），吸收 pending_rollover，并自动 render——这是周计划的唯一落盘通道。

### aggregate — 聚合
`python compass_store.py aggregate --scope week|month|year [--from 2026-10-01 --to 2026-10-31]`
读 logs frontmatter + stats → 输出聚合表（各周完成率、目标时间分配、趋势、里程碑状态、作息画像统计）。

### render — 生成仪表盘
`python compass_store.py render [--as-of YYYY-MM-DD]`
读 compass.json + logs + stats → 聚合计算 → 数据内联至模板 → 写 dashboard.html。输出：文件路径 + 六板块就绪校验 + 模板/Schema 版本核对结果。**render 总规则：任何数据落盘后必须调用。**

`--as-of YYYY-MM-DD`（可选）：虚拟今天——学期进度、当前周、教学周标签按该日期计算，用于归档/历史数据的演示重演（如 `render --as-of 2027-01-17` 冻结学期末视图）；缺省为真实今天。格式错误时报错退出，不影响现有生成物。

### archive — 学期归档
`python compass_store.py archive`
生成年度总结素材（aggregate --scope year 输出）→ 整目录搬入 `../archive/<semester-id>/`（compass.json+logs 同迁）→ phase=archived。LLM 先依素材写好 logs/yearly/ 总结再触发本命令。

### demo-init — 演示数据
`python compass_store.py demo-init [--force]`
生成演示学期：3 目标（学业/兴趣/健康组合）/ 6 里程碑 / 4 周计划与打卡（🟢🟡🔴三色周确定性可复现）/ 四级日志 / stats。输出完成后自动 render。`--force` 仅在已有 compass.json 时覆盖（非空未知目录拒绝防误删）。

## 4. timetable.json 最小字段集

```jsonc
{
  "source": "课表.xlsx", "parsed_at": "2026-10-24T15:00:00",
  "weekly": [
    {"day": 1, "period": 1, "start": "08:00", "end": "09:40",
     "course": "高等数学A(上)", "room": "教三405", "weeks": "1-16"}
  ],
  "exceptions": [
    {"date": "2026-11-05", "type": "exam", "title": "高数期中测验"}
  ]
}
```

- day：1=周一 … 7=周日；period：节次序号（1~5 默认时段 08:00/10:00/14:00/16:00/19:00 起，各 100 分钟，可用 start/end 覆盖）；
- weeks：适用周次（"1-16" 或 "1,3,5-16"），缺省全学期；
- exceptions.type：exam | event。

## 5. 日志 frontmatter 规范

daily 由脚本自动追加（打卡原话+时间戳+关联任务+提取量），无 frontmatter。weekly/monthly/yearly 由 LLM 撰写，必带机器可解析 frontmatter：

```yaml
---
week_id: 2026-W43        # weekly 用；monthly 用 month_id: 2026-10
rate: 0.72               # 完成率
level: green             # green|yellow|red
goal_hours: {g1: 5.5, g2: 3.0, g3: 4.0}
---
（正文：给学生看的叙述）
```

monthly 附加 `avg_rate` / `trend: up|flat|down`；aggregate 只读 frontmatter + stats，不解析正文。

## 6. render 数据注入契约（window.__UCOMPASS_DATA__）

template.html 内置占位符 `/*__UCOMPASS_DATA__*/null`，render 替换为聚合后的 JSON：

```jsonc
{
  "meta": {"generated_at": "...", "schema_version": 1, "template_version": "1.0"},
  "phase": "voyaging",
  "semester": {"start": "...", "end": "...", "progress": 0.42, "weeks_total": 19, "week_now": 8},
  "compass": {"streak_days": 12, "overall_rate": 0.71, "goal_health": [{"id": "g1", "name": "...", "status": "active", "progress": 0.35, "hours_done": 22, "budget": 6}]},
  "timeline": [{"goal": "英语六级", "due": "2026-10-16", "name": "真题一轮完成", "status": "in_progress"}],
  "current_week": {"week_id": "2026-W43", "days": [{"date": "2026-10-19", "tasks": [{"title": "...", "type": "pinned", "time": "19:00-20:30", "status": "done", "rough": false}]}]},
  "charts": {"weekly_rates": [{"label": "W40", "rate": 0.62}], "goal_pie": [{"label": "英语", "value": 22}], "trend": [0.62, 0.71, 0.72]},
  "logs_index": [{"type": "daily", "file": "2026-10-24.md", "date": "2026-10-24"}]
}
```

渲染红线：quota_minimum 永不渲染；rough 任务标 ⚠；借用预算（borrow 未还）显示为虚线块。模板版本号与 schema 版本不匹配时 render 输出告警提示重建。

> AI生成