#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build a simple class-level call graph from java_structure.json and output CSV/JSON.

This script aggregates method-level calls (method.calls) into class-to-class edges:
  caller_class -> callee_class

It tries to resolve callee class name from call entries using common shapes:
- {"target_class": "...", "target_method": "..."}  (preferred)
- {"class": "...", "method": "..."}
- {"callee": "Class.method"} / {"target": "Class.method"} / string "Class.method"
- {"signature": "com.pkg.Class.method(...)"}

If the callee class cannot be resolved, it is counted as "UNRESOLVED".

Outputs:
- CSV edges: one row per (caller_class, callee_class) with call count
- JSON graph: {"nodes":[...], "edges":[...], "stats":{...}}

Usage:
  python extract_class_call_graph.py java_structure.json -o class_calls.csv --json class_calls.json

Options:
  --min-count N          only output edges with at least N calls
  --include-unresolved   include edges where callee_class == UNRESOLVED
  --max-samples K        include up to K sample callsites per edge (JSON)
"""

from __future__ import annotations
import argparse
import json
import csv
import re
from typing import Any, Dict, List, Tuple

# Parse patterns for call strings like "Class.method" or "pkg.Class.method"
DOT_CALL_RE = re.compile(r'(?:(?:[A-Za-z_][\w$]*\.)+)?([A-Za-z_][\w$]*)\s*\.\s*([A-Za-z_][\w$]*)')
SIG_RE = re.compile(r'(?:(?:[A-Za-z_][\w$]*\.)+)?([A-Za-z_][\w$]*)\s*\.\s*([A-Za-z_][\w$]*)\s*\(')

def iter_methods(data: Dict[str, Any]):
    files = data.get("files")
    if isinstance(files, list):
        for f in files:
            file_path = f.get("path") or f.get("file_path") or f.get("name") or f.get("filename") or "<unknown>"
            for c in (f.get("classes") or []):
                class_name = c.get("name") or c.get("class_name") or "<unknown_class>"
                for m in (c.get("methods") or []):
                    yield (file_path, class_name, m)
    else:
        for c in (data.get("classes") or []):
            class_name = c.get("name") or c.get("class_name") or "<unknown_class>"
            for m in (c.get("methods") or []):
                yield ("<unknown>", class_name, m)

def resolve_call(call: Any) -> Tuple[str, str]:
    """Return (callee_class, callee_method) best effort. callee_class may be UNRESOLVED."""
    if isinstance(call, dict):
        for ck, mk in [
            ("target_class", "target_method"),
            ("class", "method"),
            ("callee_class", "callee_method"),
        ]:
            if ck in call and mk in call:
                return (str(call.get(ck) or "UNRESOLVED"), str(call.get(mk) or ""))
        for key in ["callee", "target", "fqn", "full", "name"]:
            v = call.get(key)
            if isinstance(v, str):
                c, m = resolve_call(v)
                if c != "UNRESOLVED" or m:
                    return c, m
        sig = call.get("signature")
        if isinstance(sig, str):
            m = SIG_RE.search(sig)
            if m:
                return m.group(1), m.group(2)
        return ("UNRESOLVED", "")
    if isinstance(call, str):
        m = DOT_CALL_RE.search(call)
        if m:
            return m.group(1), m.group(2)
        m2 = SIG_RE.search(call)
        if m2:
            return m2.group(1), m2.group(2)
        return ("UNRESOLVED", "")
    return ("UNRESOLVED", "")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("java_structure_json", help="Path to java_structure.json")
    ap.add_argument("-o", "--out", default="class_calls.csv", help="Output CSV path")
    ap.add_argument("--json", dest="out_json", default="class_calls.json", help="Output JSON path (set empty to disable)")
    ap.add_argument("--min-count", type=int, default=1, help="Only output edges with at least N calls")
    ap.add_argument("--include-unresolved", action="store_true", help="Include edges where callee is UNRESOLVED")
    ap.add_argument("--max-samples", type=int, default=5, help="Max sample callsites per edge in JSON")
    args = ap.parse_args()

    with open(args.java_structure_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    edges: Dict[Tuple[str, str], Dict[str, Any]] = {}
    node_set = set()
    unresolved_calls = 0
    total_calls = 0

    for file_path, caller_class, method in iter_methods(data):
        node_set.add(caller_class)
        caller_method = method.get("name") or method.get("method_name") or "<unknown_method>"
        calls = method.get("calls") or []
        if not isinstance(calls, list) or not calls:
            continue

        for call in calls:
            total_calls += 1
            callee_class, callee_method = resolve_call(call)
            if callee_class == "UNRESOLVED":
                unresolved_calls += 1
                if not args.include_unresolved:
                    continue

            node_set.add(callee_class)
            key = (caller_class, callee_class)
            st = edges.get(key)
            if not st:
                st = {
                    "caller_class": caller_class,
                    "callee_class": callee_class,
                    "count": 0,
                    "sample_calls": [],
                }
                edges[key] = st
            st["count"] += 1
            if len(st["sample_calls"]) < args.max_samples:
                st["sample_calls"].append({
                    "file": file_path,
                    "caller_method": caller_method,
                    "callee_method": callee_method,
                    "raw": call if isinstance(call, (str, dict)) else str(call),
                })

    edge_list = [v for v in edges.values() if v["count"] >= args.min_count]
    edge_list.sort(key=lambda x: (-x["count"], x["caller_class"], x["callee_class"]))

    # CSV
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["caller_class", "callee_class", "count"])
        w.writeheader()
        for e in edge_list:
            w.writerow({"caller_class": e["caller_class"], "callee_class": e["callee_class"], "count": e["count"]})

    # JSON
    if args.out_json:
        nodes = [{"id": n} for n in sorted(node_set)]
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
            }
        }
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False, indent=2)

    print(f"[OK] CSV written: {args.out}")
    if args.out_json:
        print(f"[OK] JSON written: {args.out_json}")
    print(f"[INFO] total_calls_seen={total_calls} unresolved_calls_seen={unresolved_calls} edges_written={len(edge_list)} nodes={len(node_set)}")

if __name__ == "__main__":
    main()
