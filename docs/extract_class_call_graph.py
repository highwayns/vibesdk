#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build a class-level call graph from java_structure.json and output:

1) Edge summary CSV (caller_class -> callee_class counts)
2) Callsite detail CSV (one row per call occurrence) with:
   caller_file, caller_class, caller_method, caller_line,
   callee_file, callee_class, callee_method, callee_line (if known),
   raw_text

GeneXus calls shape:
  {"name": "<callee_method>", "qualifier": "<callee_class>", "line": <line>, "text": "Class.method(...)"}

Resolution for callee_file:
- Build a class_to_files index from java_structure.json. If a class appears in multiple files, join with ';'.
- If no match, mark as empty.

Outputs:
  --edges-out   (default: class_calls_edges.csv)
  --calls-out   (default: class_calls_callsites.csv)
  --json        (default: class_calls.json) nodes+edges+samples

Usage:
  python extract_class_call_graph_v3.py java_structure.json
  python extract_class_call_graph_v3.py java_structure.json --edges-out edges.csv --calls-out callsites.csv --json graph.json
"""

from __future__ import annotations
import argparse
import json
import csv
import re
from typing import Any, Dict, Tuple, List, Set, Optional

DOT_CALL_RE = re.compile(r'(?:(?:[A-Za-z_][\w$]*\.)+)?([A-Za-z_][\w$]*)\s*\.\s*([A-Za-z_][\w$]*)')
SIG_RE = re.compile(r'(?:(?:[A-Za-z_][\w$]*\.)+)?([A-Za-z_][\w$]*)\s*\.\s*([A-Za-z_][\w$]*)\s*\(')
LAST_IDENT_RE = re.compile(r'([A-Za-z_][\w$]*)\s*$')

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

def iter_methods(data: Dict[str, Any]):
    for file_path, cls in iter_classes(data):
        class_name = cls.get("name") or cls.get("class_name") or "<unknown_class>"
        for m in (cls.get("methods") or []):
            yield (file_path, class_name, m)

def _canon_class_name(q: str) -> str:
    q = (q or "").strip()
    if not q:
        return ""
    q = re.sub(r'<[^>]*>', '', q)  # drop generics
    q = q.replace("()", "").strip()
    if "." in q:
        q = q.split(".")[-1].strip()
    m = LAST_IDENT_RE.search(q)
    return m.group(1) if m else q

def resolve_call(call: Any, ignore_lowercase_qual: bool) -> Tuple[str, str, Optional[int], str]:
    """
    Return (callee_class, callee_method, callee_line, raw_text)
    callee_line is best-effort (from call["line"] when present).
    """
    if isinstance(call, dict):
        raw_text = str(call.get("text") or call.get("raw") or "")
        callee_line = call.get("line")
        try:
            callee_line = int(callee_line) if callee_line is not None else None
        except Exception:
            callee_line = None

        if "qualifier" in call and "name" in call:
            qual = _canon_class_name(str(call.get("qualifier") or ""))
            mname = str(call.get("name") or "")
            if qual and qual not in {"this", "super"}:
                if ignore_lowercase_qual and qual[:1].islower():
                    return ("UNRESOLVED", mname, callee_line, raw_text)
                return (qual, mname, callee_line, raw_text)

        for ck, mk in [("target_class", "target_method"), ("class", "method"), ("callee_class", "callee_method")]:
            if ck in call and mk in call:
                return (_canon_class_name(str(call.get(ck) or "UNRESOLVED")), str(call.get(mk) or ""), callee_line, raw_text)

        for key in ["text", "callee", "target", "fqn", "full", "signature"]:
            v = call.get(key)
            if isinstance(v, str) and v.strip():
                c, m, _ln, _tx = resolve_call(v, ignore_lowercase_qual)
                return (c, m, callee_line, raw_text or _tx)

        v = call.get("name")
        if isinstance(v, str) and "." in v:
            c, m, _ln, _tx = resolve_call(v, ignore_lowercase_qual)
            return (c, m, callee_line, raw_text or _tx)

        return ("UNRESOLVED", "", callee_line, raw_text)

    if isinstance(call, str):
        s = call.strip()
        m = DOT_CALL_RE.search(s)
        if m:
            return (_canon_class_name(m.group(1)), m.group(2), None, s)
        m2 = SIG_RE.search(s)
        if m2:
            return (_canon_class_name(m2.group(1)), m2.group(2), None, s)
        return ("UNRESOLVED", "", None, s)

    return ("UNRESOLVED", "", None, str(call))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("java_structure_json", help="Path to java_structure.json")
    ap.add_argument("--edges-out", default="class_calls_edges.csv", help="Output edge summary CSV")
    ap.add_argument("--calls-out", default="class_calls_callsites.csv", help="Output callsite detail CSV")
    ap.add_argument("--json", dest="out_json", default="class_calls.json", help="Output JSON graph (set empty to disable)")
    ap.add_argument("--min-count", type=int, default=1, help="Only output edges with at least N calls")
    ap.add_argument("--include-unresolved", action="store_true", help="Include edges where callee is UNRESOLVED")
    ap.add_argument("--ignore-lowercase-qual", action="store_true", help="Treat qualifier starting lowercase as unresolved")
    ap.add_argument("--max-edge-samples", type=int, default=5, help="Max sample callsites per edge in JSON")
    args = ap.parse_args()

    with open(args.java_structure_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Build class -> files index
    class_to_files: Dict[str, Set[str]] = {}
    for file_path, cls in iter_classes(data):
        cn = cls.get("name") or cls.get("class_name") or "<unknown_class>"
        class_to_files.setdefault(cn, set()).add(file_path)

    def files_of(cls_name: str) -> str:
        fs = class_to_files.get(cls_name) or set()
        return ";".join(sorted(fs))

    # edges stats
    edges: Dict[Tuple[str, str], Dict[str, Any]] = {}
    node_set = set()
    unresolved_calls = 0
    total_calls = 0

    # callsite rows
    call_rows: List[Dict[str, Any]] = []

    for caller_file, caller_class, method in iter_methods(data):
        node_set.add(caller_class)
        caller_method = method.get("name") or method.get("method_name") or "<unknown_method>"
        calls = method.get("calls") or []
        if not isinstance(calls, list) or not calls:
            continue

        for call in calls:
            total_calls += 1
            callee_class, callee_method, callee_line, raw_text = resolve_call(call, args.ignore_lowercase_qual)

            if callee_class == "UNRESOLVED" or not callee_class:
                unresolved_calls += 1
                if not args.include_unresolved:
                    continue
                callee_class = "UNRESOLVED"

            node_set.add(callee_class)

            # record callsite detail
            call_rows.append({
                "caller_file": caller_file,
                "caller_class": caller_class,
                "caller_method": caller_method,
                "caller_line": call.get("line") if isinstance(call, dict) else "",
                "callee_file": "" if callee_class in {"UNRESOLVED"} else files_of(callee_class),
                "callee_class": callee_class,
                "callee_method": callee_method,
                "callee_line": callee_line if callee_line is not None else "",
                "raw_text": raw_text,
            })

            # aggregate edge
            key = (caller_class, callee_class)
            st = edges.get(key)
            if not st:
                st = {
                    "caller_class": caller_class,
                    "caller_file_samples": set([caller_file]),
                    "callee_class": callee_class,
                    "callee_file": "" if callee_class in {"UNRESOLVED"} else files_of(callee_class),
                    "count": 0,
                    "sample_calls": [],
                }
                edges[key] = st
            st["count"] += 1
            st["caller_file_samples"].add(caller_file)
            if len(st["sample_calls"]) < args.max_edge_samples:
                st["sample_calls"].append({
                    "caller_file": caller_file,
                    "caller_method": caller_method,
                    "callee_method": callee_method,
                    "raw": call if isinstance(call, (str, dict)) else str(call),
                })

    # Write callsites CSV
    call_fields = ["caller_file","caller_class","caller_method","caller_line",
                   "callee_file","callee_class","callee_method","callee_line","raw_text"]
    with open(args.calls_out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=call_fields)
        w.writeheader()
        for r in call_rows:
            w.writerow({k: r.get(k, "") for k in call_fields})

    # Edge list filtered
    edge_list = []
    for e in edges.values():
        if e["count"] < args.min_count:
            continue
        # finalize caller_files
        e2 = dict(e)
        e2["caller_files"] = ";".join(sorted(list(e2.pop("caller_file_samples", set()))))
        edge_list.append(e2)

    edge_list.sort(key=lambda x: (-x["count"], x["caller_class"], x["callee_class"]))

    # Write edges CSV (include caller_files and callee_file)
    edge_fields = ["caller_class","caller_files","callee_class","callee_file","count"]
    with open(args.edges_out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=edge_fields)
        w.writeheader()
        for e in edge_list:
            w.writerow({k: e.get(k, "") for k in edge_fields})

    # JSON graph
    if args.out_json:
        nodes = [{"id": n, "files": files_of(n)} for n in sorted(node_set)]
        graph = {
            "nodes": nodes,
            "edges": edge_list,
            "stats": {
                "total_calls_seen": total_calls,
                "unresolved_calls_seen": unresolved_calls,
                "edges_written": len(edge_list),
                "nodes_written": len(nodes),
                "min_count": args.min_count,
                "include_unresolved": args.include_unresolved,
                "ignore_lowercase_qual": args.ignore_lowercase_qual,
                "callsites_written": len(call_rows),
            }
        }
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False, indent=2)

    print(f"[OK] callsites CSV written: {args.calls_out}")
    print(f"[OK] edges CSV written: {args.edges_out}")
    if args.out_json:
        print(f"[OK] JSON written: {args.out_json}")
    print(f"[INFO] total_calls_seen={total_calls} unresolved_calls_seen={unresolved_calls} edges={len(edge_list)} callsites={len(call_rows)}")

if __name__ == "__main__":
    main()
