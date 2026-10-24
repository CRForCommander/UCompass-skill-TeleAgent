#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""timetable_parse.py — Excel/CSV 课表解析器（你的司南UCompass）

把学生课表（xlsx/csv）解析为 timetable.json（字段契约见 references/api_reference.md §4）。
纯标准库：xlsx 经 zipfile + XML 解析，csv 自动尝试 utf-8/gbk。

用法：
  python timetable_parse.py 课表.xlsx --out <ucompass-data>/timetable/timetable.json [--weeks 1-20]
解析失败或格式过于怪异时，退出码 2，并提示降级为对话口述录入（见 SKILL.md 航行阶段）。
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

DEFAULT_PERIODS = {
    1: ("08:00", "09:40"), 2: ("10:00", "11:40"), 3: ("14:00", "15:40"),
    4: ("16:00", "17:40"), 5: ("19:00", "20:40"), 6: ("21:00", "21:50"),
}

DAY_PATTERNS = [
    (re.compile(r"^周\s*(一|二|三|四|五|六|日|天)$"), "1234567"),
    (re.compile(r"^星期\s*(一|二|三|四|五|六|日|天)$"), "1234567"),
    (re.compile(r"^礼拜\s*(一|二|三|四|五|六|日|天)$"), "1234567"),
    (re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)$", re.I), "MonTueWedThuFriSatSun"),
]
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7,
          "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6, "Sun": 7}
PERIOD_RE = re.compile(r"第\s*(\d+)\s*(?:-\s*\d+)?\s*(?:节|大节|节课)")
TIME_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-~]\s*(\d{1,2}:\d{2})")
WEEK_RANGE_RE = re.compile(r"(\d+)\s*[-~]\s*(\d+)\s*周")
WEEK_SINGLE_RE = re.compile(r"^(\d+)\s*周$")
ROOM_RE = re.compile(
    r"(教[一二三四五六七八九十]?\s?\d{2,4}|[东西南北AB]?\s?楼\s?\d{0,4}|\d+\s?号楼|"
    r"[A-Z]\s?区\s?\d{2,4}|实验楼\s?\d*|[A-Z]\s?栋\s?\d{0,4}|机房|操场|体育馆|舞蹈房|工作室)")
ROOM_ONLY_RE = re.compile(
    r"^(?:[A-Z]{1,2}\d{2,4}|\d{4}|[A-Z]\s?楼\s?\d{0,4}|文[A-Z]?\d{2,4}|南[AB]?\d{2,4}|北[AB]?\d{2,4})$")


def die(msg, code=1):
    print("[ERROR] " + msg)
    sys.exit(code)


# ── xlsx 读取（零依赖） ────────────────────────────────────

def col_index(ref):
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


def read_xlsx(path):
    try:
        zf = zipfile.ZipFile(path)
    except Exception as e:
        die("无法打开 xlsx：%s" % e, 2)
    names = zf.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in root:
            text = "".join(t.text or "" for t in si.iter()
                           if t.tag.endswith("}t") or t.tag == "t")
            shared.append(text)
    sheet_name = None
    for n in names:
        if re.match(r"xl/worksheets/sheet1?\.xml$", n):
            sheet_name = n
            break
    if sheet_name is None:
        for n in names:
            if re.match(r"xl/worksheets/.*\.xml$", n):
                sheet_name = n
                break
    if sheet_name is None:
        die("xlsx 中未找到工作表（xl/worksheets/*.xml）", 2)
    root = ET.fromstring(zf.read(sheet_name))
    grid = {}
    max_row = max_col = 0
    for c in root.iter():
        if not (c.tag.endswith("}c") or c.tag == "c"):
            continue
        ref = c.get("r", "")
        if not ref:
            continue
        m = re.match(r"([A-Z]+)(\d+)", ref)
        if not m:
            continue
        ci, ri = col_index(m.group(1)), int(m.group(2))
        val = ""
        if c.get("t") == "s":
            v = next((x for x in c.iter() if x.tag.endswith("}v") or x.tag == "v"), None)
            if v is not None and v.text is not None:
                idx = int(v.text)
                val = shared[idx] if idx < len(shared) else ""
        elif c.get("t") == "inlineStr":
            val = "".join(t.text or "" for t in c.iter()
                          if t.tag.endswith("}t") or t.tag == "t")
        else:
            v = next((x for x in c.iter() if x.tag.endswith("}v") or x.tag == "v"), None)
            val = (v.text or "") if v is not None else ""
        if val != "":
            grid[(ri, ci)] = val.strip()
            max_row = max(max_row, ri)
            max_col = max(max_col, ci)
    return [[grid.get((r, c), "") for c in range(max_col + 1)] for r in range(1, max_row + 2)]


