#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extract an overview list of Screen and Batch classes from java_structure.json.

Because GeneXus output naming differs by project, this script uses multiple heuristics:
- class name / file path keywords (screen/web/panel/prompt/servlet, batch/job/proc/scheduler)
- presence of typical GeneXus entry points (execute, doExecute, webExecute, renderHtml, main)
- annotations if present
- optional config: user can pass --screen-regex / --batch-regex to override

Outputs:
- CSV: one row per class
- JSON: structured list

Usage:
  python extract_screen_batch_overview.py java_structure.json -o screen_batch_overview.csv --json screen_batch_overview.json

Tip:
  If auto-detection is not accurate, pass explicit regex:
    --screen-regex '(?i)^(.*webpanel.*|.*prompt.*|.*trx.*)$'
    --batch-regex  '(?i)^(.*batch.*|.*job.*|.*procedure.*|.*scheduler.*)$'
"""

from __future__ import annotations
import argparse
import json
import csv
import re
from typing import Any, Dict, List, Tuple

DEFAULT_SCREEN_RE = r'(?i)(webpanel|web|screen|panel|prompt|trx|transaction|servlet|controller|page)'
DEFAULT_BATCH_RE  = r'(?i)(batch|job|procedure|proc|scheduler|cron|daemon|worker|task|timer)'

ENTRYPOINT_RE_SCREEN = re.compile(r'(?i)^(webexecute|renderhtml|execute|initweb|process|draw|event|doload)$')
ENTRYPOINT_RE_BATCH  = re.compile(r'(?i)^(main|execute|doexecute|run|start|process|perform|call|submit)$')

def iter_classes(data: Dict[str, Any]):
    files = data.get("files")
    if isinstance(files, list):
        for f in files:
            file_path = f.get("path") or f.get("file_path") or f.get("name") or f.get("filename") or "<unknown>"
            for c in (f.get("classes") or []):
                yield (file_path, c)
    else:
        for c in (data.get("classes") or []):
            yield ("<unknown>", c)

def class_method_stats(cls: Dict[str, Any]) -> Dict[str, Any]:
    methods = cls.get("methods") or []
    method_names = [m.get("name","") for m in methods]
    # counts
    sql_method_count = sum(1 for m in methods if (m.get("sql_strings") or m.get("sqlStrings")))
    db_hint_method_count = sum(1 for m in methods if m.get("has_db_hints"))
    call_total = 0
    for m in methods:
        calls = m.get("calls") or []
        call_total += len(calls) if isinstance(calls, list) else 0

    # entrypoint guess
    entrypoints = [n for n in method_names if ENTRYPOINT_RE_SCREEN.match(n) or ENTRYPOINT_RE_BATCH.match(n)]
    entrypoints = entrypoints[:8]

    return {
        "method_count": len(methods),
        "sql_method_count": sql_method_count,
        "db_hint_method_count": db_hint_method_count,
        "call_total": call_total,
        "entrypoints": entrypoints,
    }

def detect_type(file_path: str, class_name: str, methods: List[Dict[str, Any]], screen_re: re.Pattern, batch_re: re.Pattern) -> str:
    s_score = 0
    b_score = 0

    target = f"{file_path}::{class_name}"

    if screen_re.search(target):
        s_score += 3
    if batch_re.search(target):
        b_score += 3

    # method signals
    for m in methods:
        name = (m.get("name") or "")
        if ENTRYPOINT_RE_SCREEN.match(name):
            s_score += 1
        if ENTRYPOINT_RE_BATCH.match(name):
            b_score += 1

    # sql/db hints: batch tends to have more, but screen also can
    sql_count = sum(1 for m in methods if (m.get("sql_strings") or m.get("sqlStrings")))
    hint_count = sum(1 for m in methods if m.get("has_db_hints"))
    if sql_count >= 3 or hint_count >= 3:
        b_score += 1
    if "render" in " ".join([m.get("name","").lower() for m in methods]):
        s_score += 1

    if s_score == 0 and b_score == 0:
        return "unknown"
    if s_score >= b_score + 2:
        return "screen"
    if b_score >= s_score + 2:
        return "batch"
    # ambiguous
    return "ambiguous"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("java_structure_json", help="Path to java_structure.json")
    ap.add_argument("-o", "--out", default="screen_batch_overview.csv", help="Output CSV path")
    ap.add_argument("--json", dest="out_json", default=None, help="Optional output JSON path")
    ap.add_argument("--screen-regex", default=DEFAULT_SCREEN_RE, help="Regex to detect screen classes (matched against 'file::class')")
    ap.add_argument("--batch-regex", default=DEFAULT_BATCH_RE, help="Regex to detect batch classes (matched against 'file::class')")
    ap.add_argument("--only", choices=["all","screen","batch","unknown","ambiguous"], default="all", help="Filter output types")
    args = ap.parse_args()

    screen_re = re.compile(args.screen_regex)
    batch_re = re.compile(args.batch_regex)

    with open(args.java_structure_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    rows: List[Dict[str, Any]] = []
    items: List[Dict[str, Any]] = []

    for file_path, cls in iter_classes(data):
        class_name = cls.get("name") or cls.get("class_name") or "<unknown_class>"
        methods = cls.get("methods") or []
        stats = class_method_stats(cls)

        ty = detect_type(file_path, class_name, methods, screen_re, batch_re)
        if args.only != "all" and ty != args.only:
            continue

        # A simple "confidence" score
        matched_screen = bool(screen_re.search(f"{file_path}::{class_name}"))
        matched_batch = bool(batch_re.search(f"{file_path}::{class_name}"))
        confidence = 0.5
        if ty == "screen":
            confidence = 0.8 if matched_screen else 0.65
        elif ty == "batch":
            confidence = 0.8 if matched_batch else 0.65
        elif ty == "ambiguous":
            confidence = 0.4
        else:
            confidence = 0.3

        row = {
            "type": ty,
            "confidence": round(confidence, 2),
            "file": file_path,
            "class": class_name,
            "start_line": cls.get("start_line"),
            "end_line": cls.get("end_line"),
            **stats,
        }
        rows.append(row)

        items.append({
            **row,
            "entrypoints": stats["entrypoints"],
        })

    # sort: type then confidence desc then sql_method_count desc
    def sort_key(r):
        type_order = {"screen":0,"batch":1,"ambiguous":2,"unknown":3}
        return (type_order.get(r["type"], 9), -r["confidence"], -(r.get("sql_method_count") or 0), -(r.get("db_hint_method_count") or 0))
    rows.sort(key=sort_key)

    # write CSV
    fieldnames = ["type","confidence","file","class","start_line","end_line","method_count","sql_method_count","db_hint_method_count","call_total","entrypoints"]
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            # entrypoints list -> compact string
            r2 = dict(r)
            ep = r2.get("entrypoints", [])
            if isinstance(ep, list):
                r2["entrypoints"] = ";".join(ep)
            w.writerow({k: r2.get(k,"") for k in fieldnames})

    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump({"items": items, "screen_regex": args.screen_regex, "batch_regex": args.batch_regex}, f, ensure_ascii=False, indent=2)

    print(f"[OK] CSV written: {args.out}")
    if args.out_json:
        print(f"[OK] JSON written: {args.out_json}")
    print(f"[INFO] classes listed: {len(rows)}")

if __name__ == "__main__":
    main()
