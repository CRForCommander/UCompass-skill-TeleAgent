#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compass_store.py — 你的司南UCompass 航海日志引擎

数据读写唯一入口：状态摘要 / 定锚落盘 / 打卡 / 周结算 / 滚动补载 /
排程查询与校验落盘 / 聚合 / 仪表盘渲染 / 学期归档 / 演示数据。

契约见 references/api_reference.md。纯标准库，Python 3.7+。
用法：python compass_store.py <子命令> [参数] [--data-dir <路径>]
"""

import argparse
import json
import os
import random
import re
import shutil
import sys
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCHEMA_VERSION = 1
TEMPLATE_VERSION = "1.0"
DAY_CAPACITY_H = 10.0          # 单日默认可负荷小时（80% 阈值的基数）
UNTUNED_TASK_H = 0.75          # 无时段任务的负荷估算
WEEK_WINDOW = (8, 22)          # 排程考察窗口 08:00~22:00

DEFAULT_PERIODS = {            # 节次默认时段（可被 start/end 覆盖）
    1: ("08:00", "09:40"), 2: ("10:00", "11:40"), 3: ("14:00", "15:40"),
    4: ("16:00", "17:40"), 5: ("19:00", "20:40"), 6: ("21:00", "21:50"),
}

# 鼓励语语料库（与 references/templates.md §9 同步的内置副本，兜底通道）
ENCOURAGE_LIB = [
    "今日航线灯全绿，稳稳向前 ⛵", "连续第 {n} 天，这条航线越走越顺了",
    "电量 +1 格，本周进度条又亮了一截 ⚡", "桅杆立住了，剩下的交给风和时间",
    "每一格进度都是真桨划出来的，不掺水", "小步快跑第 {n} 天——方向对了，就不怕远",
    "今天的浪不算小，你划过去了 🌊", "罗盘指针又正了一分，继续保持",
    "{task} 收进船舱，今晚可以睡个踏实觉", "连续 {n} 天不断档，这本身就是种了不起",
    "不是每天都要满帆，今天这样刚刚好", "风浪里最值钱的是节奏，你的节奏还在",
    "进度 {p}%——航道过半，越往后越顺流", "星星不会辜负夜里划桨的人 ✨",
    "{task} 达成！船身又轻了一分", "别小看今天的量，复利就是这么长的",
    "第 {n} 天打卡，航线图上又多一个实心点", "半程补给站到了，喝口水，我们继续",
    "今天的额度清零，明天的海面见 🌅", "航海日志 +1 行，你的故事正在变厚",
]

WEEK_CN = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
TASK_TYPE_ICON = {"pinned": "🔒", "deadline": "📖", "floating": "🌊"}
STATUS_CN = {"pending": "待完成", "done": "达成", "partial": "部分达成", "missed": "失约",
             "pending_confirm": "待确认", "rolled": "已滚动", "achieved": "达成",
             "in_progress": "进行中", "delayed": "延期", "dropped": "放弃"}


# ── 路径与 IO ──────────────────────────────────────────────

def resolve_data_dir(arg):
    d = arg or os.environ.get("UCOMPASS_DATA_DIR") or os.path.join(os.getcwd(), "ucompass-data")
    return os.path.abspath(d)


def ensure_layout(dd):
    for sub in ("timetable", os.path.join("logs", "daily"), os.path.join("logs", "weekly"),
                os.path.join("logs", "monthly"), os.path.join("logs", "yearly"),
                os.path.join("logs", "archive")):
        os.makedirs(os.path.join(dd, sub), exist_ok=True)


def compass_path(dd):
    return os.path.join(dd, "compass.json")


def load_data(dd, required=True):
    p = compass_path(dd)
    if not os.path.exists(p):
        if required:
            die("无 compass.json（数据目录 %s）。请先定锚：init，或运行 demo-init 生成演示数据。" % dd)
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        die("compass.json 解析失败：%s（可从 compass.json.bak 恢复）" % e)
    if not isinstance(data.get("version"), int) or data.get("phase") not in (
            "anchoring", "voyaging", "calibrating", "archived"):
        die("compass.json 校验失败：version/phase 无效（%s）" % p)
    return data


def save_data(dd, data):
    ensure_layout(dd)
    p = compass_path(dd)
    bak = p + ".bak"
    if os.path.exists(p):
        shutil.copy2(p, bak)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def load_timetable(dd):
    p = os.path.join(dd, "timetable", "timetable.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def append_file(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def die(msg, code=1):
    print("[ERROR] " + msg)
    sys.exit(code)


# ── 日期工具 ──────────────────────────────────────────────

def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def d2s(d):
    return d.strftime("%Y-%m-%d")


def monday_of(d):
    return d - timedelta(days=d.weekday())


def week_id_of(d):
    y, w, _ = d.isocalendar()
    return "%d-W%02d" % (y, w)


def teach_week_of(wid, semester_start):
    """ISO 周号 → 学期教学周序号（开学周一=第 1 周）；开学前或无学期返回 None。"""
    try:
        m = monday_of_weekid(wid)
        s = parse_date(semester_start)
        s_mon = monday_of(s)
        n = (m - s_mon).days // 7 + 1
        return n if n >= 1 else None
    except Exception:
        return None


def teach_week_label(wid, semester_start):
    """显示用：教学周中文标签（如"第 8 周"）；无法换算时回退 ISO 周号。"""
    n = teach_week_of(wid, semester_start)
    if n:
        return "第 %d 周" % n
    return wid


def monday_of_weekid(wid):
    y, w = wid.split("-W")
    return date(int(y), 1, 4) + timedelta(weeks=int(w) - 1, days=-(date(int(y), 1, 4).weekday()))


def now_effective_date():
    """打卡归属：0~5 点计前一日。"""
    now = datetime.now()
    d = now.date()
    if now.hour < 5:
        d -= timedelta(days=1)
    return d, now


def parse_hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def fmt_hm(minutes):
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def overlap(a1, a2, b1, b2):
    return a1 < b2 and b1 < a2


# ── 任务/判卷工具 ──────────────────────────────────────────

def _norm_match(s):
    return re.sub(r"[\s\d.,，。:：()（）\-—/%]", "", (s or "").lower())


def find_task(day_tasks, text, task_hint):
    if not day_tasks:
        return None
    if task_hint:
        hint = task_hint.lower()
        for t in day_tasks:
            tid = t.get("id") or ""
            ttl = (t.get("title") or "").lower()
            if tid == task_hint or hint in ttl or ttl in hint:
                return t
        best = max(day_tasks, key=lambda t: SequenceMatcher(None, hint, t.get("title") or "").ratio())
        if SequenceMatcher(None, hint, best.get("title") or "").ratio() > 0.35:
            return best
        return None
    low = _norm_match(text)

    def score(t):
        tl = _norm_match(t["title"])
        if not tl:
            return 0.0
        if tl in low:
            return 1.0
        # 标题切分为语义词块，学生话含任一词块即强命中（≥2 字词块防误配）
        chunks = [tl[i:i + n] for n in (4, 3, 2)
                  for i in range(len(tl) - n + 1) if len(tl) >= n]
        hit3 = any(c in low for c in chunks if len(c) >= 3)
        if hit3:
            return 0.9
        hit2 = any(c in low for c in chunks if len(c) == 2)
        if hit2:
            return 0.6
        grams = [tl[i:i + 2] for i in range(len(tl) - 1)]
        if not grams:
            return 0.0
        return sum(1 for g in grams if g in low) / len(grams)

    best = max(day_tasks, key=score)
    s_best = score(best)
    if s_best >= 0.3:
        return best
    # 动词兜底：含完成类动词时，标题与学生话有共享核心字（≥1 字）的任务命中；
    # 当日仅 1 个任务时直接命中
    if VERB_DONE_RE.search(text or "") or VERB1_DONE_RE.search(text or ""):
        if len(day_tasks) == 1:
            return day_tasks[0]
        cand = [t for t in day_tasks
                if set(_norm_match(t["title"])) & set(low)]
        if len(cand) == 1:
            return cand[0]
    return None


NEG_RE = re.compile(r"没做|没时间|没完成|没弄|没搞|没背|没写|没去|skip|放弃了|没干")
HALF_RE = re.compile(r"一半|过半|50%|半套|半张|半小时")
NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(个词|单词|个|页|题|套|篇|小时|h|H|分钟|分|km|公里|千米|组|次)")
SKIPNUM_RE = re.compile(r"\d+(?:\.\d+)?\s*(月|日|号|点|周|年|届|次课)")
# 打卡话术动词（verb_full 的词库扩充：完成/收尾类说法）
VERB_DONE_RE = re.compile(r"完了|搞定|做完|完成|done|打卡|背完|看完|写完|练完|整理完|收尾")
# 单字完成动词：学生说"背了X个/看了X页"时触发（配合共享字域匹配）
VERB1_DONE_RE = re.compile(r"[背看写练读刷跑做]")


def parse_amount(text, standard):
    """从打卡原话提取实际量；返回 (amount, how)。"""
    if not text:
        return float(standard), "default_full"
    t = text.strip()
    if NUM_RE.search(t):
        m = NUM_RE.search(t)
        return float(m.group(1)), "num_unit"
    if HALF_RE.search(t):
        return round(standard * 0.5, 2), "half"
    if NEG_RE.search(t):
        return 0.0, "neg"
    bare = [m for m in re.finditer(r"\d+(?:\.\d+)?", t)
            if not SKIPNUM_RE.search(t[max(0, m.start() - 2):m.end() + 3])]
    if bare:
        v = float(bare[0].group())
        if 0 < v <= 1000:
            return v, "bare_num"
    if VERB_DONE_RE.search(t):
        return float(standard), "verb_full"
    return float(standard), "default_full"


def judge(amount, std, minimum):
    if amount >= std - 1e-9:
        return "done"
    if amount >= minimum - 1e-9:
        return "partial"
    return "low"


def day_load_h(day):
    load = 0.0
    for t in day.get("tasks", []):
        if t.get("time"):
            a, b = t["time"].split("-")
            try:
                load += (parse_hm(b) - parse_hm(a)) / 60.0
            except Exception:
                load += UNTUNED_TASK_H
        else:
            load += UNTUNED_TASK_H
    return load


def new_task_id(data):
    n = data.get("_seq", 100)
    data["_seq"] = n + 1
    return "t%d" % n


def get_plan(data, wid):
    for p in data.get("weekly_plans", []):
        if p.get("week_id") == wid:
            return p
    return None


def all_days(data):
    for p in data.get("weekly_plans", []):
        for d in p.get("days", []):
            yield p, d


def amounts_of(data, task_id):
    return [c.get("amount", 0) or 0 for c in data.get("checkins", []) if c.get("task_id") == task_id]


def course_blocks_on(tt, d):
    """某日期的课表占用块 [(start_min, end_min, label)]。"""
    blocks = []
    if not tt:
        return blocks
    for exc in tt.get("exceptions", []):
        if exc.get("date") == d2s(d):
            blocks.append((parse_hm("08:00"), parse_hm("22:00"), "event:" + exc.get("title", "")))
    for row in tt.get("weekly", []):
        if row.get("day") != d.isoweekday():
            continue
        s, e = row.get("start"), row.get("end")
        if not s or not e:
            s, e = DEFAULT_PERIODS.get(row.get("period", 1), ("08:00", "09:40"))
        blocks.append((parse_hm(s), parse_hm(e), row.get("course", "课")))
    return blocks


def free_slots(day, tt):
    """当日空档表：[(起,止,时长分钟,粒度)]。"""
    d = parse_date(day["date"])
    busy = course_blocks_on(tt, d)
    for t in day.get("tasks", []):
        if t.get("time") and t.get("type") in ("pinned", "deadline"):
            a, b = t["time"].split("-")
            busy.append((parse_hm(a), parse_hm(b), t["title"]))
    busy.sort()
    slots, cur = [], WEEK_WINDOW[0] * 60
    for s, e, _ in busy:
        if s > cur:
            slots.append((cur, min(s, WEEK_WINDOW[1] * 60)))
        cur = max(cur, e)
    if cur < WEEK_WINDOW[1] * 60:
        slots.append((cur, WEEK_WINDOW[1] * 60))
    out = []
    for s, e in slots:
        dur = e - s
        if dur >= 60:
            out.append((fmt_hm(s), fmt_hm(e), dur, "block"))
        elif dur >= 20:
            out.append((fmt_hm(s), fmt_hm(e), dur, "fragment"))
    return out


def encourage_pick(data):
    idx = data["stats"].get("last_encourage_idx", -1)
    cand = [i for i in range(len(ENCOURAGE_LIB)) if i != idx]
    i = random.choice(cand)
    data["stats"]["last_encourage_idx"] = i
    return ENCOURAGE_LIB[i]


# ── 子命令：status ─────────────────────────────────────────

def cmd_status(args):
    dd = resolve_data_dir(args.data_dir)
    data = load_data(dd, required=False)
    if not data:
        print("status: 无数据（目录 %s）" % dd)
        print("→ 请先定锚（init），或运行 demo-init 看演示效果")
        return
    sem = data.get("semester", {})
    today, _ = now_effective_date()
    lines = []
    lines.append("phase=%s  学期=%s(%s~%s)" % (data["phase"], sem.get("id", "?"),
                                               sem.get("start", "?"), sem.get("end", "?")))
    try:
        s, e = parse_date(sem["start"]), parse_date(sem["end"])
        left = (e - today).days // 7 + 1
        lines.append("学期进度: 第 %d/%d 周，剩约 %d 周" % (
            (today - s).days // 7 + 1, (e - s).days // 7 + 1, max(left, 0)))
    except Exception:
        pass
    for g in data.get("goals", []):
        ms = [m for m in data.get("milestones", []) if m.get("goal_id") == g["id"]]
        ach = sum(1 for m in ms if m.get("status") == "achieved")
        lines.append("目标 %s[%s] 预算%sh/w urgency=%s 进度%d/%d里程碑" % (
            g["name"], g.get("status", "active"), g.get("weekly_budget_h", "?"),
            g.get("urgency", "normal"), ach, len(ms)))
    wid = week_id_of(today)
    plan = get_plan(data, wid)
    if plan:
        ts = [t for d in plan.get("days", []) for t in d.get("tasks", [])]
        done = sum(1 for t in ts if t.get("status") in ("done", "partial"))
        pc = sum(1 for t in ts if t.get("status") == "pending_confirm")
        lines.append("本周(%s): %d/%d 完成%s streak=%d天" % (
            wid, done, len(ts), ("，%d项待确认" % pc) if pc else "",
            data["stats"].get("streak_days", 0)))
    else:
        rates_now = data["stats"].get("weekly_rates", [])
        if rates_now and rates_now[-1].get("week_id") == wid:
            lines.append("本周(%s): 已结算 → 待生成新周计划（schedule-query）" % wid)
        else:
            lines.append("本周(%s): 无周计划 → 待生成（先 settle 上一周）" % wid)
    rates = data["stats"].get("weekly_rates", [])
    if rates and rates[-1].get("week_id") != wid:
        last_settled = rates[-1]["week_id"]
        lines.append("⚠ 欠结算: %s 未结算 → 生成新周计划前须先 settle" % last_settled)
    # 欠月总结
    try:
        m_prev = (today.replace(day=1) - timedelta(days=1))
        if today.day >= 3:
            mp = os.path.join(dd, "logs", "monthly", m_prev.strftime("%Y-%m") + ".md")
            if not os.path.exists(mp):
                lines.append("欠月总结: %s（被动补做，不强推）" % m_prev.strftime("%Y-%m"))
    except Exception:
        pass
    for m in data.get("milestones", []):
        if m.get("status") in ("pending", "in_progress") and m.get("due"):
            days_left = (parse_date(m["due"]) - today).days
            if 0 <= days_left <= 3:
                lines.append("⏰ 临期里程碑: %s 截 %s（%d天后）" % (m["name"], m["due"], days_left))
    if today > parse_date(sem.get("end", "9999-12-31")):
        lines.append("🗄 学期已结束 → 触发归档（archive）")
    print("\n".join(lines[:10]))
    return


# ── 子命令：init ───────────────────────────────────────────

def cmd_init(args):
    dd = resolve_data_dir(args.data_dir)
    ensure_layout(dd)
    payload = read_payload(args.payload)
    if args.update:
        data = load_data(dd)
        apply_update(data, payload, dd)
        save_data(dd, data)
        print("init --update 完成：goals=%d milestones=%d" % (
            len(data.get("goals", [])), len(data.get("milestones", []))))
        print("数据目录: %s" % dd)
        return
    sem = payload.get("semester")
    goals = payload.get("goals")
    if not sem or not goals:
        die("payload 需含 semester 与 goals[]")
    data = {
        "version": SCHEMA_VERSION, "phase": "voyaging", "semester": sem,
        "goals": [], "milestones": payload.get("milestones", []),
        "weekly_plans": [], "checkins": [],
        "stats": {"streak_days": 0, "last_encourage_idx": -1, "recent_quotes": [],
                  "weekly_rates": [], "borrow_log": [], "last_streak_date": None,
                  "pending_rollover": {}},
        "_seq": 100,
    }
    for i, g in enumerate(goals, 1):
        g = dict(g)
        g.setdefault("id", "g%d" % i)
        g.setdefault("version", 1)
        g.setdefault("status", "active")
        g.setdefault("history", [])
        g.setdefault("dormant_weeks", 0)
        data["goals"].append(g)
    for j, m in enumerate(data["milestones"], 1):
        m = dict(m)
        m.setdefault("id", "m%d" % j)
        m.setdefault("status", "pending")
        m.setdefault("verified_at", None)
        m.setdefault("new_due", None)
        m.setdefault("task_refs", [])
    save_data(dd, data)
    total = sum(g.get("weekly_budget_h", 0) for g in data["goals"])
    print("定锚落盘完成 ✓")
    for g in data["goals"]:
        print("  %s[%s] %s · 预算%sh/w · %s" % (g["id"], g.get("urgency", "normal"),
                                                g["name"], g.get("weekly_budget_h", "?"), g.get("metric", "")))
    print("总预算: %d h/周 ｜ 数据目录: %s" % (total, dd))
    print("→ 下一步: 提供课表（timetable_parse.py / 口述）或直接进入拆解（schedule-query）")


def read_payload(p):
    if not p:
        die("缺少 --payload（JSON 字符串或 @file.json）")
    if p.startswith("@"):
        with open(p[1:], "r", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(p)


def apply_update(data, payload, dd):
    """init --update：goals/milestones/semester/timetable/exceptions 增量更新。"""
    for g in payload.get("goals", []):
        cur = next((x for x in data["goals"]
                    if x.get("id") == g.get("id") or x.get("name") == g.get("name")), None)
        if cur:
            if any(cur.get(k) != v for k, v in g.items() if k not in ("id",)):
                cur.setdefault("history", []).append(
                    {k: cur.get(k) for k in ("name", "metric", "evidence",
                                             "weekly_budget_h", "urgency", "status")})
                cur["version"] = cur.get("version", 1) + 1
            cur.update({k: v for k, v in g.items()})
        else:
            g = dict(g)
            g.setdefault("id", "g%d" % (len(data["goals"]) + 1))
            g.setdefault("version", 1); g.setdefault("status", "active")
            g.setdefault("history", []); g.setdefault("dormant_weeks", 0)
            data["goals"].append(g)
    for m in payload.get("milestones", []):
        cur = next((x for x in data.get("milestones", []) if x.get("id") == m.get("id")), None)
        if cur:
            cur.update(m)
        else:
            m = dict(m)
            m.setdefault("id", "m%d" % (len(data.get("milestones", [])) + 1))
            m.setdefault("status", "pending"); m.setdefault("verified_at", None)
            m.setdefault("new_due", None); m.setdefault("task_refs", [])
            data.setdefault("milestones", []).append(m)
    if "semester" in payload:
        data["semester"].update(payload["semester"])
    if "timetable" in payload or "exceptions" in payload:
        tt = payload.get("timetable") or {}
        if "exceptions" in payload:
            tt["exceptions"] = payload.get("exceptions")
        write_json(os.path.join(dd, "timetable", "timetable.json"), tt)
        print("timetable 已写入（口述课表通道）")
    # 休眠/重启处理
    for g in data["goals"]:
        if g.get("status") == "dormant":
            g["dormant_weeks"] = g.get("dormant_weeks", 0) + 1


# ── 子命令：checkin ────────────────────────────────────────

def checkin_core(data, dd, text, task_hint, amount_arg, date_arg, makeup, ts=None):
    today, now = now_effective_date()
    d = parse_date(date_arg) if date_arg else today
    wid = week_id_of(d)
    plan = get_plan(data, wid)
    day = next((x for x in plan.get("days", []) if x["date"] == d2s(d)), None) if plan else None
    day_tasks = day.get("tasks", []) if day else []
    task = find_task(day_tasks, text, task_hint) if day_tasks else None
    ts = ts or now.strftime("%Y-%m-%dT%H:%M:%S")
    if task is None:
        cid = "c%d" % (len(data["checkins"]) + 1)
        data["checkins"].append({"id": cid, "date": d2s(d), "task_id": None,
                                 "note": text, "timestamp": ts,
                                 "makeup": bool(makeup), "amount": None})
        append_file(os.path.join(dd, "logs", "daily", d2s(d) + ".md"),
                    "- %s ｜ 计划外完成 ｜ %s\n" % (ts, text))
        return {"matched": False, "date": d2s(d), "note": text}
    std = float(task.get("quota_standard", 1) or 1)
    minimum = float(task.get("quota_minimum", std * 0.6) or std * 0.6)
    if amount_arg is not None:
        if isinstance(amount_arg, str) and amount_arg.strip().endswith("%"):
            amount = round(std * float(amount_arg.strip().rstrip("%")) / 100.0, 2)
        else:
            amount = float(amount_arg)
        how = "explicit"
    else:
        amount, how = parse_amount(text, std)
    prior = sum(amounts_of(data, task["id"]))
    cumulative = round(prior + amount, 2)
    j = judge(cumulative, std, minimum)
    prev = task.get("status", "pending")
    if j == "done":
        task["status"] = "done"
    elif j == "partial":
        task["status"] = "partial"
    # low → 保持 pending，判卷延后（不冤枉）
    cid = "c%d" % (len(data["checkins"]) + 1)
    data["checkins"].append({"id": cid, "date": d2s(d), "task_id": task["id"],
                             "note": text, "timestamp": ts,
                             "makeup": bool(makeup), "amount": amount})
    append_file(os.path.join(dd, "logs", "daily", d2s(d) + ".md"),
                "- %s ｜ %s ｜ 提取量=%s(%s) 累计=%s/%s ｜ 判卷=%s\n" % (
                    ts, task["title"], amount, how, cumulative, std,
                    {"done": "达成", "partial": "部分达成", "low": "低于保底线(待复核)"}[j]))
    # streak：当日任务全部 done/partial 时 +1（同日不重复计）
    remaining = [t for t in day_tasks if t.get("status") not in ("done", "partial")]
    st = data["stats"]
    if day_tasks and not remaining and st.get("last_streak_date") != d2s(d):
        st["streak_days"] = st.get("streak_days", 0) + 1
        st["last_streak_date"] = d2s(d)
    return {"matched": True, "task": task, "amount": amount, "cumulative": cumulative,
            "judgment": j, "how": how, "remaining": remaining, "date": d2s(d), "makeup": makeup}


def cmd_checkin(args):
    dd = resolve_data_dir(args.data_dir)
    data = load_data(dd)
    r = checkin_core(data, dd, args.text, args.task, args.amount, args.date, args.makeup)
    if not r["matched"]:
        print("未匹配到当日计划项 → 已登记「计划外完成」：%s" % r["note"])
        print("→ 周复盘时会作为素材分析")
        save_data(dd, data)
        return
    task, j = r["task"], r["judgment"]
    print("✅ %s「%s」本次量=%s 累计=%s/%s → 判卷：%s%s" % (
        "补卡 " if r["makeup"] else "", task["title"], r["amount"], r["cumulative"],
        task.get("quota_standard", "?"),
        {"done": "达成(1)", "partial": "部分达成(0.5)", "low": "低于保底线，暂不判"}[j],
        "（显式量）" if r["how"] == "explicit" else ""))
    if r["remaining"]:
        print("今天还有 %d 项待完成：%s" % (
            len(r["remaining"]), "、".join(t["title"] for t in r["remaining"])))
    else:
        print("今日清盘！全部任务有记录")
    st = data["stats"]
    # 近期摘要（供 LLM 生成鼓励语）
    wid = week_id_of(parse_date(r["date"]))
    plan = get_plan(data, wid)
    ts = [t for d in (plan.get("days", []) if plan else []) for t in d.get("tasks", [])]
    done = sum(1 for t in ts if t.get("status") in ("done", "partial"))
    half = sum(0.5 for t in ts if t.get("status") == "partial")
    rate = (done - half) / len(ts) if ts else 0
    devs = [t["title"] for d in (plan.get("days", []) if plan else [])
            for t in d.get("tasks", []) if t.get("rollover", 0) > 0 or t.get("adjustment_log")]
    print("── 近期摘要 ──")
    print("streak=%d天 ｜ 周完成率 %.0f%% ｜ %s" % (
        st.get("streak_days", 0), rate * 100,
        ("偏离标记: " + "、".join(devs[:3])) if devs else "无偏离标记"))
    enc = encourage_pick(data)
    enc = (enc.replace("{n}", str(st.get("streak_days", 0)))
              .replace("{task}", task["title"])
              .replace("{p}", str(int(rate * 100))))
    print("[encourage] " + enc)
    save_data(dd, data)


# ── 子命令：settle ─────────────────────────────────────────

def cmd_settle(args):
    dd = resolve_data_dir(args.data_dir)
    data = load_data(dd)
    today, _ = now_effective_date()
    if args.week:
        wid = args.week
    else:
        wid = week_id_of(today - timedelta(days=7))   # 默认上一完成周
    plan = get_plan(data, wid)
    if not plan:
        die("周 %s 无计划，无需结算" % wid)
    rates = data["stats"].get("weekly_rates", [])
    if any(r.get("week_id") == wid for r in rates):
        print("周 %s 已结算过（幂等），如需重算请人工处理归档" % wid)
        return
    # 判卷（对账闸门：无打卡记录的任务先标待确认，不判失约）
    done = partial = missed = pending_confirm = 0
    missed_list, confirm_list, deficit_by_goal = [], [], {}
    for day in plan.get("days", []):
        for t in day.get("tasks", []):
            std = float(t.get("quota_standard", 1) or 1)
            minimum = float(t.get("quota_minimum", std * 0.6) or std * 0.6)
            amts = amounts_of(data, t["id"])
            if not amts:
                if t.get("status") in ("pending", "pending_confirm"):
                    t["status"] = "pending_confirm"
                    pending_confirm += 1
                    confirm_list.append("%s %s" % (day["date"], t["title"]))
                continue
            total = sum(amts)
            j = judge(total, std, minimum)
            if j == "done":
                t["status"] = "done"; done += 1
            elif j == "partial":
                t["status"] = "partial"; partial += 1
                deficit = round(std - total, 2)
                if deficit > 0:
                    deficit_by_goal[t.get("goal_id")] = deficit_by_goal.get(t.get("goal_id"), 0) + deficit
            else:
                t["status"] = "missed"; missed += 1
                missed_list.append("%s %s" % (day["date"], t["title"]))
    if pending_confirm:
        save_data(dd, data)
        print("⚠ 周结算暂缓 %s：%d 项待对账（对账确认前不得判失约）" % (wid, pending_confirm))
        for c in confirm_list[:8]:
            print("  · " + c)
        print("→ 先补账：做了的用 checkin --date <日> --makeup 补登；确认没做的 checkin --amount 0")
        print("  补齐后重跑 settle 完成判卷与归档")
        return
    n = done + partial + missed
    rate = round((done + 0.5 * partial) / n, 2) if n else 0.0
    capped = any(float(t.get("rollover", 0) or 0) > float(t.get("quota_standard", 1) or 1)
                 for day in plan.get("days", []) for t in day.get("tasks", []))
    if rate >= 0.6 and not capped:
        level = "green"
    elif rate >= 0.4:
        level = "yellow"
    else:
        level = "red"
    # rollover 差额累计到 pending_rollover（新周计划吸收）
    pr = data["stats"].setdefault("pending_rollover", {})
    for g, v in deficit_by_goal.items():
        if g:
            pr[g] = round(pr.get(g, 0) + v, 2)
    # streak 清零：出现失约终态
    if missed:
        data["stats"]["streak_days"] = 0
    # borrow 核销
    for b in data["stats"].get("borrow_log", []):
        if b.get("week") == wid:
            b["repaid"] = True
    # stats + 归档
    data["stats"].setdefault("weekly_rates", []).append(
        {"week_id": wid, "rate": rate, "level": level})
    week_checkins = [c for c in data.get("checkins", []) if week_id_of(parse_date(c["date"])) == wid]
    write_json(os.path.join(dd, "logs", "archive", "%s.json" % wid),
               {"week_id": wid, "plan": plan, "checkins": week_checkins,
                "rate": rate, "level": level})
    data["weekly_plans"] = [p for p in data["weekly_plans"] if p.get("week_id") != wid]
    data["checkins"] = [c for c in data.get("checkins", []) if c not in week_checkins]
    data["phase"] = "calibrating"
    save_data(dd, data)
    # 输出
    print("周结算完成 %s ｜ 完成率 %.0f%% ｜ 诊断 %s" % (
        wid, rate * 100, {"green": "🟢轻度", "yellow": "🟡中度", "red": "🔴重度"}[level]))
    print("达成 %d · 部分 %d · 失约 %d%s" % (
        done, partial, missed, ("（失约清单: %s）" % "；".join(missed_list[:5])) if missed_list else ""))
    if confirm_list:
        print("⚠ 待对账 %d 项（确认前不判失约）: %s" % (pending_confirm, "；".join(confirm_list[:6])))
        print("  → 补账后重跑 settle 完成判卷")
    if deficit_by_goal:
        gnm = {g["id"]: g["name"] for g in data.get("goals", [])}
        print("差额滚动累计: %s（新周计划经 schedule-query 吸收）" %
              "、".join("%s +%.1f" % (gnm.get(g, g), v) for g, v in deficit_by_goal.items()))
    print("归档: logs/archive/%s.json ｜ streak=%d天" % (wid, data["stats"]["streak_days"]))
    render_core(dd, data)
    print("render 已刷新 → %s" % os.path.join(dd, "dashboard.html"))


# ── 子命令：rollover ───────────────────────────────────────

def cmd_rollover(args):
    dd = resolve_data_dir(args.data_dir)
    data = load_data(dd)
    today, _ = now_effective_date()
    d = parse_date(args.date) if args.date else today
    wid = args.week or week_id_of(d)
    plan = get_plan(data, wid)
    if not plan:
        die("周 %s 无计划，无可滚动" % wid)
    src_day = next((x for x in plan.get("days", []) if x["date"] == d2s(d)), None)
    if not src_day:
        die("%s 不在周 %s 计划内" % (d2s(d), wid))
    deficits = []
    for t in src_day.get("tasks", []):
        std = float(t.get("quota_standard", 1) or 1)
        amts = amounts_of(data, t["id"])
        total = sum(amts) if amts else 0
        if amts and 0 < total < std:
            deficits.append((t, round(std - total, 2)))
    if not deficits:
        print("当日无部分达成差额，无需滚动")
        return
    # 目标日：同周内 d 之后、负荷 <80% 的空闲日，就近优先
    mon = monday_of(d)
    cands = []
    for day in plan.get("days", []):
        dd_ = parse_date(day["date"])
        if dd_ > d:
            load = day_load_h(day)
            if load < 0.8 * DAY_CAPACITY_H:
                cands.append((dd_, day))
    cands.sort(key=lambda x: x[0])
    changes = []
    for t, deficit in deficits:
        left = deficit
        for dd_, day in cands:
            if left <= 0:
                break
            std = float(t.get("quota_standard", 1) or 1)
            cap = std                     # 封顶：单日新增 ≤ 单日标准（合计≤2倍标准）
            add = min(left, cap)
            if add <= 0:
                continue
            target = next((x for x in day.get("tasks", [])
                           if x.get("goal_id") == t.get("goal_id")
                           and x["title"].split(" ")[0] == t["title"].split(" ")[0]), None)
            if target is None:
                nt = {"id": new_task_id(data), "goal_id": t.get("goal_id"),
                      "title": t["title"], "type": "floating", "granularity": "fragment",
                      "time": None, "deadline": None, "quota_standard": std,
                      "quota_minimum": float(t.get("quota_minimum", std * 0.6)),
                      "urgency": t.get("urgency", "normal"), "rough": t.get("rough", False),
                      "rollover": round(add, 2), "adjustment_log": [], "status": "pending"}
                day.setdefault("tasks", []).append(nt)
            else:
                target["rollover"] = round(target.get("rollover", 0) + add, 2)
            changes.append("%s %s +%s（来自 %s 差额）" % (day["date"], target["title"] if target else t["title"], add, t["title"]))
            left = round(left - add, 2)
        if left > 0:
            pr = data["stats"].setdefault("pending_rollover", {})
            pr[t.get("goal_id")] = round(pr.get(t.get("goal_id"), 0) + left, 2)
            changes.append("剩余 %.1f 转入周级累计（新周吸收）" % left)
    save_data(dd, data)
    for c in changes:
        print("⚡ " + c)
    if not changes:
        print("本周无可用空闲日（负荷均≥80%）→ 差额转入周级累计")
    render_core(dd, data)


# ── 子命令：schedule-query ─────────────────────────────────

def cmd_schedule_query(args):
    dd = resolve_data_dir(args.data_dir)
    data = load_data(dd)
    today, _ = now_effective_date()
    d = parse_date(args.date) if args.date else today
    wid = args.week or week_id_of(d)
    mon = monday_of_weekid(wid) if "-W" in wid and len(wid.split("-W")[1]) <= 2 else monday_of(d)
    tt = load_timetable(dd)
    plan = get_plan(data, wid)
    print("排程查询 %s（%s ~ %s）%s" % (wid, d2s(mon), d2s(mon + timedelta(days=6)),
                                       "" if tt else "｜ ⚠ 无课表 → 粗排模式"))
    # ①空档表 + ②负荷
    days = plan.get("days", []) if plan else []
    for i in range(7):
        cur = mon + timedelta(days=i)
        day = next((x for x in days if x["date"] == d2s(cur)), None)
        slots = free_slots(day or {"date": d2s(cur), "tasks": []}, tt)
        load = day_load_h(day) if day else 0.0
        sl = "；".join("%s-%s(%s)" % (s, e, g) for s, e, dur, g in slots) or "无"
        courses = [b[2] for b in course_blocks_on(tt, cur)]
        print("  %s %s 负荷%.1fh｜课: %s｜空档: %s" % (
            WEEK_CN[cur.isoweekday()], cur.strftime("%m-%d"), load,
            ("、".join(courses) if courses else "无"), sl))
    # ③各目标近 3 天排程史
    hist = {}
    for p, day in all_days(data):
        if abs((parse_date(day["date"]) - today).days) <= 3:
            for t in day.get("tasks", []):
                if t.get("goal_id"):
                    hist.setdefault(t["goal_id"], []).append(day["date"])
    print("── 排程史（近3天）──")
    for g in data.get("goals", []):
        dates = hist.get(g["id"], [])
        print("  %s: %s" % (g["name"], ("、".join(sorted(set(dates)))) if dates else "⚠ 未排入（饥饿计数+1）"))
    # ④课表冲突
    conflicts = []
    for day in days:
        for t in day.get("tasks", []):
            if t.get("type") == "pinned" and t.get("time"):
                a1, a2 = [parse_hm(x) for x in t["time"].split("-")]
                for b1, b2, label in course_blocks_on(tt, parse_date(day["date"])):
                    if overlap(a1, a2, b1, b2):
                        conflicts.append("%s %s ↔ %s" % (day["date"], t["title"], label))
    print("── 课表冲突 ──\n  " + ("；".join(conflicts) if conflicts else "无"))
    # ⑤期限任务 + 待吸收 rollover
    dl = []
    for p, day in all_days(data):
        for t in day.get("tasks", []):
            if t.get("type") == "deadline" and t.get("deadline") and t.get("status") in ("pending", "partial", "pending_confirm"):
                days_left = (parse_date(t["deadline"]) - today).days
                dl.append("%s 截%s（%d天后）%s" % (t["title"], t["deadline"], days_left,
                                                   "⚠已逾期" if days_left < 0 else ""))
    print("── 期限任务 ──\n  " + ("；".join(dl) if dl else "无"))
    pr = data["stats"].get("pending_rollover", {})
    if any(v > 0 for v in pr.values()):
        gnames = {g["id"]: g["name"] for g in data.get("goals", [])}
        print("── 待吸收差额（rollover）──\n  " +
              "；".join("%s +%.1f" % (gnames.get(k, k), v) for k, v in pr.items() if v > 0))
    print("→ LLM 依此决策排程，排好后 schedule-validate --plan <json> 校验")


# ── 子命令：schedule-validate（--write 落盘） ──────────────

def cmd_schedule_validate(args):
    dd = resolve_data_dir(args.data_dir)
    data = load_data(dd)
    plan = read_payload(args.plan)
    if not plan.get("week_id") or not plan.get("days"):
        die("plan 需含 week_id 与 days[]")
    tt = load_timetable(dd)
    errors = []
    seen_dates = set()
    for day in plan.get("days", []):
        try:
            cur = parse_date(day["date"])
        except Exception:
            errors.append("[RED] 日期格式错误: %s" % day.get("date"))
            continue
        seen_dates.add(day["date"])
        pinned = [t for t in day.get("tasks", []) if t.get("type") == "pinned"]
        # ②每日固定任务 ≤4
        if len(pinned) > 4:
            errors.append("[RED] %s 固定任务 %d 项 >4" % (day["date"], len(pinned)))
        # ①时段冲突（课表 + pinned 互撞）
        tb = course_blocks_on(tt, cur)
        pt = []
        for t in pinned:
            if not t.get("time"):
                errors.append("[RED] %s pinned 任务缺 time: %s" % (day["date"], t.get("title")))
                continue
            try:
                a1, a2 = [parse_hm(x) for x in t["time"].split("-")]
            except Exception:
                errors.append("[RED] %s time 格式错误: %s" % (day["date"], t["time"]))
                continue
            pt.append((t, a1, a2))
            for b1, b2, label in tb:
                if overlap(a1, a2, b1, b2):
                    errors.append("[RED] %s %s 与课表 %s 冲突" % (day["date"], t["title"], label))
        # pinned 互撞
        pt.sort(key=lambda x: x[1])
        for i in range(len(pt) - 1):
            if pt[i][2] > pt[i + 1][1]:
                errors.append("[RED] %s %s 与 %s 时段重叠" % (day["date"], pt[i][0]["title"], pt[i + 1][0]["title"]))
        # ③连续学习块 ≤2h（间隔<15min 视为连续）
        run_start, run_end = None, None
        for t, a1, a2 in pt:
            if run_end is not None and a1 - run_end < 15:
                run_end = max(run_end, a2)
                if run_end - run_start > 120:
                    errors.append("[RED] %s 连续学习块 >2h（含 %s）——块间须插休息" % (day["date"], t["title"]))
            else:
                run_start, run_end = a1, a2
        # ④granularity 匹配：block 任务时长须 ≥1h
        for t in day.get("tasks", []):
            if t.get("granularity") == "block" and t.get("time"):
                a1, a2 = [parse_hm(x) for x in t["time"].split("-")]
                if a2 - a1 < 60:
                    errors.append("[RED] %s block 任务 %s 时长不足 1h" % (day["date"], t["title"]))
        # ⑤rollover 封顶
        for t in day.get("tasks", []):
            if float(t.get("rollover", 0) or 0) > float(t.get("quota_standard", 1) or 1):
                errors.append("[RED] %s %s 滚动量超封顶（>单日标准）" % (day["date"], t["title"]))
    # 天数完整性（7 天，可缺休息日？至少 5 天）
    if len(seen_dates) < 5:
        errors.append("[WARN] 仅 %d 天，建议覆盖周一~周日（休息日显式排入）" % len(seen_dates))
    for e in errors:
        if e.startswith("[RED]"):
            print(e)
    for e in errors:
        if e.startswith("[WARN]"):
            print(e)
    if any(e.startswith("[RED]") for e in errors):
        print("校验结果: FAIL（%d 项违规）→ 修正后重新校验，禁止落盘" %
              sum(1 for e in errors if e.startswith("[RED]")))
        return
    print("校验结果: PASS")
    if args.write:
        # 补全 task 字段
        for day in plan.get("days", []):
            for t in day.get("tasks", []):
                t.setdefault("id", new_task_id(data))
                std = float(t.get("quota_standard", 1) or 1)
                t.setdefault("quota_minimum", round(std * 0.6, 2))
                t.setdefault("urgency", "normal"); t.setdefault("rough", False)
                t.setdefault("rollover", 0); t.setdefault("adjustment_log", [])
                t.setdefault("status", "pending"); t.setdefault("goal_id", None)
                t.setdefault("granularity", "fragment"); t.setdefault("time", None)
                t.setdefault("deadline", None)
        plan.setdefault("goal_time_alloc", {})
        data["weekly_plans"] = [p for p in data.get("weekly_plans", [])
                                if p.get("week_id") != plan["week_id"]]
        data["weekly_plans"].append(plan)
        data["phase"] = "voyaging"
        # 吸收 pending_rollover
        pr = data["stats"].get("pending_rollover", {})
        absorbed = {}
        for day in plan.get("days", []):
            for t in day.get("tasks", []):
                if t.get("rollover", 0) > 0 and t.get("goal_id") in pr:
                    absorbed[t["goal_id"]] = True
        for g in absorbed:
            pr[g] = 0.0
        save_data(dd, data)
        print("已落盘 weekly_plans[%s]（%d 天 %d 任务）" % (
            plan["week_id"], len(plan.get("days", [])),
            sum(len(d.get("tasks", [])) for d in plan.get("days", []))))
        render_core(dd, data)
        print("render 已刷新 → %s" % os.path.join(dd, "dashboard.html"))


# ── 子命令：aggregate ──────────────────────────────────────

def read_frontmatter(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read()
        m = re.match(r"^---\n(.*?)\n---", txt, re.S)
        if not m:
            return {}
        out = {}
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip()
        return out
    except Exception:
        return {}


def cmd_aggregate(args):
    dd = resolve_data_dir(args.data_dir)
    data = load_data(dd)
    scope = args.scope
    today, _ = now_effective_date()
    if scope == "week":
        rates = data["stats"].get("weekly_rates", [])
        print("周完成率序列: " + (" → ".join("%s %.0f%%" % (r["week_id"], r["rate"] * 100) for r in rates) or "无"))
        return
    # month / year：读 weekly frontmatter + stats
    wdir = os.path.join(dd, "logs", "weekly")
    weeks = []
    if os.path.isdir(wdir):
        for fn in sorted(os.listdir(wdir)):
            fm = read_frontmatter(os.path.join(wdir, fn))
            if fm.get("week_id"):
                weeks.append(fm)
    rates = data["stats"].get("weekly_rates", [])
    print("== 聚合 scope=%s ==" % scope)
    print("周数: %d ｜ 均完成率: %.0f%%" % (
        len(rates), (sum(r["rate"] for r in rates) / len(rates) * 100) if rates else 0))
    if len(rates) >= 2:
        trend = "up" if rates[-1]["rate"] > rates[0]["rate"] else ("down" if rates[-1]["rate"] < rates[0]["rate"] else "flat")
        print("趋势: %s（%s → %s）" % (trend, rates[0]["week_id"], rates[-1]["week_id"]))
    # 目标时长分配
    alloc = {}
    for fm in weeks:
        try:
            gh = json.loads(fm.get("goal_hours", "{}"))
            for k, v in gh.items():
                alloc[k] = alloc.get(k, 0) + float(v)
        except Exception:
            pass
    if not alloc and data.get("weekly_plans"):
        for p in data["weekly_plans"]:
            for k, v in p.get("goal_time_alloc", {}).items():
                alloc[k] = alloc.get(k, 0) + float(v)
    gnames = {g["id"]: g["name"] for g in data.get("goals", [])}
    if alloc:
        total = sum(alloc.values()) or 1
        print("目标时间分配: " + " · ".join("%s %dh(%d%%)" % (
            gnames.get(k, k), v, round(v / total * 100)) for k, v in alloc.items()))
    # 里程碑
    ms = data.get("milestones", [])
    if ms:
        by = {}
        for m in ms:
            by[m.get("status", "pending")] = by.get(m.get("status", "pending"), 0) + 1
        print("里程碑: " + " · ".join("%s %d" % (STATUS_CN.get(k, k), v) for k, v in by.items()))
    # 作息画像（归档 + 现存 checkins）
    late = 0
    arch = os.path.join(dd, "logs", "archive")
    if os.path.isdir(arch):
        for fn in os.listdir(arch):
            try:
                with open(os.path.join(arch, fn), "r", encoding="utf-8") as f:
                    wk = json.load(f)
                for c in wk.get("checkins", []):
                    h = int(c.get("timestamp", "T23").split("T")[1][:2])
                    if h >= 23 or h < 5:
                        late += 1
            except Exception:
                pass
    for c in data.get("checkins", []):
        h = int(c.get("timestamp", "T23").split("T")[1][:2])
        if h >= 23 or h < 5:
            late += 1
    if late:
        print("作息画像: %d 次 23:00 后打卡%s" % (late, " ⚠ 熬夜苗头，建议任务前移" if late >= 5 else ""))
    if scope == "year":
        st = data["stats"]
        print("最长 streak（当前口径）: %d 天 ｜ 目标终态: %s" % (
            st.get("streak_days", 0),
            " · ".join("%s[%s]" % (g["name"], STATUS_CN.get(g.get("status"), g.get("status")))
                       for g in data.get("goals", []))))


# ── 子命令：render ─────────────────────────────────────────

def template_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "assets", "compass-dashboard", "template.html")


def build_payload(dd, data, as_of=None):
    if as_of:
        try:
            today = parse_date(as_of)
        except ValueError:
            die("--as-of 日期格式应为 YYYY-MM-DD：%s" % as_of)
    else:
        today, _ = now_effective_date()
    sem = data.get("semester", {})
    try:
        s, e = parse_date(sem["start"]), parse_date(sem["end"])
        weeks_total = (e - s).days // 7 + 1
        week_now = max(1, min((today - s).days // 7 + 1, weeks_total))
        progress = max(0.0, min(1.0, (today - s).days / max(1, (e - s).days)))
    except Exception:
        s = e = None; weeks_total = week_now = 0; progress = 0.0
    # goal health
    goal_health = []
    hours = {g["id"]: 0.0 for g in data.get("goals", [])}
    arch = os.path.join(dd, "logs", "archive")
    if os.path.isdir(arch):
        for fn in sorted(os.listdir(arch)):
            try:
                with open(os.path.join(arch, fn), "r", encoding="utf-8") as f:
                    wk = json.load(f)
                rate = wk.get("rate", 0)
                for gid, h in (wk.get("plan", {}).get("goal_time_alloc") or {}).items():
                    hours[gid] = hours.get(gid, 0) + float(h) * rate
            except Exception:
                pass
    for p in data.get("weekly_plans", []):
        done = sum(1 for d in p.get("days", []) for t in d.get("tasks", [])
                   if t.get("status") in ("done", "partial"))
        n = sum(len(d.get("tasks", [])) for d in p.get("days", []))
        r = (done / n) if n else 0
        for gid, h in (p.get("goal_time_alloc") or {}).items():
            hours[gid] = hours.get(gid, 0) + float(h) * r
    for g in data.get("goals", []):
        ms = [m for m in data.get("milestones", []) if m.get("goal_id") == g["id"]]
        # 进度计分：达成=1，进行中=0.5，待闯/延期=0 —— 已开工的目标不应显示 0%
        score = sum(1.0 if m.get("status") == "achieved" else (0.5 if m.get("status") == "in_progress" else 0.0)
                    for m in ms)
        ach = sum(1 for m in ms if m.get("status") == "achieved")
        inp = sum(1 for m in ms if m.get("status") == "in_progress")
        goal_health.append({"id": g["id"], "name": g["name"], "status": g.get("status", "active"),
                            "urgency": g.get("urgency", "normal"), "budget": g.get("weekly_budget_h", 0),
                            "metric": g.get("metric", ""), "progress": round(score / len(ms), 2) if ms else 0.0,
                            "progress_parts": ("%d达成·%d进行中" % (ach, inp)) if (ach or inp) else ("" if not ms else "未开工"),
                            "hours_done": round(hours.get(g["id"], 0), 1),
                            "milestones": [{"name": m["name"], "due": m.get("due", ""),
                                            "status": m.get("status", "pending")} for m in ms]})
    timeline = sorted(({"goal": next((g["name"] for g in data.get("goals", []) if g["id"] == m.get("goal_id")), "?"),
                        "name": m["name"], "due": m.get("due", ""), "status": m.get("status", "pending")}
                       for m in data.get("milestones", [])), key=lambda x: x["due"])
    wid = week_id_of(today)
    plan = get_plan(data, wid) or (data["weekly_plans"][-1] if data.get("weekly_plans") else None)
    current_week = None
    if plan:
        current_week = {"week_id": plan["week_id"],
                        "week_label": teach_week_label(plan["week_id"], sem.get("start")),
                        "days": [
            {"date": d["date"], "tasks": [
                {"title": t["title"], "type": t.get("type", "floating"),
                 "time": t.get("time"), "status": t.get("status", "pending"),
                 "rough": bool(t.get("rough")), "rollover": t.get("rollover", 0)}
                for t in d.get("tasks", [])]} for d in plan.get("days", [])]}
    rates = data["stats"].get("weekly_rates", [])
    # 目标时间分配：归档周 + 活跃周都统计（周结算归档后历史仍应进入饼图）
    alloc = {}
    arch = os.path.join(dd, "logs", "archive")
    if os.path.isdir(arch):
        for fn in sorted(os.listdir(arch)):
            try:
                with open(os.path.join(arch, fn), "r", encoding="utf-8") as f:
                    wk = json.load(f)
                for gid, v in (wk.get("plan", {}).get("goal_time_alloc") or {}).items():
                    alloc[gnames(gid, data)] = round(alloc.get(gnames(gid, data), 0) + float(v), 1)
            except Exception:
                pass
    for p in data.get("weekly_plans", []):
        for gid, v in (p.get("goal_time_alloc") or {}).items():
            alloc[gnames(gid, data)] = round(alloc.get(gnames(gid, data), 0) + float(v), 1)
    logs_index = []
    for sub, typ in (("daily", "daily"), ("weekly", "weekly"), ("monthly", "monthly"), ("yearly", "yearly")):
        p = os.path.join(dd, "logs", sub)
        if os.path.isdir(p):
            for fn in sorted(os.listdir(p), reverse=True)[:12]:
                item = {"type": typ, "file": fn, "date": fn[:-3]}
                if typ == "weekly":
                    # 显示层教学周标签（文件名保持 ISO 周号存储主键）
                    item["label"] = teach_week_label(fn[:-3], sem.get("start"))
                logs_index.append(item)
    # 周报板块：取最近一次结算周的数据（归档优先，活跃计划未结算时无周报）
    week_report = build_week_report(dd, data, rates)
    return {
        "meta": {"generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                 "schema_version": SCHEMA_VERSION, "template_version": TEMPLATE_VERSION},
        "phase": data["phase"],
        "semester": {"start": sem.get("start"), "end": sem.get("end"),
                     "progress": round(progress, 2), "weeks_total": weeks_total, "week_now": week_now},
        "compass": {"streak_days": data["stats"].get("streak_days", 0),
                    "overall_rate": round((sum(r["rate"] for r in rates) / len(rates)) if rates else 0, 2),
                    "goal_health": goal_health},
        "timeline": timeline,
        "current_week": current_week,
        "week_report": week_report,
        "charts": {"weekly_rates": [{"label": teach_week_label(r["week_id"], sem.get("start")),
                                     "week_id": r["week_id"], "rate": r["rate"], "level": r["level"]} for r in rates],
                   "goal_pie": [{"label": k, "value": v} for k, v in alloc.items() if v],
                   "trend": [r["rate"] for r in rates]},
        "logs_index": logs_index,
        "logs_base": dd.rstrip("\\/") + os.sep,
    }


def build_week_report(dd, data, rates):
    """从最近归档周提取周报数据（结算+诊断+司南建议，供仪表盘周报板块）。"""
    arch = os.path.join(dd, "logs", "archive")
    if not os.path.isdir(arch) or not rates:
        return None
    latest = rates[-1]
    wid = latest["week_id"]
    sem_start = (data.get("semester") or {}).get("start")
    # 周报建议：优先读 LLM 写的周总结日志（frontmatter 之后正文的司南建议）；
    # 无周总结时脚本给基础建议
    suggestions = []
    wp = os.path.join(dd, "logs", "weekly", "%s.md" % wid)
    if os.path.exists(wp):
        try:
            with open(wp, "r", encoding="utf-8") as f:
                content = f.read()
            m = re.search(r"##\s*💡?\s*司南建议(.*?)(?=\n##\s|\Z)", content, re.S)
            if m:
                for line in m.group(1).strip().splitlines():
                    if line.lstrip().startswith(">"):
                        continue  # 跳过引用/水印行（如平台 AIGC 尾注）
                    line = re.sub(r"^[\s·\-•]+", "", line).strip()
                    if line and len(line) > 4:
                        suggestions.append(line[:80])
        except Exception:
            pass
    if not suggestions:
        lvl = latest["level"]
        base = {"green": "航线健康，保持当前节奏即可",
                "yellow": "完成率中等，建议下周重排计划时降低单日任务密度",
                "red": "偏离较大，建议安排一次深度复盘对话（对我说\"复盘本周\"）"}
        suggestions = [base.get(lvl, "保持节奏")]
    # 归档数据
    stats = {"done": 0, "partial": 0, "missed": 0, "rollover_total": 0.0, "range": ""}
    try:
        with open(os.path.join(arch, "%s.json" % wid), "r", encoding="utf-8") as f:
            wk = json.load(f)
        for day in wk.get("plan", {}).get("days", []):
            if not stats["range"]:
                stats["range"] = day["date"]
            stats["range_end"] = day["date"]
            for t in day.get("tasks", []):
                st = t.get("status")
                if st == "done":
                    stats["done"] += 1
                elif st == "partial":
                    stats["partial"] += 1
                elif st == "missed":
                    stats["missed"] += 1
                stats["rollover_total"] += float(t.get("rollover", 0) or 0)
        if stats.get("range_end"):
            stats["range"] = "%s ~ %s" % (stats["range"], stats["range_end"])
    except Exception:
        pass
    verdicts = {"green": "完成率达标且欠账未超限——航线健康，维持航向",
                "yellow": "完成率中等或滚动欠账触顶——下周需要重排任务密度",
                "red": "完成率显著低于目标——建议深度复盘，检查目标预算与现实的差距"}
    return {"week_id": wid, "week_label": teach_week_label(wid, sem_start),
            "rate": latest["rate"], "level": latest["level"],
            "verdict": verdicts.get(latest["level"], ""),
            "done": stats["done"], "partial": stats["partial"], "missed": stats["missed"],
            "rollover_total": round(stats["rollover_total"], 1) if stats["rollover_total"] else 0,
            "streak": data["stats"].get("streak_days", 0),
            "range": stats["range"], "suggestions": suggestions[:4]}


def gnames(gid, data):
    return next((g["name"] for g in data.get("goals", []) if g["id"] == gid), gid or "其他")


def render_core(dd, data, as_of=None):
    tp = template_path()
    if not os.path.exists(tp):
        die("模板缺失: %s" % tp)
    with open(tp, "r", encoding="utf-8") as f:
        html = f.read()
    payload = build_payload(dd, data, as_of=as_of)
    token = "/*__UCOMPASS_DATA__*/null"
    if token not in html:
        die("模板缺少数据占位符 %s" % token)
    m = re.search(r"TEMPLATE_VERSION\s*=\s*['\"]([^'\"]+)['\"]", html)
    tv = m.group(1) if m else "?"
    warn = ""
    if tv != TEMPLATE_VERSION:
        warn = " ⚠ 模板版本(%s)与引擎预期(%s)不一致，建议重建仪表盘" % (tv, TEMPLATE_VERSION)
    html = html.replace(token, json.dumps(payload, ensure_ascii=False))
    out = os.path.join(dd, "dashboard.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out, warn


def cmd_render(args):
    dd = resolve_data_dir(args.data_dir)
    data = load_data(dd)
    as_of = getattr(args, "as_of", None)
    out, warn = render_core(dd, data, as_of=as_of)
    if as_of:
        print("虚拟今天: %s（进度与当前周按此日期计算）" % as_of)
    print("仪表盘已生成: %s%s" % (out, warn))
    print("六板块: 罗盘总览 · 航程时间轴 · 目标航线 · 科学图表 · 本周计划 · 日志时间轴")
    print("→ 双击打开即可（单文件自包含，可分享）")


# ── 子命令：archive ────────────────────────────────────────

def cmd_archive(args):
    dd = resolve_data_dir(args.data_dir)
    data = load_data(dd)
    sem = data.get("semester", {})
    sid = sem.get("id", "unknown")
    print("== 学期归档素材（aggregate --scope year）==")
    args.scope = "year"
    cmd_aggregate(args)
    data["phase"] = "archived"
    save_data(dd, data)
    target_root = os.path.join(os.path.dirname(dd), "archive")
    target = os.path.join(target_root, sid)
    if os.path.exists(target):
        die("归档目录已存在: %s（防覆盖，请人工处理）" % target)
    os.makedirs(target_root, exist_ok=True)
    shutil.move(dd, target)
    print("归档完成: %s → %s" % (dd, target))
    print("→ 引导学生重新定锚（init），新学期目录将重新创建")


# ── 子命令：demo-init ──────────────────────────────────────

def cmd_demo_init(args):
    dd = resolve_data_dir(args.data_dir)
    if os.path.exists(compass_path(dd)) and not args.force:
        die("数据目录已有 compass.json，加 --force 覆盖重建演示数据")
    if os.path.exists(dd):
        has_compass = os.path.exists(compass_path(dd))
        entries = os.listdir(dd)
        if has_compass or not entries:
            shutil.rmtree(dd)
        else:
            die("目录 %s 非空且无 compass.json，拒绝覆盖（防误删）" % dd)
    ensure_layout(dd)
    today, _ = now_effective_date()
    mon = monday_of(today)
    start = mon - timedelta(weeks=7)
    end = start + timedelta(days=132)
    data = {
        "version": SCHEMA_VERSION, "phase": "voyaging",
        "semester": {"id": "2026S1", "start": d2s(start), "end": d2s(end),
                     "exam_weeks": [[d2s(end - timedelta(days=21)), d2s(end - timedelta(days=8))]]},
        "goals": [
            {"id": "g1", "name": "英语六级 520", "metric": "12月真题模考 ≥520",
             "evidence": "模考成绩单截图", "weekly_budget_h": 6, "urgency": "high",
             "version": 1, "status": "active", "history": [], "dormant_weeks": 0},
            {"id": "g2", "name": "街舞入门", "metric": "学会 3 支完整 routine",
             "evidence": "社团汇报演出视频", "weekly_budget_h": 3, "urgency": "normal",
             "version": 1, "status": "active", "history": [], "dormant_weeks": 0},
            {"id": "g3", "name": "高数筑基 85+", "metric": "期末 ≥85",
             "evidence": "期末成绩单", "weekly_budget_h": 5, "urgency": "high",
             "version": 1, "status": "active", "history": [], "dormant_weeks": 0},
        ],
        "milestones": [
            {"id": "m1", "goal_id": "g1", "name": "真题一轮完成", "due": d2s(start + timedelta(days=40)),
             "criterion": "近5年真题做完并订正", "status": "achieved",
             "verified_at": d2s(start + timedelta(days=40)), "new_due": None, "task_refs": []},
            {"id": "m2", "goal_id": "g1", "name": "词汇二轮到位", "due": d2s(today + timedelta(days=35)),
             "criterion": "高频词二轮背诵完成", "status": "in_progress",
             "verified_at": None, "new_due": None, "task_refs": []},
            {"id": "m3", "goal_id": "g1", "name": "模考稳定 520", "due": d2s(today + timedelta(days=60)),
             "criterion": "连续两次模考 ≥520", "status": "pending",
             "verified_at": None, "new_due": None, "task_refs": []},
            {"id": "m4", "goal_id": "g2", "name": "routine 1 学成", "due": d2s(start + timedelta(days=53)),
             "criterion": "能独立完成 routine 1", "status": "achieved",
             "verified_at": d2s(start + timedelta(days=53)), "new_due": None, "task_refs": []},
            {"id": "m5", "goal_id": "g2", "name": "routine 2~3", "due": d2s(today + timedelta(days=42)),
             "criterion": "两支新 routine 动作到位", "status": "in_progress",
             "verified_at": None, "new_due": None, "task_refs": []},
            {"id": "m6", "goal_id": "g3", "name": "前四章错题清零", "due": d2s(today + timedelta(days=20)),
             "criterion": "错题本前四章全部重做", "status": "in_progress",
             "verified_at": None, "new_due": None, "task_refs": []},
        ],
        "weekly_plans": [], "checkins": [],
        "stats": {"streak_days": 0, "last_encourage_idx": -1, "recent_quotes": [],
                  "weekly_rates": [], "borrow_log": [], "last_streak_date": None,
                  "pending_rollover": {}},
        "_seq": 100,
    }
    write_json(os.path.join(dd, "timetable", "timetable.json"), {
        "source": "demo", "parsed_at": datetime.now().isoformat(timespec="seconds"),
        "weekly": [
            {"day": 1, "period": 1, "start": "08:00", "end": "09:40", "course": "高等数学A(上)", "room": "教三405", "weeks": "1-16"},
            {"day": 2, "period": 3, "start": "14:00", "end": "15:40", "course": "数据结构", "room": "教二210", "weeks": "1-16"},
            {"day": 4, "period": 2, "start": "10:00", "end": "11:40", "course": "大学英语", "room": "外语楼301", "weeks": "1-16"},
        ],
        "exceptions": [{"date": d2s(today + timedelta(days=17)), "type": "exam", "title": "高数期中测验"}],
    })
    # 生成 3 个已结算周 + 当前周
    rates_plan = {0: 0.80, 1: 0.45, 2: 0.25}   # 三周完成率：绿/黄/红（确定性）
    task_templates = [
        ("g1", "六级真题精读", "deadline", "block", "19:00-20:30", None, 1.0),
        ("g1", "睡前背 50 词", "floating", "fragment", None, None, 50),
        ("g3", "高数错题整理", "deadline", "fragment", "16:30-17:30", None, 10),
        ("g2", "街舞社 routine", "pinned", "block", "14:00-15:30", None, 1),
    ]
    for wk in range(4):
        wstart = mon - timedelta(weeks=3 - wk)
        wid = week_id_of(wstart)
        target_rate = rates_plan.get(wk, 0.75)
        plan = {"week_id": wid, "goal_time_alloc": {"g1": 6, "g2": 3, "g3": 5}, "days": []}
        for i in range(7):
            cur = wstart + timedelta(days=i)
            if cur > today:
                break
            tasks = []
            for gid, title, typ, gran, tm, dl, std in task_templates:
                if typ == "pinned" and cur.isoweekday() != 3:
                    continue
                t = {"id": new_task_id(data), "goal_id": gid, "title": title, "type": typ,
                     "granularity": gran, "time": tm if typ != "floating" else None,
                     "deadline": d2s(cur + timedelta(days=2)) if typ == "deadline" else None,
                     "quota_standard": std, "quota_minimum": round(std * 0.6, 2),
                     "urgency": "high" if gid in ("g1", "g3") else "normal", "rough": False,
                     "rollover": 0, "adjustment_log": [], "status": "pending"}
                tasks.append(t)
            if tasks:
                plan["days"].append({"date": d2s(cur), "tasks": tasks})
        data["weekly_plans"].append(plan)
        # 模拟打卡（确定性比例：done→partial→missed，保证三色周稳定可复现）
        flat = []
        for day in plan["days"]:
            dcur = parse_date(day["date"])
            if dcur >= today:
                continue
            for t in day["tasks"]:
                flat.append((dcur, t))
        total = max(1, len(flat))
        for i, (dcur, t) in enumerate(flat):
            std = float(t["quota_standard"])
            r = i / total
            if r < target_rate:
                amt = std
            elif r < target_rate + 0.2:
                amt = round(std * 0.7, 1)
            else:
                amt = 0
            note = ("demo:%s" % t["title"]) if amt > 0 else "没做"
            checkin_core(data, dd, note, t["title"], amt,
                         d2s(dcur), False,
                         ts="%sT22:%02d:00" % (d2s(dcur), 20 + (i % 30)))
        # 历史周结算
        if wk < 3:
            _settle_week(data, dd, wid)
            rate = data["stats"]["weekly_rates"][-1]["rate"]
            # 写周日志（frontmatter + 简短正文）
            gh = {"g1": 6 * rate, "g2": 3 * rate, "g3": 5 * rate}
            append_file(os.path.join(dd, "logs", "weekly", "%s.md" % wid),
                        "---\nweek_id: %s\nrate: %.2f\nlevel: %s\ngoal_hours: %s\n---\n\n# %s 航海日志\n\n演示周记录：完成率 %.0f%%。\n" % (
                            wid, rate, data["stats"]["weekly_rates"][-1]["level"],
                            json.dumps(gh, ensure_ascii=False), wid, rate * 100))
    # 月日志（上月）
    m_prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    rates = data["stats"]["weekly_rates"]
    avg = sum(r["rate"] for r in rates) / len(rates) if rates else 0
    append_file(os.path.join(dd, "logs", "monthly", "%s.md" % m_prev),
                "---\nmonth_id: %s\navg_rate: %.2f\ntrend: up\n---\n\n# %s 航线月报（演示）\n\n均完成率 %.0f%%，趋势向上。\n" % (
                    m_prev, avg, m_prev, avg * 100))
    save_data(dd, data)
    out, warn = render_core(dd, data)
    print("演示学期已生成 ✓（3 目标 · 6 里程碑 · 4 周计划与打卡 · 四级日志）")
    print("数据目录: %s" % dd)
    print("周完成率: " + " → ".join("%.0f%%" % (r["rate"] * 100) for r in rates) + "（🟢🟡🔴三色周，演示三级诊断与休眠素材）")
    print("仪表盘: %s%s" % (out, warn))
    print("→ 打开 dashboard.html 查看六板块；用 status 查看当前状态")


def _settle_week(data, dd, wid):
    """demo 内部复用的结算（同 cmd_settle 核心，跳过输出）。"""
    plan = get_plan(data, wid)
    if not plan:
        return
    done = partial = missed = 0
    deficit_by_goal = {}
    for day in plan.get("days", []):
        for t in day.get("tasks", []):
            std = float(t.get("quota_standard", 1) or 1)
            minimum = float(t.get("quota_minimum", std * 0.6) or std * 0.6)
            amts = amounts_of(data, t["id"])
            if not amts:
                continue
            j = judge(sum(amts), std, minimum)
            if j == "done":
                t["status"] = "done"; done += 1
            elif j == "partial":
                t["status"] = "partial"; partial += 1
                deficit_by_goal[t.get("goal_id")] = deficit_by_goal.get(t.get("goal_id"), 0) + (std - sum(amts))
            else:
                t["status"] = "missed"; missed += 1
    n = done + partial + missed
    rate = round((done + 0.5 * partial) / n, 2) if n else 0.0
    level = "green" if rate >= 0.6 else ("yellow" if rate >= 0.4 else "red")
    if missed:
        data["stats"]["streak_days"] = 0
    pr = data["stats"].setdefault("pending_rollover", {})
    for g, v in deficit_by_goal.items():
        if g:
            pr[g] = round(pr.get(g, 0) + v, 2)
    data["stats"]["weekly_rates"].append(
        {"week_id": wid, "rate": rate, "level": level})
    week_checkins = [c for c in data.get("checkins", []) if week_id_of(parse_date(c["date"])) == wid]
    write_json(os.path.join(dd, "logs", "archive", "%s.json" % wid),
               {"week_id": wid, "plan": plan, "checkins": week_checkins, "rate": rate, "level": level})
    data["weekly_plans"] = [p for p in data["weekly_plans"] if p.get("week_id") != wid]
    data["checkins"] = [c for c in data["checkins"] if c not in week_checkins]


# ── main ───────────────────────────────────────────────────

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=argparse.SUPPRESS,
                        help="数据目录（默认 ./ucompass-data 或 UCOMPASS_DATA_DIR）")
    p = argparse.ArgumentParser(prog="compass_store.py", description="你的司南UCompass 航海日志引擎")
    p.add_argument("--data-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", parents=[common], help="会话开头状态摘要")
    s.set_defaults(func=cmd_status)
    s = sub.add_parser("init", parents=[common], help="定锚落盘 / 结构化更新")
    s.add_argument("--payload", required=True, help="JSON 字符串或 @file.json")
    s.add_argument("--update", action="store_true", help="增量更新模式（goals/milestones/semester/timetable）")
    s.set_defaults(func=cmd_init)
    s = sub.add_parser("checkin", parents=[common], help="一句话打卡")
    s.add_argument("--text", default="", help="打卡原话")
    s.add_argument("--task", default=None, help="指定任务（id 或标题，可省）")
    s.add_argument("--amount", default=None, help="显式量：数字或 50%%")
    s.add_argument("--date", default=None, help="补卡日期 YYYY-MM-DD")
    s.add_argument("--makeup", action="store_true", help="标记为补卡")
    s.set_defaults(func=cmd_checkin)
    s = sub.add_parser("settle", parents=[common], help="周结算（生成新周计划前强制）")
    s.add_argument("--week", default=None, help="周号 YYYY-Www（默认上一完成周）")
    s.set_defaults(func=cmd_settle)
    s = sub.add_parser("rollover", parents=[common], help="日级滚动补载")
    s.add_argument("--date", default=None, help="差额来源日 YYYY-MM-DD")
    s.add_argument("--week", default=None, help="周号（与 --date 二选一）")
    s.set_defaults(func=cmd_rollover)
    s = sub.add_parser("schedule-query", parents=[common], help="排程查询（脚本查询，LLM 决策）")
    s.add_argument("--week", default=None, help="周号 YYYY-Www")
    s.add_argument("--date", default=None, help="以该日所在周为准")
    s.set_defaults(func=cmd_schedule_query)
    s = sub.add_parser("schedule-validate", parents=[common], help="排程校验（PASS 后可 --write 落盘）")
    s.add_argument("--plan", required=True, help="拟落盘周计划 JSON 或 @file.json")
    s.add_argument("--write", action="store_true", help="校验通过后写入 weekly_plans 并 render")
    s.set_defaults(func=cmd_schedule_validate)
    s = sub.add_parser("aggregate", parents=[common], help="聚合（周/月/年）")
    s.add_argument("--scope", default="month", choices=["week", "month", "year"])
    s.add_argument("--from", dest="from_d", default=None)
    s.add_argument("--to", dest="to_d", default=None)
    s.set_defaults(func=cmd_aggregate)
    s = sub.add_parser("render", parents=[common], help="生成 dashboard.html")
    s.add_argument("--as-of", default=None, help="虚拟今天 YYYY-MM-DD（归档/演示数据重演用，默认真实今天）")
    s.set_defaults(func=cmd_render)
    s = sub.add_parser("archive", parents=[common], help="学期归档")
    s.set_defaults(func=cmd_archive)
    s = sub.add_parser("demo-init", parents=[common], help="生成演示学期数据")
    s.add_argument("--force", action="store_true", help="覆盖已有数据重建")
    s.set_defaults(func=cmd_demo_init)
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    setattr(args, "data_dir", getattr(args, "data_dir", None))
    args.func(args)


if __name__ == "__main__":
    main()
