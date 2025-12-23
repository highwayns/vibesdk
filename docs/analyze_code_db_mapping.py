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
import sys
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


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _tokenize_identifiers(text: str) -> Set[str]:
    """Tokenize identifiers from code/SQL text.

    Much faster than running a regex search per column name.
    """
    if not text:
        return set()
    return {m.group(0).lower() for m in _IDENT_RE.finditer(text)}


def _extract_used_columns(
    text: str,
    columns: List[Dict[str, Any]],
    max_cols: int = 25,
    tokens: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Extract likely-used column names from SQL/code text.

    Heuristic (static analysis): over/under-approximation is possible.
    Optimized for large projects by using identifier token sets.
    """
    used: List[Dict[str, Any]] = []
    if not text or not columns:
        return used
    if tokens is None:
        tokens = _tokenize_identifiers(text)
    if not tokens:
        return used

    for col in columns:
        name = (col.get('name') or '').strip()
        if not name:
            continue
        if len(name) <= 2:
            continue
        if name.lower() in tokens:
            used.append({'name': name, 'logical_name': col.get('logical_name')})
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
    # NOTE:
    # - related_classes は既存互換のため残す（最終的に function_id の配列を入れる）
    # - raw のクラス名（コールグラフ由来）は related_class_names に保持する
    related_classes: List[str]                              # 互換用: 関連機能ID一覧
    related_class_names: List[str] = field(default_factory=list)        # raw: 関連クラス名一覧
    related_functions_callgraph: List[str] = field(default_factory=list) # callgraph: 関連機能ID一覧
    related_functions_prefix: List[str] = field(default_factory=list)    # prefix: 関連機能ID一覧
    related_functions: List[str] = field(default_factory=list)           # union(callgraph,prefix)
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
            r'(?i)\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)',
            r'(?i)\bJOIN\s+([A-Za-z_][A-Za-z0-9_]*)',
            r'(?i)\bINNER\s+JOIN\s+([A-Za-z_][A-Za-z0-9_]*)',
            r'(?i)\bLEFT\s+JOIN\s+([A-Za-z_][A-Za-z0-9_]*)',
            r'(?i)\bRIGHT\s+JOIN\s+([A-Za-z_][A-Za-z0-9_]*)',
        ],
        'INSERT': [
            r'(?i)\bINSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)',
        ],
        'UPDATE': [
            r'(?i)\bUPDATE\s+([A-Za-z_][A-Za-z0-9_]*)',
        ],
        'DELETE': [
            r'(?i)\bDELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)',
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
                    table_name = match.group(1)
                    if self._is_valid_table(table_name):
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
        if name.upper() in exclude:
            return False
        return name.upper() in self.table_names or name.lower() in self.table_names
    
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

        # Progress / debug controls (set from CLI)
        self._progress: bool = False
        self._progress_every: int = 20
        self._debug_callgraph: bool = False

        # Build a static call graph index so we can resolve "screen -> other" DB access.
        self._call_max_depth = int(java_structure.get('call_graph', {}).get('max_depth', 8)) if isinstance(java_structure, dict) else 8
        self._call_max_nodes = int(java_structure.get('call_graph', {}).get('max_nodes', 800)) if isinstance(java_structure, dict) else 800
        self._method_index, self._class_index = self._build_call_graph_indexes()

        # Fast lookup indexes for call resolution (critical for large codebases)
        self._methods_by_class_and_name: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        self._methods_by_class_full: Dict[str, List[str]] = defaultdict(list)
        self._methods_by_name: Dict[str, List[str]] = defaultdict(list)
        self._class_fulls_by_class_name: Dict[str, List[str]] = defaultdict(list)
        for mid, rec in self._method_index.items():
            cfull = rec.get('class_full')
            mname = rec.get('method_name')
            if cfull and mname:
                self._methods_by_class_and_name[(cfull, mname)].append(mid)
                self._methods_by_class_full[cfull].append(mid)
                self._methods_by_name[mname].append(mid)
        for cfull, crec in self._class_index.items():
            cname = crec.get('class_name')
            if cname:
                self._class_fulls_by_class_name[cname].append(cfull)

        # Cache resolved calls to avoid repeated candidate scans
        self._call_resolve_cache: Dict[Tuple[str, str, int, Optional[str]], List[str]] = {}

        self._method_refs_cache: Dict[str, List[TableReference]] = {}
        self._method_columns_cache: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    
    def _build_table_info(self) -> Dict[str, Dict[str, Any]]:
        """テーブル情報辞書を構築"""
        info = {}
        for table in self.db_metadata.get('tables', []):
            info[table['table_name'].upper()] = table
            info[table['table_name'].lower()] = table
        return info

    def _build_call_graph_indexes(self) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        """Build method/class indexes for call graph resolution.

        Returns:
          method_index: {
            method_id: {
              class_full, class_name, method_name, param_count, text, calls, class_type_refs
            }
          }
          class_index: {
            class_full: {
              class_name, function_type, genexus_type, type_references
            }
          }
        """
        method_index: Dict[str, Dict[str, Any]] = {}
        class_index: Dict[str, Dict[str, Any]] = {}

        for file_entry in self.java_structure.get('files', []):
            for cls in file_entry.get('classes', []):
                class_name = cls.get('name', '')
                class_full = cls.get('full_name') or (f"{cls.get('package')}.{class_name}" if cls.get('package') else class_name)
                deps = cls.get('dependencies') or {}
                type_refs = deps.get('type_references') or []

                class_index[class_full] = {
                    'class_name': class_name,
                    'class_full': class_full,
                    'function_type': cls.get('function_type', 'other'),
                    'genexus_type': cls.get('genexus_type'),
                    'type_references': list(type_refs),
                }

                for m in cls.get('methods', []) or []:
                    mname = m.get('name') or ''
                    if not mname:
                        continue
                    param_count = m.get('param_count', -1)
                    start_line = m.get('start_line', 0)

                    # Build analysis text: excerpt/signature + extracted SQL literals
                    text = (m.get('code') or '')
                    if not text:
                        text = (m.get('signature') or '')
                    sql_strings = m.get('sql_strings') or []
                    if sql_strings:
                        text = text + "\n" + "\n".join(sql_strings)

                    method_id = f"{class_full}::{mname}({param_count})@{start_line}"
                    method_index[method_id] = {
                        'method_id': method_id,
                        'class_name': class_name,
                        'class_full': class_full,
                        'method_name': mname,
                        'param_count': int(param_count) if param_count is not None else -1,
                        'start_line': int(start_line) if start_line is not None else 0,
                        'text': text,
                        'calls': m.get('calls') or [],
                        'type_references': list(type_refs),
                    }

        return method_index, class_index

    def _resolve_call_candidates(self, caller_class_full: str, call: Dict[str, Any]) -> List[str]:
        """Resolve a call dict to candidate method_id list (best-effort)."""
        name = (call.get('name') or '').strip()
        if not name or name in _IGNORE_METHOD_NAMES:
            return []
        try:
            arg_count = int(call.get('arg_count', -1))
        except Exception:
            arg_count = -1
        qualifier = _simplify_qualifier(call.get('qualifier'))

        cache_key = (caller_class_full, name, arg_count, qualifier)
        cached = self._call_resolve_cache.get(cache_key)
        if cached is not None:
            return cached

        def pick_in_class(class_full: str) -> List[str]:
            mids = self._methods_by_class_and_name.get((class_full, name)) or []
            if not mids:
                return []
            # If we don't know arg_count, return a small cap.
            if arg_count < 0:
                return list(mids)[:10]
            out: List[str] = []
            for mid in mids:
                pc = (self._method_index.get(mid) or {}).get('param_count', -1)
                if pc in (arg_count, -1):
                    out.append(mid)
            if out:
                return out[:10]
            # Fallback: GeneXus-generated code sometimes calls through wrappers (arg_count mismatch).
            # If there is only one overload, accept it; otherwise accept a small cap.
            if len(mids) == 1:
                return [mids[0]]
            return list(mids)[:3]

        cands: List[str] = []

        # 1) unqualified / this / super: same class first
        if not qualifier or qualifier in ('this', 'super'):
            cands.extend(pick_in_class(caller_class_full))

        # 2) Qualified by a class-like name (including GeneXus lower-case class names)
        if qualifier:
            if qualifier in self._class_fulls_by_class_name or _looks_like_class_name(qualifier):
                for cls_full in self._class_fulls_by_class_name.get(qualifier, []) or []:
                    cands.extend(pick_in_class(cls_full))

        # 3) If still empty, try restricting to caller's referenced types
        if not cands:
            type_refs = set((self._class_index.get(caller_class_full) or {}).get('type_references') or [])
            if type_refs:
                for cname in type_refs:
                    for cls_full in self._class_fulls_by_class_name.get(cname, []) or []:
                        cands.extend(pick_in_class(cls_full))

        # 4) Global fallback (cap)
        if not cands:
            for mid in (self._methods_by_name.get(name) or [])[:200]:
                pc = (self._method_index.get(mid) or {}).get('param_count', -1)
                if arg_count >= 0 and pc not in (arg_count, -1):
                    continue
                cands.append(mid)
                if len(cands) >= 20:
                    break

        # If still empty and arg_count is known, relax arg_count filtering a bit to allow traversal.
        # This is safe because downstream traversal is capped by call_max_nodes/depth.
        if not cands and arg_count >= 0:
            cands.extend((self._methods_by_name.get(name) or [])[:10])

# Deduplicate + cap
        seen = set()
        uniq: List[str] = []
        for c in cands:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        if len(uniq) > 20:
            uniq = uniq[:20]
        self._call_resolve_cache[cache_key] = uniq
        return uniq

    def _collect_references_for_class(self, cls_data: Dict[str, Any]) -> Tuple[List[TableReference], Dict[str, List[Dict[str, Any]]], Set[str]]:
        """Collect table references by traversing the call graph starting from a class' methods.

        Returns: (refs, columns_used_map, related_classes)
        columns_used_map: {TABLE: [ {name, logical_name}, ... ]}
        """
        class_name = cls_data.get('name', '')
        class_full = cls_data.get('full_name') or (f"{cls_data.get('package')}.{class_name}" if cls_data.get('package') else class_name)

        # Entry method candidates: all declared methods in this class (O(1) lookup)
        entry_method_ids: List[str] = list(self._methods_by_class_full.get(class_full, []) or [])

        # No method index (older json) -> fallback to in-class extraction only
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
            return all_references, columns_used_map, set()

        # Call graph traversal
        visited: Set[str] = set()
        enqueued: Set[str] = set(entry_method_ids)
        queue: deque[Tuple[str, int]] = deque((mid, 0) for mid in entry_method_ids)
        all_refs: List[TableReference] = []
        columns_used_map: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        related_classes: Set[str] = set()

        while queue and len(visited) < self._call_max_nodes:
            mid, depth = queue.popleft()
            if mid in visited:
                continue
            visited.add(mid)

            if self._debug_callgraph and (len(visited) % 200 == 0):
                print(
                    f"[debug] {class_name}: visited={len(visited)}/{self._call_max_nodes} queue={len(queue)} depth={depth}",
                    file=sys.stderr,
                )

            rec = self._method_index.get(mid) or {}
            text = rec.get('text') or ''
            src_class = rec.get('class_name') or class_name
            src_method = rec.get('method_name')

            # Extract refs (cached)
            if mid in self._method_refs_cache:
                refs = self._method_refs_cache[mid]
                cols_map = self._method_columns_cache.get(mid) or {}
            else:
                refs = self.extractor.extract_from_code(text, src_class, src_method)
                cols_map: Dict[str, List[Dict[str, Any]]] = {}
                if refs:
                    tokens = _tokenize_identifiers(text)
                    # columns used (heuristic) per table, computed once per table
                    for t in {r.table_name.upper() for r in refs}:
                        tinfo = self.table_info.get(t, {})
                        cols = tinfo.get('columns', [])
                        cols_used = _extract_used_columns(text, cols, tokens=tokens)
                        if cols_used:
                            cols_map[t] = cols_used
                self._method_refs_cache[mid] = refs
                self._method_columns_cache[mid] = cols_map

            all_refs.extend(refs)
            for t, cols in (cols_map or {}).items():
                # merge unique column dicts by name
                existing = {c.get('name') for c in columns_used_map.get(t, [])}
                for c in cols:
                    if c.get('name') not in existing:
                        columns_used_map[t].append(c)
                        existing.add(c.get('name'))

            # Follow calls
            if depth >= self._call_max_depth:
                continue
            for call in rec.get('calls') or []:
                cands = self._resolve_call_candidates(rec.get('class_full') or class_full, call)
                for cmid in cands:
                    if cmid in visited or cmid in enqueued:
                        continue
                    enqueued.add(cmid)
                    queue.append((cmid, depth + 1))
                    callee_cls_full = (self._method_index.get(cmid) or {}).get('class_full')
                    if callee_cls_full and callee_cls_full != class_full:
                        related_classes.add((self._class_index.get(callee_cls_full) or {}).get('class_name', callee_cls_full))

        return all_refs, columns_used_map, related_classes
    
    def restore_design(self) -> SystemDesign:
        """システム設計を還元"""
        functions = []
        table_function_map = defaultdict(list)

        # Collect targets first so we can report progress accurately
        targets: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for file_entry in self.java_structure.get('files', []):
            for cls_data in file_entry.get('classes', []):
                func_type = cls_data.get('function_type', 'other')
                if func_type in ('screen', 'batch'):
                    targets.append((file_entry, cls_data))

        total = len(targets)
        if self._progress:
            print(f"[progress] Step3: analyzing {total} screen/batch classes...", file=sys.stderr)

        for idx, (file_entry, cls_data) in enumerate(targets, start=1):
            class_name = cls_data.get('name', 'Unknown')
            func_type = cls_data.get('function_type', 'other')
            t0 = time.perf_counter()

            if self._progress and (idx == 1 or idx % max(1, int(self._progress_every)) == 0):
                print(f"[progress] ({idx}/{total}) start: {class_name} [{func_type}]", file=sys.stderr)

            func_design = self._restore_function(cls_data, file_entry)
            dt = time.perf_counter() - t0

            if func_design:
                functions.append(func_design)
                for table in func_design.tables_used:
                    table_function_map[table['table_name']].append(func_design.function_id)

                if self._progress and (idx == 1 or idx % max(1, int(self._progress_every)) == 0):
                    print(
                        f"[progress] ({idx}/{total}) done : {class_name} tables={len(func_design.tables_used)} visited_cache={len(self._method_refs_cache)} time={dt:.1f}s",
                        file=sys.stderr,
                    )
            else:
                if self._progress and (idx == 1 or idx % max(1, int(self._progress_every)) == 0):
                    print(
                        f"[progress] ({idx}/{total}) skip : {class_name} (no DB refs) time={dt:.1f}s",
                        file=sys.stderr,
                    )

        # 1) コールグラフで見つかったクラス名を function_id に解決（画面→呼び出し先バッチ等）
        self._resolve_related_functions_callgraph(functions)
        
        # 機能グループ化
        grouped_functions = self._group_functions(functions)
        
        return SystemDesign(
            project_name=self.java_structure.get('project_root', 'Unknown'),
            functions=grouped_functions,
            table_function_matrix=dict(table_function_map),
            er_diagram_data=self._build_er_data(),
        )

    def _resolve_related_functions_callgraph(self, functions: List[FunctionDesign]) -> None:
        """related_class_names（クラス名）を related_functions_callgraph（function_id）へ解決する"""
        class_to_func: Dict[str, str] = {}
        for f in functions:
            for cls_name in (f.entry_classes or []):
                class_to_func[str(cls_name)] = f.function_id

        for f in functions:
            resolved = set()
            for cls_name in (getattr(f, 'related_class_names', []) or []):
                fid = class_to_func.get(str(cls_name))
                if fid and fid != f.function_id:
                    resolved.add(fid)
            f.related_functions_callgraph = sorted(resolved)
    
    def _restore_function(self, cls_data: Dict[str, Any], 
                          file_entry: Dict[str, Any]) -> Optional[FunctionDesign]:
        """単一機能の設計を還元"""
        class_name = cls_data.get('name', '')
        function_type = cls_data.get('function_type', 'other')
        genexus_type = cls_data.get('genexus_type')

        # テーブル参照を抽出（コールグラフ追跡により other クラス内 SQL も拾う）
        all_references, columns_used_map, related_classes = self._collect_references_for_class(cls_data)
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
            related_classes=[],  # 互換用: 後段で function_id へ解決してセット
            related_class_names=sorted(list(related_classes)) if related_classes else [],
            related_functions_callgraph=[],
            related_functions_prefix=[],
            related_functions=[],
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
                    prefix_related = [
                        f.function_id for f in group_funcs
                        if f.function_id != func.function_id
                    ]
                    func.related_functions_prefix = prefix_related

        # 互換用 related_classes は function_id の配列として出力しつつ
        # コールグラフ由来/プレフィックス由来は別フィールドにも保持する
        for func in functions:
            call_related = getattr(func, 'related_functions_callgraph', []) or []
            prefix_related = getattr(func, 'related_functions_prefix', []) or []
            merged = sorted(set(call_related + prefix_related))
            func.related_functions = merged
            func.related_classes = merged
        
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
            'related_class_names': func.related_class_names,
            'related_functions_callgraph': func.related_functions_callgraph,
            'related_functions_prefix': func.related_functions_prefix,
            'related_functions': func.related_functions,
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
    parser.add_argument("--progress", action="store_true",
                        help="実行進捗を stderr に出力する")
    parser.add_argument("--progress-every", type=int, default=20,
                        help="進捗表示の間隔（N件ごと） (default: 20)")
    parser.add_argument("--debug-callgraph", action="store_true",
                        help="コールグラフ追跡の詳細ログを出力（重いので注意）")

    args = parser.parse_args()
    
    java_path = Path(args.java_structure)
    db_path = Path(args.db_metadata)
    output_path = Path(args.output)
    
    if not java_path.exists():
        raise SystemExit(f"Java構造ファイルが存在しません: {java_path}")
    if not db_path.exists():
        raise SystemExit(f"DBメタデータファイルが存在しません: {db_path}")
    
    t_start = time.perf_counter()
    print(f"[情報] Java構造を読み込み中: {java_path}")
    java_structure = json.loads(java_path.read_text(encoding='utf-8'))
    
    print(f"[情報] DBメタデータを読み込み中: {db_path}")
    db_metadata = json.loads(db_path.read_text(encoding='utf-8'))
    
    print(f"[情報] 機能設計を還元中...")
    restorer = FunctionDesignRestorer(java_structure, db_metadata)
    restorer._progress = bool(args.progress)
    restorer._progress_every = max(1, int(args.progress_every))
    restorer._debug_callgraph = bool(args.debug_callgraph)
    # Override traversal limits from CLI
    restorer._call_max_depth = max(0, int(args.call_depth))
    restorer._call_max_nodes = max(1, int(args.call_nodes))
    design = restorer.restore_design()
    
    design_doc = format_design_document(design)
    
    output_path.write_text(
        json.dumps(design_doc, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )
    
    if not args.quiet:
        print_design_summary(design_doc)
    
    total_dt = time.perf_counter() - t_start
    print(f"[情報] 設計ドキュメントを保存しました: {output_path} (total {total_dt:.1f}s)")


if __name__ == "__main__":
    main()
