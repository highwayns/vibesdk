#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extract (file, class, method) entries that contain sql_strings from java_structure.json,
parse referenced tables, and infer CRUD per table.

Supports quoted identifiers:
- `schema`.`table`  (MySQL style)
- `schema.table`    (whole token quoted)
- "schema"."table"  (ANSI)
- [schema].[table]  (SQL Server)
- unquoted schema.table

Outputs:
- CSV (default): one row per (sql occurrence, table) with inferred CRUD
- JSON (optional): structured report

Usage:
  python extract_sql_crud_report.py java_structure.json -o sql_crud_report.csv --json sql_crud_report.json
"""

from __future__ import annotations
import argparse
import json
import csv
import re
from typing import Any, Dict, List, Optional, Tuple, Set

# Identifier parts: `...` or "..." or [...] or bareword
IDENT_PART = r'(?:`[^`]+`|"[^"]+"|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_$]*)'
# Fully-qualified token: part(.part)*
QUALIFIED = rf'{IDENT_PART}(?:\s*\.\s*{IDENT_PART})*'

# Common SQL keywords used to locate table tokens
RE_FROM = re.compile(rf'(?is)\bFROM\s+({QUALIFIED})')
RE_JOIN = re.compile(rf'(?is)\bJOIN\s+({QUALIFIED})')
RE_UPDATE = re.compile(rf'(?is)\bUPDATE\s+({QUALIFIED})')
RE_INSERT = re.compile(rf'(?is)\bINSERT\s+INTO\s+({QUALIFIED})')
RE_DELETE = re.compile(rf'(?is)\bDELETE\s+FROM\s+({QUALIFIED})')
RE_MERGE = re.compile(rf'(?is)\bMERGE\s+INTO\s+({QUALIFIED})')
RE_USING = re.compile(rf'(?is)\bUSING\s+({QUALIFIED})')

RE_SQL_TYPE = re.compile(r'(?is)^\s*(?:/\*.*?\*/\s*)*(?:--[^\n]*\n\s*)*(\w+)\b')

def _strip_quotes(part: str) -> str:
    part = part.strip()
    if (part.startswith('`') and part.endswith('`')) or (part.startswith('"') and part.endswith('"')):
        return part[1:-1]
    if part.startswith('[') and part.endswith(']'):
        return part[1:-1]
    return part

def normalize_table_token(token: str) -> Dict[str, str]:
    """
    Normalize a qualified identifier token and return:
      - full: dot-joined normalized parts
      - name: last part (table/view name)
      - schema: second last part if present else ""
      - catalog: third last part if present else ""
    """
    parts = [p.strip() for p in re.split(r'\s*\.\s*', token.strip()) if p.strip()]
    norm_parts = [_strip_quotes(p) for p in parts]
    full = ".".join(norm_parts)
    name = norm_parts[-1] if norm_parts else ""
    schema = norm_parts[-2] if len(norm_parts) >= 2 else ""
    catalog = norm_parts[-3] if len(norm_parts) >= 3 else ""
    return {"full": full, "name": name, "schema": schema, "catalog": catalog}

def detect_sql_kind(sql: str) -> str:
    m = RE_SQL_TYPE.search(sql)
    if not m:
        return "UNKNOWN"
    kw = m.group(1).upper()
    # normalize
    if kw in {"SELECT", "WITH"}:
        return "SELECT"
    if kw in {"INSERT"}:
        return "INSERT"
    if kw in {"UPDATE"}:
        return "UPDATE"
    if kw in {"DELETE"}:
        return "DELETE"
    if kw in {"MERGE"}:
        return "MERGE"
    return kw

def extract_tables(sql: str) -> Dict[str, Set[str]]:
    """
    Return sets of qualified tokens (raw) by role:
      - read: tables read from (FROM/JOIN/USING)
      - write_insert: insert targets
      - write_update: update targets
      - write_delete: delete targets
      - write_merge: merge targets
    """
    roles: Dict[str, Set[str]] = {
        "read": set(),
        "write_insert": set(),
        "write_update": set(),
        "write_delete": set(),
        "write_merge": set(),
    }

    # INSERT targets
    for m in RE_INSERT.finditer(sql):
        roles["write_insert"].add(m.group(1))

    # UPDATE targets
    for m in RE_UPDATE.finditer(sql):
        roles["write_update"].add(m.group(1))

    # DELETE targets
    for m in RE_DELETE.finditer(sql):
        roles["write_delete"].add(m.group(1))

    # MERGE targets
    for m in RE_MERGE.finditer(sql):
        roles["write_merge"].add(m.group(1))

    # READ tables: FROM/JOIN/USING
    for m in RE_FROM.finditer(sql):
        roles["read"].add(m.group(1))
    for m in RE_JOIN.finditer(sql):
        roles["read"].add(m.group(1))
    for m in RE_USING.finditer(sql):
        roles["read"].add(m.group(1))

    return roles

def crud_from_roles(sql_kind: str, roles: Dict[str, Set[str]]) -> List[Tuple[str, str, str]]:
    """
    Produce list of (raw_token, normalized_full, crud) per table token.
    Heuristic mapping:
      SELECT -> all read tables = R
      INSERT -> target = C, read tables = R
      UPDATE -> target = U, read tables = R (joined sources)
      DELETE -> target = D, read tables = R (joined sources)
      MERGE  -> target = U (or C/U), read tables = R
    """
    out: List[Tuple[str, str, str]] = []
    def add(tokens: Set[str], crud: str):
        for t in tokens:
            norm = normalize_table_token(t)["full"]
            out.append((t, norm, crud))

    if sql_kind == "SELECT":
        add(roles["read"], "R")
    elif sql_kind == "INSERT":
        add(roles["write_insert"], "C")
        add(roles["read"], "R")
    elif sql_kind == "UPDATE":
        add(roles["write_update"], "U")
        add(roles["read"], "R")
    elif sql_kind == "DELETE":
        add(roles["write_delete"], "D")
        add(roles["read"], "R")
    elif sql_kind == "MERGE":
        # conservative: mark merge target as U
        add(roles["write_merge"], "U")
        add(roles["read"], "R")
    else:
        # unknown: just output read/write buckets with generic tags
        add(roles["write_insert"], "C")
        add(roles["write_update"], "U")
        add(roles["write_delete"], "D")
        add(roles["write_merge"], "U")
        add(roles["read"], "R")

    # de-dup exact triples
    dedup: List[Tuple[str, str, str]] = []
    seen = set()
    for triple in out:
        if triple not in seen:
            dedup.append(triple)
            seen.add(triple)
    return dedup

def shorten(s: str, n: int) -> str:
    s = " ".join(s.replace("\n", " ").replace("\r", " ").split())
    if len(s) <= n:
        return s
    return s[: max(0, n-3)] + "..."

def iter_structure(obj: Any):
    """
    Yield tuples (file_path, class_obj, method_obj)
    Compatible with different java_structure.json shapes.
    """
    files = obj.get("files")
    if isinstance(files, list):
        for f in files:
            file_path = f.get("path") or f.get("file_path") or f.get("name") or f.get("filename") or "<unknown>"
            classes = f.get("classes") or []
            for c in classes:
                methods = c.get("methods") or []
                for m in methods:
                    yield (file_path, c, m)
    else:
        # fallback: maybe top-level has classes directly
        classes = obj.get("classes") or []
        for c in classes:
            methods = c.get("methods") or []
            for m in methods:
                yield ("<unknown>", c, m)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("java_structure_json", help="Path to java_structure.json")
    ap.add_argument("-o", "--out", default="sql_crud_report.csv", help="Output CSV path")
    ap.add_argument("--json", dest="out_json", default=None, help="Optional output JSON path")
    ap.add_argument("--max-sql-len", type=int, default=220, help="Truncate SQL preview length in CSV/JSON")
    ap.add_argument("--only-with-tables", action="store_true", help="Only output entries where at least one table is extracted")
    args = ap.parse_args()

    with open(args.java_structure_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    rows: List[Dict[str, Any]] = []
    structured: List[Dict[str, Any]] = []

    for file_path, cls, mth in iter_structure(data):
        sqls = mth.get("sql_strings") or mth.get("sqlStrings") or []
        if not sqls:
            continue

        class_name = cls.get("name") or cls.get("class_name") or "<unknown_class>"
        method_name = mth.get("name") or mth.get("method_name") or "<unknown_method>"
        signature = mth.get("signature") or ""
        start_line = mth.get("start_line")
        end_line = mth.get("end_line")
        has_db_hints = bool(mth.get("has_db_hints", False))

        method_item: Dict[str, Any] = {
            "file": file_path,
            "class": class_name,
            "method": method_name,
            "signature": signature,
            "start_line": start_line,
            "end_line": end_line,
            "has_db_hints": has_db_hints,
            "sqls": [],
        }

        any_table = False
        for idx, sql in enumerate(sqls, 1):
            sql_kind = detect_sql_kind(sql)
            roles = extract_tables(sql)
            triples = crud_from_roles(sql_kind, roles)
            tables = [{"raw": raw, **normalize_table_token(raw), "crud": crud} for raw, _norm, crud in triples]
            if tables:
                any_table = True

            # per-table CSV rows
            if tables:
                for t in tables:
                    rows.append({
                        "file": file_path,
                        "class": class_name,
                        "method": method_name,
                        "signature": signature,
                        "start_line": start_line,
                        "end_line": end_line,
                        "has_db_hints": has_db_hints,
                        "sql_index": idx,
                        "sql_kind": sql_kind,
                        "table_full": t["full"],
                        "table_name": t["name"],
                        "schema": t["schema"],
                        "catalog": t["catalog"],
                        "crud": t["crud"],
                        "sql_preview": shorten(sql, args.max_sql_len),
                    })
            else:
                # still record SQL even if no table found
                rows.append({
                    "file": file_path,
                    "class": class_name,
                    "method": method_name,
                    "signature": signature,
                    "start_line": start_line,
                    "end_line": end_line,
                    "has_db_hints": has_db_hints,
                    "sql_index": idx,
                    "sql_kind": sql_kind,
                    "table_full": "",
                    "table_name": "",
                    "schema": "",
                    "catalog": "",
                    "crud": "",
                    "sql_preview": shorten(sql, args.max_sql_len),
                })

            method_item["sqls"].append({
                "index": idx,
                "sql_kind": sql_kind,
                "sql_preview": shorten(sql, args.max_sql_len),
                "tables": tables,
                "roles_raw": {k: sorted(list(v)) for k, v in roles.items()},
            })

        if (not args.only_with_tables) or any_table:
            structured.append(method_item)

    # Write CSV
    fieldnames = [
        "file","class","method","signature","start_line","end_line","has_db_hints",
        "sql_index","sql_kind",
        "table_full","table_name","schema","catalog","crud",
        "sql_preview"
    ]
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            if args.only_with_tables and not r.get("table_full"):
                continue
            w.writerow(r)

    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump({"items": structured}, f, ensure_ascii=False, indent=2)

    print(f"[OK] CSV written: {args.out}")
    if args.out_json:
        print(f"[OK] JSON written: {args.out_json}")
    print(f"[INFO] total sql occurrences: {sum(1 for _ in rows)}")
    print(f"[INFO] methods with sql_strings: {len(structured)}")
    if args.only_with_tables:
        print("[INFO] filtered: only_with_tables enabled")

if __name__ == "__main__":
    main()
