#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
コード-データベース関連分析ツール

GeneXus生成JavaコードからSQL/テーブル参照を抽出し、
データベースメタデータと関連付けて機能設計を還元する。

機能:
1. Javaコードからテーブル参照を抽出
2. データベースメタデータと関連付け
3. 機能-テーブル マッピング生成
4. 日本語名称による設計書生成

使用方法:
    python analyze_code_db_mapping.py java_structure.json db_metadata.json -o design_doc.json
"""

import argparse
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Any, Set, Tuple, Optional
from dataclasses import dataclass, field, asdict
from collections import defaultdict, deque
# ---------- 呼び出し関係（コールグラフ） ----------

_IGNORE_METHOD_NAMES = {
    # Java/common noise
    'toString', 'equals', 'hashCode', 'getClass', 'clone', 'finalize',
    # collections
    'size', 'isEmpty', 'add', 'put', 'remove', 'contains', 'containsKey',
    # typical fluent methods
    'append', 'format', 'valueOf',
}


def _simplify_qualifier(text: Optional[str]) -> Optional[str]:
    """Best-effort simplification for a call qualifier.

    Examples:
      - "this" -> "this"
      - "SomeClass" -> "SomeClass"
      - "pkg.SomeClass" -> "SomeClass"
      - "new SomeClass()" -> "SomeClass"
      - "obj" -> "obj" (variable, type unknown)
    """
    if not text:
        return None
    q = text.strip()
    if not q:
        return None

    # "new Type(...)" -> Type
    m = re.search(r"\bnew\s+([A-Za-z_][A-Za-z0-9_]*)\b", q)
    if m:
        return m.group(1)

    # Remove call suffix
    q = q.split('(', 1)[0].strip()
    # Take last segment of scoped identifiers
    if '.' in q:
        q = q.rsplit('.', 1)[-1].strip()
    # Keep only identifier-like token
    q = re.sub(r"[^A-Za-z0-9_]", "", q)
    return q or None


def _looks_like_class_name(name: str) -> bool:
    return bool(name) and name[0].isupper()


def _extract_used_columns(text: str, columns: List[Dict[str, Any]], max_cols: int = 25) -> List[Dict[str, Any]]:
    """Extract likely-used column names by scanning the SQL/code text.

    This is a heuristic (static analysis) and may over/under-approximate.
    """
    used: List[Dict[str, Any]] = []
    if not text:
        return used
    # Lower for case-insensitive contains; regex is still used for word boundary
    for col in columns:
        name = (col.get('name') or '').strip()
        if not name:
            continue
        # Avoid extremely short tokens that create many false positives
        if len(name) <= 2:
            continue
        try:
            if re.search(rf"\b{re.escape(name)}\b", text, flags=re.IGNORECASE):
                used.append({
                    'name': name,
                    'logical_name': col.get('logical_name'),
                })
        except re.error:
            continue
        if len(used) >= max_cols:
            break
    return used


# ---------- データ構造 ----------

@dataclass
class TableReference:
    """テーブル参照情報"""
    table_name: str                    # テーブル物理名
    logical_name: str                  # テーブル論理名（日本語）
    operation_type: str                # SELECT/INSERT/UPDATE/DELETE
    source_class: str                  # 参照元クラス
    source_method: Optional[str]       # 参照元メソッド
    context: str                       # 参照コンテキスト（コード断片）


@dataclass
class FunctionDesign:
    """機能設計情報"""
    function_name: str                 # 機能名（日本語）
    function_id: str                   # 機能ID
    function_type: str                 # screen/batch
    genexus_type: Optional[str]        # GeneXusオブジェクトタイプ
    description: str                   # 機能説明
    entry_classes: List[str]           # エントリクラス一覧
    related_classes: List[str]         # 関連クラス一覧
    tables_used: List[Dict[str, Any]]  # 使用テーブル一覧
    crud_matrix: Dict[str, List[str]]  # CRUD操作マトリックス


@dataclass
class SystemDesign:
    """システム設計情報"""
    project_name: str
    functions: List[FunctionDesign]
    table_function_matrix: Dict[str, List[str]]  # テーブル → 機能 マッピング
    er_diagram_data: Dict[str, Any]              # ER図データ


# ---------- SQL/テーブル参照抽出 ----------

class TableReferenceExtractor:
    """コードからテーブル参照を抽出"""
    
    # SQL文パターン
    SQL_PATTERNS = {
        'SELECT': [
            # allow schema-qualified tokens and simple quoting
            r'(?i)\bFROM\s+["`]?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)["`]?',
            r'(?i)\bJOIN\s+["`]?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)["`]?',
            r'(?i)\bINNER\s+JOIN\s+["`]?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)["`]?',
            r'(?i)\bLEFT\s+JOIN\s+["`]?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)["`]?',
            r'(?i)\bRIGHT\s+JOIN\s+["`]?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)["`]?',
        ],
        'INSERT': [
            r'(?i)\bINSERT\s+INTO\s+["`]?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)["`]?',
        ],
        'UPDATE': [
            r'(?i)\bUPDATE\s+["`]?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)["`]?',
        ],
        'DELETE': [
            r'(?i)\bDELETE\s+FROM\s+["`]?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)["`]?',
        ],
    }

    # quoted identifiers like: FROM "SCHEMA"."TABLE" (best-effort)
    SQL_QUOTED_PATTERNS = {
        'SELECT': [
            r'(?i)\bFROM\s+"([A-Za-z_][A-Za-z0-9_]*)"\s*\.\s*"([A-Za-z_][A-Za-z0-9_]*)"',
            r'(?i)\bJOIN\s+"([A-Za-z_][A-Za-z0-9_]*)"\s*\.\s*"([A-Za-z_][A-Za-z0-9_]*)"',
        ],
        'INSERT': [
            r'(?i)\bINSERT\s+INTO\s+"([A-Za-z_][A-Za-z0-9_]*)"\s*\.\s*"([A-Za-z_][A-Za-z0-9_]*)"',
        ],
        'UPDATE': [
            r'(?i)\bUPDATE\s+"([A-Za-z_][A-Za-z0-9_]*)"\s*\.\s*"([A-Za-z_][A-Za-z0-9_]*)"',
        ],
        'DELETE': [
            r'(?i)\bDELETE\s+FROM\s+"([A-Za-z_][A-Za-z0-9_]*)"\s*\.\s*"([A-Za-z_][A-Za-z0-9_]*)"',
        ],
    }
    
    # GeneXus特有のパターン
    GENEXUS_PATTERNS = {
        # Business Componentによるテーブルアクセス
        'BC_LOAD': r'(?i)(\w+)_bc\s*[.]\s*load',
        'BC_SAVE': r'(?i)(\w+)_bc\s*[.]\s*save',
        'BC_DELETE': r'(?i)(\w+)_bc\s*[.]\s*delete',
        # GeneXusのFor Each
        'FOR_EACH': r'(?i)for\s+each\s+(\w+)',
        # SDT参照
        'SDT_REF': r'(?i)sdt_(\w+)',
    }

    # DB access "hints" (heuristics) - used for debug / filtering / prioritization.
    # These patterns intentionally over-approximate: they are meant to explain
    # "this method likely touches DB" even if we can't map it to a physical table.
    DB_HINT_PATTERNS = {
        # GeneXus BC / helper access (even when table cannot be inferred)
        'GX_BC_CALL': r'(?i)\b\w+_bc\s*\.\s*(load|save|insert|update|delete)\b',
        'GX_PR_DEFAULT': r'(?i)\bpr_default\b',
        'GX_CURSOR': r'(?i)\b(cursor|open\s*\(|close\s*\(|fetch\s*\()\b',
        'GX_DATASTORE': r'(?i)\b(DataStoreProvider|IDataStoreProvider|DataStoreHelper|GxDataStore|GxContext)\b',
        'GX_EXECUTE': r'(?i)\b(execute\s*\(|executeStmt|executeDirectSQL|executeQuery|executeUpdate)\b',
        # JDBC-ish
        'JDBC': r'(?i)\b(prepareStatement|createStatement|PreparedStatement|CallableStatement|ResultSet)\b',
        # Generic SQL keywords (fallback; may be noisy but useful when literals are concatenated)
        'SQL_KEYWORD': r'(?i)\b(select|insert\s+into|update|delete\s+from)\b',
    }
    
    def __init__(self, db_metadata: Dict[str, Any]):
        self.db_metadata = db_metadata
        self.table_names = self._build_table_name_set()
        self.table_logical_names = self._build_logical_name_map()
    
    def _build_table_name_set(self) -> Set[str]:
        """テーブル名セットを構築"""
        tables = set()
        for table in self.db_metadata.get('tables', []):
            tables.add(table['table_name'].upper())
            tables.add(table['table_name'].lower())
        return tables
    
    def _build_logical_name_map(self) -> Dict[str, str]:
        """テーブル物理名 → 論理名 マッピング"""
        mapping = {}
        for table in self.db_metadata.get('tables', []):
            mapping[table['table_name'].upper()] = table.get('logical_name', table['table_name'])
            mapping[table['table_name'].lower()] = table.get('logical_name', table['table_name'])
        return mapping
    
    def extract_from_code(self, code: str, class_name: str, 
                          method_name: Optional[str] = None) -> List[TableReference]:
        """コードからテーブル参照を抽出"""
        references = []
        
        # SQL文からのテーブル抽出
        for op_type, patterns in self.SQL_PATTERNS.items():
            for pattern in patterns:
                for match in re.finditer(pattern, code):
                    raw = match.group(1)
                    table_name = self._normalize_table_token(raw)
                    if table_name and self._is_valid_table(table_name):
                        references.append(TableReference(
                            table_name=table_name,
                            logical_name=self._get_logical_name(table_name),
                            operation_type=op_type,
                            source_class=class_name,
                            source_method=method_name,
                            context=self._extract_context(code, match.start()),
                        ))

        # Quoted schema.table patterns
        for op_type, patterns in self.SQL_QUOTED_PATTERNS.items():
            for pattern in patterns:
                for match in re.finditer(pattern, code):
                    # group(1)=schema, group(2)=table
                    raw = match.group(2)
                    table_name = self._normalize_table_token(raw)
                    if table_name and self._is_valid_table(table_name):
                        references.append(TableReference(
                            table_name=table_name,
                            logical_name=self._get_logical_name(table_name),
                            operation_type=op_type,
                            source_class=class_name,
                            source_method=method_name,
                            context=self._extract_context(code, match.start()),
                        ))
        
        # GeneXus特有パターンからの抽出
        references.extend(self._extract_genexus_references(code, class_name, method_name))
        
        return references
    
    def _extract_genexus_references(self, code: str, class_name: str,
                                    method_name: Optional[str]) -> List[TableReference]:
        """GeneXus特有のテーブル参照を抽出"""
        references = []
        
        # Business Component参照
        for pattern_name, pattern in self.GENEXUS_PATTERNS.items():
            for match in re.finditer(pattern, code):
                entity_name = match.group(1)
                table_name = self._guess_table_from_entity(entity_name)
                
                if table_name:
                    op_type = {
                        'BC_LOAD': 'SELECT',
                        'BC_SAVE': 'INSERT/UPDATE',
                        'BC_DELETE': 'DELETE',
                        'FOR_EACH': 'SELECT',
                        'SDT_REF': 'REFERENCE',
                    }.get(pattern_name, 'UNKNOWN')
                    
                    references.append(TableReference(
                        table_name=table_name,
                        logical_name=self._get_logical_name(table_name),
                        operation_type=op_type,
                        source_class=class_name,
                        source_method=method_name,
                        context=self._extract_context(code, match.start()),
                    ))
        
        return references
    
    def _is_valid_table(self, name: str) -> bool:
        """有効なテーブル名かチェック"""
        # 予約語や一般的な変数名を除外
        exclude = {'SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'SET', 'INTO', 
                   'VALUES', 'ORDER', 'GROUP', 'HAVING', 'LIMIT', 'OFFSET',
                   'NULL', 'TRUE', 'FALSE', 'AS', 'ON', 'IN', 'NOT', 'LIKE'}
        if not name:
            return False
        if name.upper() in exclude:
            return False
        return name.upper() in self.table_names or name.lower() in self.table_names

    def _normalize_table_token(self, token: str) -> str:
        """Normalize a matched table token to a physical table name.

        Examples:
          - schema.table -> table
          - `table` -> table
          - "table" -> table
        """
        if not token:
            return ""
        t = token.strip()
        # strip surrounding quotes/backticks
        if (t.startswith('"') and t.endswith('"')) or (t.startswith('`') and t.endswith('`')):
            t = t[1:-1]
        # drop schema prefix
        if '.' in t:
            t = t.split('.')[-1]
        # keep identifier chars
        t = re.sub(r"[^A-Za-z0-9_]", "", t)
        return t

    def debug_scan_sql_candidates(self, code: str) -> List[Dict[str, Any]]:
        """Debug helper: scan SQL patterns and report candidates, including ignored ones.

        Returns a list of dicts:
          {op_type, raw_token, normalized, is_valid, reason}
        """
        out: List[Dict[str, Any]] = []
        if not code:
            return out
        for op_type, patterns in self.SQL_PATTERNS.items():
            for pattern in patterns:
                for match in re.finditer(pattern, code):
                    raw = match.group(1)
                    norm = self._normalize_table_token(raw)
                    if not norm:
                        out.append({'op_type': op_type, 'raw_token': raw, 'normalized': norm, 'is_valid': False, 'reason': 'normalize_empty'})
                        continue
                    if self._is_valid_table(norm):
                        out.append({'op_type': op_type, 'raw_token': raw, 'normalized': norm, 'is_valid': True, 'reason': 'ok'})
                    else:
                        out.append({'op_type': op_type, 'raw_token': raw, 'normalized': norm, 'is_valid': False, 'reason': 'not_in_db_metadata'})

        for op_type, patterns in self.SQL_QUOTED_PATTERNS.items():
            for pattern in patterns:
                for match in re.finditer(pattern, code):
                    raw = match.group(2)
                    norm = self._normalize_table_token(raw)
                    if self._is_valid_table(norm):
                        out.append({'op_type': op_type, 'raw_token': f"{match.group(1)}.{raw}", 'normalized': norm, 'is_valid': True, 'reason': 'ok'})
                    else:
                        out.append({'op_type': op_type, 'raw_token': f"{match.group(1)}.{raw}", 'normalized': norm, 'is_valid': False, 'reason': 'not_in_db_metadata'})
        return out

    def has_db_hints(self, code: str) -> bool:
        """Return True if the code contains DB-access hints.

        This is intentionally heuristic and may over-approximate.
        It is used to:
          - keep methods during parsing/filtering
          - explain 'why DB likely exists but tables were not extracted'
        """
        if not code:
            return False
        # Fast path: check GeneXus patterns too
        for _, pat in self.GENEXUS_PATTERNS.items():
            if re.search(pat, code):
                return True
        for _, pat in self.DB_HINT_PATTERNS.items():
            if re.search(pat, code):
                return True
        return False

    def debug_scan_db_hints(self, code: str, max_items: int = 12) -> List[Dict[str, Any]]:
        """Debug helper: scan DB hint patterns and report hits."""
        out: List[Dict[str, Any]] = []
        if not code:
            return out

        # 1) GeneXus patterns
        for name, pat in self.GENEXUS_PATTERNS.items():
            for m in re.finditer(pat, code):
                out.append({
                    'hint_type': f"GENEXUS:{name}",
                    'match': (m.group(0) or '')[:120],
                })
                if len(out) >= max_items:
                    return out

        # 2) Generic DB hint patterns
        for name, pat in self.DB_HINT_PATTERNS.items():
            for m in re.finditer(pat, code):
                out.append({
                    'hint_type': name,
                    'match': (m.group(0) or '')[:120],
                })
                if len(out) >= max_items:
                    return out
        return out
    
    def _get_logical_name(self, table_name: str) -> str:
        """テーブルの論理名を取得"""
        return self.table_logical_names.get(table_name.upper(), 
               self.table_logical_names.get(table_name.lower(), table_name))
    
    def _guess_table_from_entity(self, entity_name: str) -> Optional[str]:
        """エンティティ名からテーブル名を推測"""
        # 直接マッチ
        if entity_name.upper() in self.table_names:
            return entity_name.upper()
        if entity_name.lower() in self.table_names:
            return entity_name.lower()
        
        # プレフィックス除去してマッチ
        for prefix in ['sdt_', 'type_', 'bc_', 'trn_']:
            if entity_name.lower().startswith(prefix):
                base_name = entity_name[len(prefix):]
                if base_name.upper() in self.table_names:
                    return base_name.upper()
        
        return None
    
    def _extract_context(self, code: str, position: int, context_length: int = 100) -> str:
        """参照箇所の前後コンテキストを抽出"""
        start = max(0, position - context_length // 2)
        end = min(len(code), position + context_length // 2)
        return code[start:end].replace('\n', ' ').strip()


# ---------- 機能設計還元 ----------

class FunctionDesignRestorer:
    """機能設計を還元"""
    
    def __init__(self, java_structure: Dict[str, Any], db_metadata: Dict[str, Any]):
        self.java_structure = java_structure
        self.db_metadata = db_metadata
        self.extractor = TableReferenceExtractor(db_metadata)
        self.table_info = self._build_table_info()

        # Build a static call graph index so we can resolve "screen -> other" DB access.
        self._call_max_depth = int(java_structure.get('call_graph', {}).get('max_depth', 8)) if isinstance(java_structure, dict) else 8
        self._call_max_nodes = int(java_structure.get('call_graph', {}).get('max_nodes', 800)) if isinstance(java_structure, dict) else 800
        self._method_index, self._class_index = self._build_call_graph_indexes()
        self._class_name_to_fulls, self._class_full_to_method_ids, self._methods_by_class, self._methods_by_name = self._build_fast_call_indexes()
        self._method_refs_cache: Dict[str, List[TableReference]] = {}
        self._method_columns_cache: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

        # ---- progress / stats ----
        # You can override these from CLI (see main) or by setting attributes directly.
        self._progress_enabled: bool = True
        self._progress_class_every: int = 25      # log every N target classes
        self._progress_call_every: int = 200      # log every N visited methods in call-graph traversal
        self._progress_min_interval_sec: float = 2.0  # throttle logs by time
        self._progress_last_ts: float = 0.0

        # Global stats for long-running runs (best-effort)
        self._stats: Dict[str, int] = defaultdict(int)
        self._stats_unique_tables: Set[str] = set()

        # ---- debug (very verbose) ----
        # When enabled, the tool will print per-class details such as:
        # - SQL strings found in screen/batch entry class methods
        # - SQL strings found in called (non screen/batch) classes
        # - SQL candidates ignored due to db_metadata mismatch
        # - call resolution failures / ambiguities
        # - whether traversal was truncated by max_depth / max_nodes
        self._debug_enabled: bool = False
        self._debug_only_problems: bool = True
        self._debug_max_sql_snippets_per_method: int = 6
        self._debug_max_methods_with_sql_per_class: int = 40
        self._debug_max_call_samples: int = 50
        self._debug_sql_preview_len: int = 220

    def _debug(self, msg: str):
        if getattr(self, '_debug_enabled', False):
            print(msg)

    def _shorten(self, s: str, n: int) -> str:
        if s is None:
            return ''
        s = str(s)
        s = s.replace('\n', ' ').replace('\r', ' ').strip()
        return s if len(s) <= n else (s[:n] + ' ...')

    def _fmt_elapsed(self, start_ts: float) -> str:
        try:
            return f"{time.monotonic() - start_ts:.1f}s"
        except Exception:
            return "?"

    def _progress(self, msg: str, *, force: bool = False):
        """Print throttled progress logs (disabled in quiet mode)."""
        if not getattr(self, "_progress_enabled", True):
            return
        now = time.monotonic()
        last = float(getattr(self, "_progress_last_ts", 0.0) or 0.0)
        min_int = float(getattr(self, "_progress_min_interval_sec", 0.0) or 0.0)
        if force or (now - last) >= min_int:
            print(msg)
            self._progress_last_ts = now

    def _build_table_info(self) -> Dict[str, Dict[str, Any]]:
        """テーブル情報辞書を構築"""
        info = {}
        for table in self.db_metadata.get('tables', []):
            info[table['table_name'].upper()] = table
            info[table['table_name'].lower()] = table
        return info

    def _build_call_graph_indexes(self) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        """Build method/class indexes for call graph resolution (fast lookup friendly).

        Returns:
          method_index: { method_id: {class_full, class_name, method_name, param_count, start_line, file, text, calls, sql_strings, has_db_hints, type_references} }
          class_index:  { class_full: {class_name, class_full, file, function_type, genexus_type, type_references} }
        """
        method_index: Dict[str, Dict[str, Any]] = {}
        class_index: Dict[str, Dict[str, Any]] = {}

        for file_entry in self.java_structure.get('files', []) or []:
            file_path = (
                file_entry.get('path')
                or file_entry.get('file_path')
                or file_entry.get('relative_path')
                or file_entry.get('name')
                or file_entry.get('file')
                or ''
            )

            for cls in file_entry.get('classes', []) or []:
                class_name = (cls.get('name') or '').strip()
                package = (cls.get('package') or '').strip()
                class_full = (cls.get('full_name') or '').strip() or (f"{package}.{class_name}" if package else class_name)

                deps = cls.get('dependencies') or {}
                type_refs = deps.get('type_references') or []

                class_index[class_full] = {
                    'class_name': class_name,
                    'class_full': class_full,
                    'file': file_path,
                    'function_type': cls.get('function_type', 'other'),
                    'genexus_type': cls.get('genexus_type'),
                    'type_references': list(type_refs),
                }

                for m in (cls.get('methods', []) or []):
                    mname = (m.get('name') or '').strip()
                    if not mname:
                        continue

                    param_count = m.get('param_count', -1)
                    try:
                        param_count_i = int(param_count) if param_count is not None else -1
                    except Exception:
                        param_count_i = -1

                    start_line = m.get('start_line', 0)
                    try:
                        start_line_i = int(start_line) if start_line is not None else 0
                    except Exception:
                        start_line_i = 0

                    # Build analysis text: excerpt/signature + extracted SQL literals
                    text2 = (m.get('code') or '')
                    if not text2:
                        text2 = (m.get('signature') or '')
                    sql_strings = m.get('sql_strings') or []
                    if isinstance(sql_strings, list) and sql_strings:
                        text2 = text2 + "\n" + "\n".join([str(s) for s in sql_strings])

                    method_id = f"{class_full}::{mname}({param_count_i})@{start_line_i}"
                    method_index[method_id] = {
                        'method_id': method_id,
                        'file': file_path,
                        'class_name': class_name,
                        'class_full': class_full,
                        'method_name': mname,
                        'param_count': param_count_i,
                        'start_line': start_line_i,
                        'text': text2,
                        'sql_strings': list(sql_strings) if isinstance(sql_strings, list) else [],
                        'signature': m.get('signature') or '',
                        'calls': m.get('calls') or [],
                        'type_references': list(type_refs),
                        'has_db_hints': bool(m.get('has_db_hints', False)),
                    }

        return method_index, class_index

    def _build_fast_call_indexes(self):
        """Prebuild fast lookup indexes for call resolution.

        This avoids scanning all methods for every single call, which was a major slowdown.
        """
        class_name_to_fulls: Dict[str, List[str]] = defaultdict(list)
        class_full_to_method_ids: Dict[str, List[str]] = defaultdict(list)

        # methods_by_class[class_full][method_name][param_count] -> [method_id, ...]
        methods_by_class: Dict[str, Dict[str, Dict[int, List[str]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        # methods_by_name[method_name][param_count] -> [method_id, ...] (global fallback)
        methods_by_name: Dict[str, Dict[int, List[str]]] = defaultdict(lambda: defaultdict(list))

        # class name -> full names
        for cls_full, cinfo in (self._class_index or {}).items():
            cname = (cinfo.get('class_name') or '').strip()
            if cname:
                class_name_to_fulls[cname].append(cls_full)

        # method indexes
        for mid, rec in (self._method_index or {}).items():
            cls_full = rec.get('class_full') or ''
            mname = rec.get('method_name') or ''
            try:
                pc = int(rec.get('param_count', -1))
            except Exception:
                pc = -1

            if cls_full:
                class_full_to_method_ids[cls_full].append(mid)
            if mname:
                methods_by_class[cls_full][mname][pc].append(mid)
                methods_by_name[mname][pc].append(mid)

        # De-duplicate lists while preserving order (important for stable output)
        def dedup_list(xs: List[str]) -> List[str]:
            seen = set()
            out = []
            for x in xs:
                if x in seen:
                    continue
                seen.add(x)
                out.append(x)
            return out

        for k in list(class_name_to_fulls.keys()):
            class_name_to_fulls[k] = dedup_list(class_name_to_fulls[k])
        for k in list(class_full_to_method_ids.keys()):
            class_full_to_method_ids[k] = dedup_list(class_full_to_method_ids[k])

        # deep structures: de-dup leaf lists
        for cf, m_map in list(methods_by_class.items()):
            for mn, pc_map in list(m_map.items()):
                for pc, mids in list(pc_map.items()):
                    pc_map[pc] = dedup_list(mids)
        for mn, pc_map in list(methods_by_name.items()):
            for pc, mids in list(pc_map.items()):
                pc_map[pc] = dedup_list(mids)

        return class_name_to_fulls, class_full_to_method_ids, methods_by_class, methods_by_name

    def _resolve_call_candidates(self, caller_class_full: str, call: Dict[str, Any]) -> List[str]:
        """Resolve a call dict to candidate method_id list.

        IMPORTANT: In java_structure.json (as confirmed by your sample),
          - call["qualifier"] is the *callee class name* (or an expression like "new Xxx()")
          - call["name"] is the *callee method name*
        This implementation prioritizes qualifier->class resolution and uses prebuilt indexes.
        """
        name = (call.get('name') or '').strip()
        if not name or name in _IGNORE_METHOD_NAMES:
            return []

        try:
            arg_count = int(call.get('arg_count', -1))
        except Exception:
            arg_count = -1

        qualifier_raw = call.get('qualifier')
        qualifier = _simplify_qualifier(qualifier_raw)  # may return None

        # Fast helper: get methods in a given class_full for (name, arg_count)
        def methods_in_class(class_full: str) -> List[str]:
            out: List[str] = []
            m_map = (self._methods_by_class.get(class_full) or {}).get(name)
            if not m_map:
                return out

            if arg_count >= 0:
                out.extend(m_map.get(arg_count, []))
                # allow unknown param_count(-1) as compatible
                out.extend(m_map.get(-1, []))
            else:
                # unknown arg_count: take all overloads
                for mids in m_map.values():
                    out.extend(mids)
            return out

        def methods_in_class_fulls(class_fulls: List[str], cap: int = 40) -> List[str]:
            seen = set()
            out: List[str] = []
            for cf in class_fulls:
                for mid in methods_in_class(cf):
                    if mid in seen:
                        continue
                    seen.add(mid)
                    out.append(mid)
                    if len(out) >= cap:
                        return out
            return out

        cands: List[str] = []

        # 1) this/super/unqualified => same class first
        if (not qualifier) or qualifier in ('this', 'super'):
            cands.extend(methods_in_class_fulls([caller_class_full], cap=40))

        # 2) qualifier resolves to an existing class name (do NOT require uppercase)
        if qualifier and qualifier not in ('this', 'super'):
            fulls = self._class_name_to_fulls.get(qualifier, [])
            if fulls:
                cands.extend(methods_in_class_fulls(fulls, cap=60))

        # 3) If still empty, use caller's referenced types as a constraint
        if not cands:
            type_refs = (self._class_index.get(caller_class_full) or {}).get('type_references') or []
            for t in type_refs:
                fulls = self._class_name_to_fulls.get(str(t), [])
                if not fulls:
                    continue
                cands.extend(methods_in_class_fulls(fulls, cap=60))
                if cands:
                    break

        # 4) Global fallback by method name (cap hard to prevent explosion)
        if not cands:
            pc_map = self._methods_by_name.get(name) or {}
            out: List[str] = []
            if arg_count >= 0:
                out.extend(pc_map.get(arg_count, []))
                out.extend(pc_map.get(-1, []))
            else:
                for mids in pc_map.values():
                    out.extend(mids)
            cands = out[:20]

        # Deduplicate
        seen = set()
        uniq: List[str] = []
        for c in cands:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        return uniq

    def _collect_references_for_class(self, cls_data: Dict[str, Any]) -> Tuple[List[TableReference], Dict[str, List[Dict[str, Any]]], Set[str], Dict[str, Any]]:
        """Collect table references by traversing the call graph starting from a class' methods.

        Returns: (refs, columns_used_map, related_classes, debug_info)
        """
        class_name = cls_data.get('name', '')
        class_full = cls_data.get('full_name') or (f"{cls_data.get('package')}.{class_name}" if cls_data.get('package') else class_name)

        debug_info: Dict[str, Any] = {
            'entry_class': class_name,
            'entry_class_full': class_full,
            'entry_method_count': 0,
            'visited_methods': 0,
            'direct_sql_methods': [],
            'indirect_sql_methods': [],
            'db_hints_counter': defaultdict(int),
            'db_hints_samples': [],
            'ignored_sql_candidates': defaultdict(int),
            'ignored_sql_tokens_sample': [],
            'unresolved_calls': 0,
            'ambiguous_calls': 0,
            'call_samples': [],
            'truncated_by_nodes': False,
            'queue_remaining_when_truncated': 0,
            'skipped_calls_by_depth': 0,
            'max_depth_seen': 0,
        }

        # Entry methods: use prebuilt mapping (fast), avoid scanning all methods.
        entry_method_ids: List[str] = list(self._class_full_to_method_ids.get(class_full, []))
        debug_info['entry_method_count'] = len(entry_method_ids)

        # If no method index (older json) -> fallback to in-class extraction only
        if not entry_method_ids:
            all_references: List[TableReference] = []
            columns_used_map: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for m in cls_data.get('methods', []) or []:
                method_code = (m.get('code') or '')
                if not method_code:
                    method_code = (m.get('signature') or '')
                sql_strings = m.get('sql_strings') or []
                if sql_strings:
                    method_code = method_code + "\n" + "\n".join(sql_strings)
                refs = self.extractor.extract_from_code(method_code, class_name, m.get('name'))
                all_references.extend(refs)
                for r in refs:
                    tinfo = self.table_info.get(r.table_name.upper(), {})
                    cols = tinfo.get('columns', [])
                    cols_used = _extract_used_columns(method_code, cols)
                    if cols_used:
                        columns_used_map[r.table_name.upper()] = cols_used
            return all_references, columns_used_map, set(), debug_info

        # Heuristic: start from "interesting" entry methods to avoid exploding the traversal.
        def is_interesting(mid: str) -> bool:
            rec = self._method_index.get(mid) or {}
            if rec.get('calls'):
                return True
            if rec.get('sql_strings'):
                return True
            if rec.get('has_db_hints'):
                return True
            return False

        filtered_entry = [mid for mid in entry_method_ids if is_interesting(mid)]
        if filtered_entry:
            entry_method_ids = filtered_entry

        # Call graph traversal (deque + enqueued de-dup for performance)
        visited: Set[str] = set()
        enqueued: Set[str] = set(entry_method_ids)
        queue: deque = deque([(mid, 0) for mid in entry_method_ids])

        all_refs: List[TableReference] = []
        columns_used_map: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        related_classes: Set[str] = set()

        cg_start_ts = time.monotonic()
        refs_total = 0
        cache_hits = 0
        unique_tables: Set[str] = set()

        while queue and len(visited) < self._call_max_nodes:
            mid, depth = queue.popleft()
            if mid in visited:
                continue
            visited.add(mid)
            if depth > debug_info.get('max_depth_seen', 0):
                debug_info['max_depth_seen'] = depth

            self._stats['methods_visited'] = self._stats.get('methods_visited', 0) + 1

            rec = self._method_index.get(mid) or {}
            text = rec.get('text') or ''
            src_class = rec.get('class_name') or class_name
            src_method = rec.get('method_name')

            # Debug: collect SQL strings/candidates even if they don't map to db_metadata
            if getattr(self, '_debug_enabled', False):
                sql_strings = rec.get('sql_strings') or []
                candidates = self.extractor.debug_scan_sql_candidates(text)
                hints = self.extractor.debug_scan_db_hints(text)
                if rec.get('has_db_hints') and not any(h.get('hint_type') == 'FLAG:has_db_hints' for h in hints):
                    hints = ([{'hint_type': 'FLAG:has_db_hints', 'match': ''}] + hints)[:12]
                for h in hints:
                    ht = (h.get('hint_type') or 'UNKNOWN')
                    debug_info['db_hints_counter'][ht] += 1
                    if len(debug_info['db_hints_samples']) < 30:
                        debug_info['db_hints_samples'].append(self._shorten(f"{ht}:{h.get('match')}", 120))

                for c in candidates:
                    if not c.get('is_valid') and c.get('normalized'):
                        debug_info['ignored_sql_candidates'][str(c.get('normalized')).upper()] += 1
                        if len(debug_info['ignored_sql_tokens_sample']) < 30:
                            debug_info['ignored_sql_tokens_sample'].append(self._shorten(str(c.get('raw_token')), 80))

                has_any_sql_signal = bool(sql_strings) or bool(candidates) or bool(hints)
                if has_any_sql_signal:
                    item = {
                        'class_full': rec.get('class_full') or class_full,
                        'class_name': rec.get('class_name') or src_class,
                        'method': src_method,
                        'method_id': mid,
                        'depth': depth,
                        'sql_strings': [self._shorten(s, self._debug_sql_preview_len) for s in (sql_strings[: self._debug_max_sql_snippets_per_method])],
                        'db_hints': hints[:12],
                        'sql_candidates': [c for c in candidates if c.get('is_valid')][:15],
                        'sql_candidates_ignored': [c for c in candidates if not c.get('is_valid')][:15],
                    }
                    if (rec.get('class_full') or class_full) == class_full:
                        if len(debug_info['direct_sql_methods']) < self._debug_max_methods_with_sql_per_class:
                            debug_info['direct_sql_methods'].append(item)
                    else:
                        if len(debug_info['indirect_sql_methods']) < self._debug_max_methods_with_sql_per_class:
                            debug_info['indirect_sql_methods'].append(item)

            # Extract refs (cached)
            if mid in self._method_refs_cache:
                refs = self._method_refs_cache[mid]
                cols_map = self._method_columns_cache.get(mid) or {}
                cache_hits += 1
                self._stats['cache_hits'] = self._stats.get('cache_hits', 0) + 1
            else:
                refs = self.extractor.extract_from_code(text, src_class, src_method)
                cols_map: Dict[str, List[Dict[str, Any]]] = {}
                for r in refs:
                    tinfo = self.table_info.get(r.table_name.upper(), {})
                    cols = tinfo.get('columns', [])
                    cols_used = _extract_used_columns(text, cols)
                    if cols_used:
                        cols_map[r.table_name.upper()] = cols_used
                self._method_refs_cache[mid] = refs
                self._method_columns_cache[mid] = cols_map

            all_refs.extend(refs)
            refs_total += len(refs)
            self._stats['table_refs'] = self._stats.get('table_refs', 0) + len(refs)

            for r in refs:
                tn = (r.table_name or '').upper()
                if tn:
                    unique_tables.add(tn)
                    self._stats_unique_tables.add(tn)

            if self._progress_call_every > 0 and len(visited) % self._progress_call_every == 0:
                self._progress(
                    f"[進捗] callgraph {class_name}: visited={len(visited)}/{self._call_max_nodes} queue={len(queue)} depth={depth}/{self._call_max_depth} refs={refs_total} unique_tables={len(unique_tables)} cache_hits={cache_hits} elapsed={self._fmt_elapsed(cg_start_ts)}"
                )

            for t, cols in (cols_map or {}).items():
                existing = {c.get('name') for c in columns_used_map.get(t, [])}
                for c in cols:
                    if c.get('name') not in existing:
                        columns_used_map[t].append(c)
                        existing.add(c.get('name'))

            # Follow calls
            if depth >= self._call_max_depth:
                calls_here = rec.get('calls') or []
                if calls_here:
                    debug_info['skipped_calls_by_depth'] += len(calls_here)
                continue

            caller_cf = rec.get('class_full') or class_full
            for call in rec.get('calls') or []:
                cands = self._resolve_call_candidates(caller_cf, call)
                if not cands:
                    debug_info['unresolved_calls'] += 1
                    if getattr(self, '_debug_enabled', False) and len(debug_info['call_samples']) < self._debug_max_call_samples:
                        debug_info['call_samples'].append({
                            'kind': 'unresolved',
                            'depth': depth,
                            'caller': {
                            'method_id': mid,
                            'file': rec.get('file'),
                            'class_full': rec.get('class_full') or class_full,
                            'class_name': rec.get('class_name') or src_class,
                            'method': src_method,
                        },
                        'call': {
                            'name': call.get('name'),
                            'qualifier': call.get('qualifier'),
                            'arg_count': call.get('arg_count'),
                        },
                        'resolved_count': 0,
                        })
                    continue

                if len(cands) > 1:
                    debug_info['ambiguous_calls'] += 1
                    if getattr(self, '_debug_enabled', False) and len(debug_info['call_samples']) < self._debug_max_call_samples:
                        debug_info['call_samples'].append({
                            'kind': 'ambiguous',
                            'depth': depth,
                            'caller': mid,
                            'call': {
                                'name': call.get('name'),
                                'qualifier': call.get('qualifier'),
                                'arg_count': call.get('arg_count'),
                            },
                            'resolved_count': len(cands),
                            'resolved_sample': [
                            {
                                'method_id': x,
                                'file': (self._method_index.get(x) or {}).get('file'),
                                'class_full': (self._method_index.get(x) or {}).get('class_full'),
                                'class_name': (self._method_index.get(x) or {}).get('class_name'),
                                'method': (self._method_index.get(x) or {}).get('method_name'),
                            }
                            for x in cands[:5]
                        ],
                        })

                for cmid in cands:
                    if cmid in visited or cmid in enqueued:
                        continue

                    # Skip "boring leaf" methods: no calls/hints/sql. (performance)
                    crec = self._method_index.get(cmid) or {}
                    if not (crec.get('calls') or crec.get('sql_strings') or crec.get('has_db_hints')):
                        continue

                    enqueued.add(cmid)
                    queue.append((cmid, depth + 1))

                    callee_cls_full = crec.get('class_full')
                    if callee_cls_full and callee_cls_full != class_full:
                        related_classes.add((self._class_index.get(callee_cls_full) or {}).get('class_name', callee_cls_full))

        if queue and len(visited) >= self._call_max_nodes:
            debug_info['truncated_by_nodes'] = True
            debug_info['queue_remaining_when_truncated'] = len(queue)

        debug_info['visited_methods'] = len(visited)

        if isinstance(debug_info.get('ignored_sql_candidates'), defaultdict):
            debug_info['ignored_sql_candidates'] = dict(debug_info['ignored_sql_candidates'])
        if isinstance(debug_info.get('db_hints_counter'), defaultdict):
            debug_info['db_hints_counter'] = dict(debug_info['db_hints_counter'])

        self._progress(
            f"[進捗] callgraph done {class_name}: visited={len(visited)} refs={refs_total} unique_tables={len(unique_tables)} related_classes={len(related_classes)} cache_hits={cache_hits} elapsed={self._fmt_elapsed(cg_start_ts)}",
            force=False
        )

        return all_refs, columns_used_map, related_classes, debug_info
    
    def restore_design(self) -> SystemDesign:
        """システム設計を還元"""
        start_ts = time.monotonic()
        functions = []
        table_function_map = defaultdict(list)

        # 対象クラス（screen/batch）の総数を事前集計して進捗を出す
        target_classes: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for fe in self.java_structure.get('files', []):
            for cd in fe.get('classes', []) or []:
                ft = cd.get('function_type', 'other')
                if ft in ('screen', 'batch'):
                    target_classes.append((fe, cd))

        total_targets = len(target_classes)
        self._progress(f"[進捗] 解析開始: 対象クラス(screen/batch)={total_targets} / call_depth={self._call_max_depth} / call_nodes={self._call_max_nodes}", force=True)

        processed = 0

        # 対象クラスを1件ずつ解析
        for file_entry, cls_data in target_classes:
            processed += 1
            if self._progress_class_every > 0 and processed % self._progress_class_every == 0:
                self._progress(f"[進捗] クラス進捗: {processed}/{total_targets} elapsed={self._fmt_elapsed(start_ts)}")

            func_design = self._restore_function(cls_data, file_entry)
            if func_design:
                functions.append(func_design)

                # テーブル-機能マッピング更新
                for table in func_design.tables_used:
                    table_function_map[table['table_name']].append(func_design.function_id)
        
        self._progress(
            f"[進捗] 解析完了: functions={len(functions)} methods_visited={self._stats.get('methods_visited',0)} cache_hits={self._stats.get('cache_hits',0)} refs={self._stats.get('table_refs',0)} unique_tables={len(self._stats_unique_tables)} elapsed={self._fmt_elapsed(start_ts)}",
            force=True
        )

        # 機能グループ化
        grouped_functions = self._group_functions(functions)
        
        return SystemDesign(
            project_name=self.java_structure.get('project_root', 'Unknown'),
            functions=grouped_functions,
            table_function_matrix=dict(table_function_map),
            er_diagram_data=self._build_er_data(),
        )

    def _should_print_debug_report(self, debug_info: Dict[str, Any], refs: List[TableReference]) -> bool:
        """Decide whether to print a debug report for this class."""
        if not getattr(self, '_debug_enabled', False):
            return False
        if not getattr(self, '_debug_only_problems', True):
            return True
        # problem heuristics
        if not refs:
            return True
        if debug_info.get('truncated_by_nodes'):
            return True
        if (debug_info.get('skipped_calls_by_depth') or 0) > 0:
            return True
        if (debug_info.get('unresolved_calls') or 0) > 0:
            return True
        if (debug_info.get('ambiguous_calls') or 0) > 0:
            return True
        if debug_info.get('ignored_sql_candidates'):
            return True
        return False

    def _print_debug_report(self, class_name: str, function_type: str, debug_info: Dict[str, Any], refs: List[TableReference]):
        """Print a per-class debug report."""
        if not getattr(self, '_debug_enabled', False):
            return

        uniq_tables = sorted({(r.table_name or '').upper() for r in (refs or []) if (r.table_name or '').strip()})
        self._debug("\n" + "=" * 92)
        self._debug(f"[DEBUG] Target: {function_type}:{class_name}")
        self._debug(f"[DEBUG] entry_class_full={debug_info.get('entry_class_full')} entry_methods={debug_info.get('entry_method_count')} visited={debug_info.get('visited_methods')} max_depth_seen={debug_info.get('max_depth_seen')} max_depth_limit={self._call_max_depth} max_nodes_limit={self._call_max_nodes}")
        self._debug(f"[DEBUG] refs_found={len(refs or [])} unique_tables_found={len(uniq_tables)} tables={uniq_tables[:30]}{' ...' if len(uniq_tables)>30 else ''}")

        # Depth/node truncation analysis
        trunc_nodes = bool(debug_info.get('truncated_by_nodes'))
        skipped_depth = int(debug_info.get('skipped_calls_by_depth') or 0)
        self._debug(f"[DEBUG] traversal_limits: truncated_by_nodes={trunc_nodes} queue_remaining={debug_info.get('queue_remaining_when_truncated')} skipped_calls_by_depth={skipped_depth}")

        # DB hint summary (methods that look like DB access even if tables were not extracted)
        hints_counter = debug_info.get('db_hints_counter') or {}
        if hints_counter:
            top_hints = sorted(hints_counter.items(), key=lambda kv: kv[1], reverse=True)[:20]
            self._debug("[DEBUG] db_hints_top: " + ", ".join([f"{k}={v}" for k, v in top_hints]))
            samples = debug_info.get('db_hints_samples') or []
            if samples:
                self._debug("[DEBUG] db_hints_samples: " + "; ".join(samples[:10]))

        # Candidates ignored due to db_metadata mismatch
        ignored = debug_info.get('ignored_sql_candidates') or {}
        if ignored:
            top = sorted([(k, v) for k, v in ignored.items()], key=lambda x: x[1], reverse=True)[:20]
            self._debug("[DEBUG] ignored_sql_candidates(not_in_db_metadata) top20: " + ", ".join([f"{k}×{v}" for k, v in top]))
            samples = debug_info.get('ignored_sql_tokens_sample') or []
            if samples:
                self._debug("[DEBUG] ignored_sql_tokens_sample: " + ", ".join(samples[:20]) + (" ..." if len(samples) > 20 else ""))

        # SQL in entry class vs in called classes
        direct = debug_info.get('direct_sql_methods') or []
        indirect = debug_info.get('indirect_sql_methods') or []

        def dump_sql_items(title: str, items: List[Dict[str, Any]]):
            if not items:
                return
            self._debug(f"[DEBUG] {title}: methods_with_sql_signal={len(items)} (showing up to {self._debug_max_methods_with_sql_per_class})")
            for it in items[: self._debug_max_methods_with_sql_per_class]:
                mid = it.get('method_id')
                mname = it.get('method')
                d = it.get('depth')
                self._debug(f"  - {it.get('class_name')}::{mname} depth={d} mid={self._shorten(mid, 90)}")
                sqls = it.get('sql_strings') or []
                if sqls:
                    for s in sqls[: self._debug_max_sql_snippets_per_method]:
                        self._debug(f"      SQL: {self._shorten(s, self._debug_sql_preview_len)}")
                hints = it.get('db_hints') or []
                if hints:
                    self._debug("      db_hints: " + ", ".join([str(h.get('hint_type')) for h in hints[:12]]))
                c_ok = it.get('sql_candidates') or []
                c_ng = it.get('sql_candidates_ignored') or []
                if c_ok:
                    self._debug("      candidates_ok: " + ", ".join([f"{c.get('op_type')}:{c.get('normalized')}" for c in c_ok[:10]]))
                if c_ng:
                    self._debug("      candidates_ignored: " + ", ".join([f"{c.get('op_type')}:{c.get('normalized')}({c.get('reason')})" for c in c_ng[:10]]))

        dump_sql_items("SQL in entry(screen/batch) class", direct)
        dump_sql_items("SQL in called(other) classes", indirect)

        # Call resolution issues
        unresolved = int(debug_info.get('unresolved_calls') or 0)
        ambiguous = int(debug_info.get('ambiguous_calls') or 0)
        if unresolved or ambiguous:
            self._debug(f"[DEBUG] call_resolution: unresolved_calls={unresolved} ambiguous_calls={ambiguous}")
            for s in (debug_info.get('call_samples') or [])[: self._debug_max_call_samples]:
                self._debug(f"  - {s.get('kind')} depth={s.get('depth')} caller={self._shorten(s.get('caller'), 80)} call={s.get('call')} resolved_count={s.get('resolved_count')} sample={s.get('resolved_sample','')}")

        # Final conclusion line for 'why nothing was extracted'
        if not refs:
            reason_bits: List[str] = []
            if trunc_nodes:
                reason_bits.append("node数上限で探索が途中終了")
            if skipped_depth:
                reason_bits.append("depth上限で一部呼び出し追跡をスキップ")
            if ignored:
                reason_bits.append("SQLは検出したがテーブル名がdb_metadataに存在せず除外")
            if unresolved or ambiguous:
                reason_bits.append("呼び出し解決が不十分(未解決/曖昧)")
            if not reason_bits:
                reason_bits.append("SQL候補が見つからない/抽出パターン外")
            self._debug("[DEBUG] conclusion(no_refs): " + " / ".join(reason_bits))

        self._debug("=" * 92 + "\n")
    
    def _restore_function(self, cls_data: Dict[str, Any], 
                          file_entry: Dict[str, Any]) -> Optional[FunctionDesign]:
        """単一機能の設計を還元"""
        class_name = cls_data.get('name', '')
        function_type = cls_data.get('function_type', 'other')
        genexus_type = cls_data.get('genexus_type')

        # テーブル参照を抽出（コールグラフ追跡により other クラス内 SQL も拾う）
        all_references, columns_used_map, related_classes, debug_info = self._collect_references_for_class(cls_data)
        if self._should_print_debug_report(debug_info, all_references):
            self._print_debug_report(class_name, function_type, debug_info, all_references)
        if not all_references:
            return None
        
        # 使用テーブル集計
        tables_used = self._aggregate_tables(all_references, columns_used_map)
        
        # CRUD操作マトリックス
        crud_matrix = self._build_crud_matrix(all_references)
        
        # 機能名推測
        function_name = self._infer_function_name(class_name, tables_used, genexus_type)
        
        return FunctionDesign(
            function_name=function_name,
            function_id=class_name,
            function_type=function_type,
            genexus_type=genexus_type,
            description=self._generate_description(class_name, tables_used, crud_matrix),
            entry_classes=[class_name],
            related_classes=sorted(list(related_classes)) if related_classes else [],
            tables_used=tables_used,
            crud_matrix=crud_matrix,
        )
    
    def _aggregate_tables(self, references: List[TableReference], columns_used_map: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> List[Dict[str, Any]]:
        """テーブル参照を集計"""
        table_map = {}
        
        for ref in references:
            key = ref.table_name.upper()
            if key not in table_map:
                table_info = self.table_info.get(key, {})
                table_map[key] = {
                    'table_name': ref.table_name,
                    'logical_name': ref.logical_name,
                    'operations': set(),
                    'column_count': len(table_info.get('columns', [])),
                    'columns_used': [],
                    'columns': [
                        {
                            'name': col.get('name'),
                            'logical_name': col.get('logical_name'),
                            'data_type': col.get('data_type'),
                            'is_primary_key': col.get('is_primary_key', False),
                        }
                        for col in table_info.get('columns', [])[:10]  # 主要カラムのみ
                    ],
                }
            table_map[key]['operations'].add(ref.operation_type)

            # Merge heuristic used columns
            if columns_used_map:
                cols_used = columns_used_map.get(key) or columns_used_map.get(ref.table_name.upper())
                if cols_used:
                    existing = {c.get('name') for c in table_map[key].get('columns_used', [])}
                    for c in cols_used:
                        if c.get('name') and c.get('name') not in existing:
                            table_map[key]['columns_used'].append(c)
                            existing.add(c.get('name'))
        
        # setをlistに変換
        for table in table_map.values():
            table['operations'] = list(table['operations'])
        
        return list(table_map.values())
    
    def _build_crud_matrix(self, references: List[TableReference]) -> Dict[str, List[str]]:
        """CRUD操作マトリックスを構築"""
        matrix = {'CREATE': [], 'READ': [], 'UPDATE': [], 'DELETE': []}
        
        for ref in references:
            table_name = ref.table_name.upper()
            op = ref.operation_type.upper()
            
            if 'INSERT' in op:
                if table_name not in matrix['CREATE']:
                    matrix['CREATE'].append(table_name)
            if 'SELECT' in op or 'READ' in op:
                if table_name not in matrix['READ']:
                    matrix['READ'].append(table_name)
            if 'UPDATE' in op:
                if table_name not in matrix['UPDATE']:
                    matrix['UPDATE'].append(table_name)
            if 'DELETE' in op:
                if table_name not in matrix['DELETE']:
                    matrix['DELETE'].append(table_name)
        
        return matrix
    
    def _infer_function_name(self, class_name: str, tables_used: List[Dict],
                             genexus_type: Optional[str]) -> str:
        """機能名を推測"""
        # テーブルの論理名から推測
        if tables_used:
            main_table = tables_used[0]
            table_logical = main_table.get('logical_name', '')
            
            if genexus_type:
                type_suffix = {
                    'WebPanel': '画面',
                    'Transaction': '登録画面',
                    'WorkWithPlus': '一覧画面',
                    'Procedure': '処理',
                    'DataProvider': 'データ取得',
                    'Report': '帳票',
                }.get(genexus_type, '')
                
                if table_logical and table_logical != main_table['table_name']:
                    return f"{table_logical}{type_suffix}"
        
        # クラス名から推測
        return self._class_name_to_japanese(class_name)
    
    def _class_name_to_japanese(self, class_name: str) -> str:
        """クラス名から日本語名を推測"""
        # プレフィックス/サフィックスマッピング
        keywords = {
            'list': '一覧', 'detail': '詳細', 'edit': '編集', 'entry': '登録',
            'search': '検索', 'inquiry': '照会', 'inq': '照会',
            'report': '帳票', 'rpt': '帳票', 'print': '印刷',
            'export': 'エクスポート', 'import': 'インポート',
            'batch': 'バッチ', 'proc': '処理', 'calc': '計算',
            'order': '受注', 'ord': '受注', 'purchase': '発注', 'po': '発注',
            'customer': '得意先', 'cust': '得意先', 'supplier': '仕入先', 'sup': '仕入先',
            'product': '商品', 'prd': '商品', 'item': '品目', 'itm': '品目',
            'inventory': '在庫', 'inv': '在庫', 'stock': '在庫', 'stk': '在庫',
            'user': 'ユーザー', 'usr': 'ユーザー', 'employee': '従業員', 'emp': '従業員',
            'master': 'マスタ', 'mst': 'マスタ', 'maintenance': 'メンテナンス',
            'home': 'ホーム', 'menu': 'メニュー', 'login': 'ログイン',
        }
        
        name_lower = class_name.lower()
        parts = []
        
        for key, value in keywords.items():
            if key in name_lower:
                parts.append(value)
        
        if parts:
            return ''.join(parts)
        
        return class_name
    
    def _generate_description(self, class_name: str, tables_used: List[Dict],
                              crud_matrix: Dict[str, List[str]]) -> str:
        """機能説明を生成"""
        desc_parts = []
        
        # 使用テーブル
        if tables_used:
            table_names = [t['logical_name'] for t in tables_used[:3]]
            desc_parts.append(f"対象テーブル: {', '.join(table_names)}")
        
        # 操作内容
        ops = []
        if crud_matrix['CREATE']:
            ops.append('登録')
        if crud_matrix['READ']:
            ops.append('参照')
        if crud_matrix['UPDATE']:
            ops.append('更新')
        if crud_matrix['DELETE']:
            ops.append('削除')
        
        if ops:
            desc_parts.append(f"操作: {'/'.join(ops)}")
        
        return ' | '.join(desc_parts)
    
    def _group_functions(self, functions: List[FunctionDesign]) -> List[FunctionDesign]:
        """関連機能をグループ化"""
        # プレフィックスでグループ化
        groups = defaultdict(list)
        
        for func in functions:
            prefix = self._extract_prefix(func.function_id)
            groups[prefix].append(func)
        
        # グループ内で関連クラスを設定
        for prefix, group_funcs in groups.items():
            if len(group_funcs) > 1:
                for func in group_funcs:
                    func.related_classes = [
                        f.function_id for f in group_funcs 
                        if f.function_id != func.function_id
                    ]
        
        return functions
    
    def _extract_prefix(self, class_name: str) -> str:
        """クラス名からプレフィックスを抽出"""
        if '_' in class_name:
            return class_name.split('_')[0].lower()
        
        match = re.match(r'^([A-Z]?[a-z]+)', class_name)
        if match:
            return match.group(1).lower()
        
        return class_name[:3].lower()
    
    def _build_er_data(self) -> Dict[str, Any]:
        """ER図データを構築"""
        tables = []
        relationships = []
        
        for table in self.db_metadata.get('tables', []):
            tables.append({
                'name': table['table_name'],
                'logical_name': table.get('logical_name', table['table_name']),
                'columns': [
                    {
                        'name': col['name'],
                        'logical_name': col.get('logical_name', col['name']),
                        'type': col['data_type'],
                        'pk': col.get('is_primary_key', False),
                        'fk': col.get('is_foreign_key', False),
                    }
                    for col in table.get('columns', [])
                ],
            })
        
        for fk in self.db_metadata.get('foreign_keys', []):
            relationships.append({
                'from_table': fk['from_table'],
                'to_table': fk['to_table'],
                'from_columns': fk['from_columns'],
                'to_columns': fk['to_columns'],
                'type': 'many-to-one',  # 簡略化
            })
        
        return {
            'tables': tables,
            'relationships': relationships,
        }


# ---------- 出力フォーマット ----------

def format_design_document(design: SystemDesign) -> Dict[str, Any]:
    """設計ドキュメントをフォーマット"""
    
    # 機能一覧
    function_list = []
    for func in design.functions:
        function_list.append({
            'id': func.function_id,
            'name': func.function_name,
            'type': func.function_type,
            'genexus_type': func.genexus_type,
            'description': func.description,
            'tables': [
                {
                    'name': t['table_name'],
                    'logical_name': t['logical_name'],
                    'operations': t['operations'],
                }
                for t in func.tables_used
            ],
            'crud_matrix': func.crud_matrix,
            'related_classes': func.related_classes,
        })
    
    # 統計情報
    stats = {
        'total_functions': len(design.functions),
        'screen_functions': len([f for f in design.functions if f.function_type == 'screen']),
        'batch_functions': len([f for f in design.functions if f.function_type == 'batch']),
        'total_tables': len(design.er_diagram_data.get('tables', [])),
        'total_relationships': len(design.er_diagram_data.get('relationships', [])),
    }
    
    return {
        'project_name': design.project_name,
        'statistics': stats,
        'functions': function_list,
        'table_function_matrix': design.table_function_matrix,
        'er_diagram': design.er_diagram_data,
    }


def print_design_summary(design_doc: Dict[str, Any]):
    """設計サマリーを出力"""
    stats = design_doc.get('statistics', {})
    functions = design_doc.get('functions', [])
    
    print("\n" + "=" * 70)
    print("システム設計還元サマリー")
    print("=" * 70)
    
    print(f"\n【統計情報】")
    print(f"  機能数: {stats.get('total_functions', 0)}")
    print(f"    - 画面機能: {stats.get('screen_functions', 0)}")
    print(f"    - バッチ機能: {stats.get('batch_functions', 0)}")
    print(f"  テーブル数: {stats.get('total_tables', 0)}")
    print(f"  リレーション数: {stats.get('total_relationships', 0)}")
    
    print(f"\n【機能一覧】")
    
    # 画面機能
    screen_funcs = [f for f in functions if f['type'] == 'screen']
    if screen_funcs:
        print(f"\n  ■ 画面機能 ({len(screen_funcs)}件)")
        for func in screen_funcs[:10]:
            tables = ', '.join([t['logical_name'] for t in func['tables'][:3]])
            gx_type = f"[{func['genexus_type']}]" if func['genexus_type'] else ""
            print(f"    · {func['name']} {gx_type}")
            print(f"      テーブル: {tables}")
    
    # バッチ機能
    batch_funcs = [f for f in functions if f['type'] == 'batch']
    if batch_funcs:
        print(f"\n  ■ バッチ機能 ({len(batch_funcs)}件)")
        for func in batch_funcs[:10]:
            tables = ', '.join([t['logical_name'] for t in func['tables'][:3]])
            gx_type = f"[{func['genexus_type']}]" if func['genexus_type'] else ""
            print(f"    · {func['name']} {gx_type}")
            print(f"      テーブル: {tables}")
    
    # テーブル-機能マトリックス
    matrix = design_doc.get('table_function_matrix', {})
    if matrix:
        print(f"\n【テーブル-機能マトリックス】(主要テーブル)")
        for table_name, func_ids in list(matrix.items())[:10]:
            er_tables = {t['name']: t for t in design_doc.get('er_diagram', {}).get('tables', [])}
            logical_name = er_tables.get(table_name, {}).get('logical_name', table_name)
            print(f"    · {logical_name} ({table_name})")
            print(f"      使用機能: {len(func_ids)}件")
    
    print()


# ---------- CLIエントリポイント ----------

def main():
    parser = argparse.ArgumentParser(
        description="コード-データベース関連分析ツール - 機能設計を還元"
    )
    parser.add_argument("java_structure", help="Java構造JSONファイル")
    parser.add_argument("db_metadata", help="データベースメタデータJSONファイル")
    parser.add_argument("-o", "--output", default="design_document.json",
                        help="出力設計ドキュメントJSONファイル")
    parser.add_argument("-q", "--quiet", action="store_true", help="サイレントモード")
    parser.add_argument("--call-depth", type=int, default=8,
                        help="コールグラフ追跡の最大深さ (default: 8)")
    parser.add_argument("--call-nodes", type=int, default=800,
                        help="コールグラフ追跡の最大ノード数 (default: 800)")

    # debug (very verbose)
    parser.add_argument("--debug", action="store_true",
                        help="詳細デバッグログを表示（SQL/呼び出し/探索制限の理由を出力）")
    parser.add_argument("--debug-all", action="store_true",
                        help="問題がない場合も含め、全対象クラスのデバッグログを表示")
    parser.add_argument("--debug-sql-preview-len", type=int, default=220,
                        help="デバッグ出力時のSQLプレビュー最大長 (default: 220)")
    parser.add_argument("--debug-max-sql-per-method", type=int, default=6,
                        help="1メソッドあたり表示するSQL文字列の上限 (default: 6)")
    parser.add_argument("--debug-max-methods-per-class", type=int, default=40,
                        help="1クラスあたりSQLを含むメソッドの表示上限 (default: 40)")
    parser.add_argument("--debug-max-call-samples", type=int, default=50,
                        help="未解決/曖昧呼び出しサンプルの表示上限 (default: 50)")

    # progress logs
    parser.add_argument("--progress", action="store_true",
                        help="進捗ログを表示（--quiet を上書き）")
    parser.add_argument("--progress-every-classes", type=int, default=25,
                        help="クラス進捗ログの間隔（対象クラス N 件ごと）")
    parser.add_argument("--progress-every-calls", type=int, default=200,
                        help="コールグラフ進捗ログの間隔（visited メソッド N 件ごと）")
    parser.add_argument("--progress-min-seconds", type=float, default=2.0,
                        help="進捗ログの最小出力間隔（秒）")

    args = parser.parse_args()
    
    java_path = Path(args.java_structure)
    db_path = Path(args.db_metadata)
    output_path = Path(args.output)
    
    if not java_path.exists():
        raise SystemExit(f"Java構造ファイルが存在しません: {java_path}")
    if not db_path.exists():
        raise SystemExit(f"DBメタデータファイルが存在しません: {db_path}")
    
    print(f"[情報] Java構造を読み込み中: {java_path}")
    java_structure = json.loads(java_path.read_text(encoding='utf-8'))
    
    print(f"[情報] DBメタデータを読み込み中: {db_path}")
    db_metadata = json.loads(db_path.read_text(encoding='utf-8'))
    
    print(f"[情報] 機能設計を還元中...")
    restorer = FunctionDesignRestorer(java_structure, db_metadata)
    # progress settings
    restorer._progress_enabled = bool(args.progress or (not args.quiet))
    restorer._progress_class_every = max(1, int(args.progress_every_classes))
    restorer._progress_call_every = max(1, int(args.progress_every_calls))
    restorer._progress_min_interval_sec = max(0.0, float(args.progress_min_seconds))

    # Override traversal limits from CLI
    restorer._call_max_depth = max(0, int(args.call_depth))
    restorer._call_max_nodes = max(1, int(args.call_nodes))

    # debug settings
    restorer._debug_enabled = bool(args.debug)
    restorer._debug_only_problems = not bool(args.debug_all)
    restorer._debug_sql_preview_len = max(50, int(args.debug_sql_preview_len))
    restorer._debug_max_sql_snippets_per_method = max(1, int(args.debug_max_sql_per_method))
    restorer._debug_max_methods_with_sql_per_class = max(1, int(args.debug_max_methods_per_class))
    restorer._debug_max_call_samples = max(0, int(args.debug_max_call_samples))

    design = restorer.restore_design()
    
    design_doc = format_design_document(design)
    
    output_path.write_text(
        json.dumps(design_doc, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )
    
    if not args.quiet:
        print_design_summary(design_doc)
    
    print(f"[情報] 設計ドキュメントを保存しました: {output_path}")


if __name__ == "__main__":
    main()