def read_csv(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        die("CSV 编码无法识别（utf-8/gbk 均失败）", 2)
    rows = list(csv.reader(io.StringIO(text)))
    return [[(c or "").strip() for c in row] for row in rows if any(c.strip() for c in row)]


# ── 结构识别 ───────────────────────────────────────────────

def detect_day_columns(header_row):
    """返回 {列号: day(1~7)}；识别失败返回 {}。"""
    cols = {}
    for i, cell in enumerate(header_row):
        for pat, order in DAY_PATTERNS:
            m = pat.match(cell.strip())
            if m:
                key = m.group(1)
                if key in CN_NUM:
                    cols[i] = CN_NUM[key]
                break
    return cols


def detect_period(text):
    """行首列 → (period 序号, start, end) 或 None。"""
    t = text.strip()
    m = PERIOD_RE.search(t)
    if m:
        p = int(m.group(1))
        tm = TIME_RE.search(t)
        if tm:
            return p, tm.group(1), tm.group(2)
        return p, *DEFAULT_PERIODS.get(p, ("08:00", "09:40"))
    tm = TIME_RE.search(t)
    if tm and re.match(r"^\d{1,2}:\d{2}", t):
        hour = int(tm.group(1).split(":")[0])
        for p, (s, _) in DEFAULT_PERIODS.items():
            if int(s.split(":")[0]) == hour:
                return p, tm.group(1), tm.group(2)
    return None


def parse_course_cell(text, default_weeks):
    """课格文本 → (course, room, weeks)；无法解析时 course 原样、room=None。"""
    t = text.replace("\r", "\n").strip()
    parts = [p.strip() for p in re.split(r"[\n@/]", t) if p.strip()]
    weeks = default_weeks
    room = None
    course_parts = []
    for p in parts:
        if WEEK_RANGE_RE.search(p):
            m = WEEK_RANGE_RE.search(p)
            weeks = "%s-%s" % (m.group(1), m.group(2))
            p = WEEK_RANGE_RE.sub("", p).strip()
            if not p:
                continue
        if re.fullmatch(r"单周", p):
            weeks = ",".join(str(i) for i in range(1, 40, 2)); continue
        if re.fullmatch(r"双周", p):
            weeks = ",".join(str(i) for i in range(2, 40, 2)); continue
        if re.fullmatch(r"(\d+)\s*周", p):
            weeks = WEEK_SINGLE_RE.match(p).group(1); continue
        if room is None and (ROOM_RE.search(p) or ROOM_ONLY_RE.fullmatch(p)):
            rm = ROOM_RE.search(p) or ROOM_ONLY_RE.fullmatch(p)
            room = rm.group(1).replace(" ", "")
            # 只在教室匹配"独立段"时才剥离：教室前后需是空格/行首行尾，
            # 防止把"教三405"嵌在课程名里时误剥
            rest = p.replace(room, " ", 1).strip(" ()（）-—") if re.search(
                r"(?<![\w(（])" + re.escape(room) + r"(?![\w)）])", p) else p.strip(" ()（）-—")
            if rest:
                course_parts.append(rest)
            continue
        course_parts.append(p)
    course = " ".join(course_parts).strip() or text.strip()[:20]
    course = re.sub(r"\(\s*\)|（\s*）", "", course).strip(" -—/@")
    # 课程名里的半角括号平衡修补：如"高等数学A(上" → "高等数学A(上)"
    if "(" in course and ")" not in course:
        course += ")"
    if "（" in course and "）" not in course:
        course += "）"
    return course, room, weeks


def main():
    ap = argparse.ArgumentParser(prog="timetable_parse.py", description="课表解析器")
    ap.add_argument("input", help="课表文件 xlsx/csv")
    ap.add_argument("--out", default=None, help="输出 timetable.json 路径")
    ap.add_argument("--weeks", default="1-20", help="默认适用周次（格内未标注时）")
    args = ap.parse_args()

    if not os.path.exists(args.input):
        die("文件不存在: %s" % args.input, 2)
    ext = os.path.splitext(args.input)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        grid = read_xlsx(args.input)
    elif ext == ".csv":
        grid = read_csv(args.input)
    else:
        die("不支持的格式 %s（仅 xlsx/csv；其他格式请走口述录入）" % ext, 2)
    if not grid:
        die("表格为空", 2)

    # 找表头行（含 ≥2 个星期标识的行）
    header_idx, day_cols = -1, {}
    for i, row in enumerate(grid[:10]):
        cols = detect_day_columns(row)
        if len(cols) >= 2:
            header_idx, day_cols = i, cols
            break
    if header_idx < 0:
        die("未识别到星期表头（需含 周一~周日/星期X 列名）。请检查格式或改用口述录入", 2)

    weekly, warnings = [], []
    for row in grid[header_idx + 1:]:
        if not any(c.strip() for c in row):
            continue
        first = row[0] if row else ""
        pr = detect_period(first)
        if pr is None:
            # 也许节次列不在第 0 列：找含节次标识的单元格
            cand = None
            for j, c in enumerate(row[:3]):
                cand = detect_period(c)
                if cand:
                    break
            if cand is None:
                continue
            pr = cand
        period, start, end = pr
        for ci, day in day_cols.items():
            if ci >= len(row):
                continue
            cell = row[ci].strip()
            if not cell or cell in ("—", "-", "/", "无", "空"):
                continue
            course, room, weeks = parse_course_cell(cell, args.weeks)
            if not course or len(course) > 60:
                warnings.append("第%s节 周%s 格子无法解析: %r" % (period, day, cell[:30]))
                continue
            weekly.append({"day": day, "period": period, "start": start, "end": end,
                           "course": course, "room": room, "weeks": weeks})
    if not weekly:
        die("识别到表头但未解析出任何课程格。请人工核对或改用口述录入", 2)

    # 去重（合并单元格重复格）
    seen, uniq = set(), []
    for w in weekly:
        key = (w["day"], w["period"], w["course"], w["start"])
        if key not in seen:
            seen.add(key)
            uniq.append(w)
    weekly = uniq

    out = {"source": os.path.basename(args.input),
           "parsed_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
           "weekly": weekly, "exceptions": []}
    out_path = args.out or os.path.join(os.path.dirname(os.path.abspath(args.input)),
                                        "timetable.json")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out_path)

    days_used = sorted(set(w["day"] for w in weekly))
    courses = sorted(set(w["course"] for w in weekly))
    print("课表解析完成 ✓ %d 个课格 → %s" % (len(weekly), out_path))
    print("覆盖: 周%s ｜ %d 门课: %s" % (
        "".join(str(d) for d in days_used), len(courses), "、".join(courses[:8]) +
        ("…" if len(courses) > 8 else "")))
    if warnings:
        print("⚠ %d 格未解析（需人工核对）:" % len(warnings))
        for w in warnings[:5]:
            print("  · " + w)
    print("→ 请学生过目确认课程与周次；考试/活动等例外事件由后续对话补录")


if __name__ == "__main__":
    main()
