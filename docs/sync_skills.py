#!/usr/bin/env python3
"""同步 Agent-Skills-for-Context-Engineering 仓库的 skills 到本地.

此脚本从 GitHub 仓库 muratcankoylan/Agent-Skills-for-Context-Engineering 
下载最新的 context engineering skills，包括关联的 scripts/、references/ 目录。

Usage:
    python scripts/sync_skills.py --target-dir .claude/skills --force --full
    
Options:
    --target-dir    目标目录，默认为 skills/
    --force         强制覆盖已存在的文件
    --dry-run       仅显示将要执行的操作，不实际下载
    --include       仅同步指定的 skills (逗号分隔)
    --exclude       排除指定的 skills (逗号分隔)
    --full          同步完整目录结构 (包括 scripts/, references/, assets/)
    --flat          仅同步 SKILL.md 为扁平结构 (默认)
    --analyze       分析项目中 skills 的使用情况

上游 Skill 目录结构:
    skill-name/
    ├── SKILL.md         # 必需：主指令文件 (frontmatter + instructions)
    ├── scripts/         # 可选：可执行脚本 (Python/Bash)
    ├── references/      # 可选：参考文档
    └── assets/          # 可选：模板和资源文件

修复版本: 添加了动态发现子目录文件的功能
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# =============================================================================
# Configuration
# =============================================================================

GITHUB_REPO = "muratcankoylan/Agent-Skills-for-Context-Engineering"
GITHUB_BRANCH = "main"
GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}"
GITHUB_API_BASE = f"https://api.github.com/repos/{GITHUB_REPO}"

# Known skills from the repository (based on documentation and search results)
# 每个 skill 是一个目录，包含 SKILL.md 和可能的其他文件
KNOWN_SKILLS = [
    # Context Engineering Fundamentals (基础)
    "context-fundamentals",
    "context-degradation", 
    "context-optimization",
    
    # Agent Architecture (架构)
    "multi-agent-patterns",
    "memory-systems",
    "tool-design",
    
    # Evaluation & Development (评估与开发)
    "evaluation",
    "agent-evaluation",
    "agent-development",
    
    # Advanced (高级)
    "agent-architecture",
    "cognitive-architecture",
    
    # Specialized (专门化)
    "background-agents",
    "llm-project-development",
]

# 每个 skill 目录下可能包含的子目录
SKILL_SUBDIRS = ["scripts", "references", "assets"]

# 每个 skill 目录下已知的文件 (基于文档和搜索结果)
# 格式: skill_name -> [(subdir, filename), ...]
# 注意: 这个列表现在仅作为后备方案，主要依赖动态发现
KNOWN_SKILL_FILES: Dict[str, List[Tuple[str, str]]] = {
    "context-fundamentals": [
        ("scripts", "progressive_disclosure.py"),
    ],
    "context-optimization": [
        ("scripts", "compaction.py"),
        ("scripts", "observation_masking.py"),
    ],
    "memory-systems": [
        ("scripts", "vector_store.py"),
        ("scripts", "knowledge_graph.py"),
    ],
    "tool-design": [
        ("scripts", "tool_wrapper.py"),
        ("references", "tool_patterns.md"),
    ],
    "evaluation": [
        ("scripts", "evaluator.py"),
        ("references", "metrics.md"),
    ],
    "multi-agent-patterns": [
        ("scripts", "orchestrator.py"),
        ("scripts", "swarm.py"),
    ],
}

# Mapping: 本地旧文件名 -> 对应的新 skill 目录
LOCAL_TO_UPSTREAM_MAPPING = {
    "context-fundamentals.md": "context-fundamentals",
    "context-degradation.md": "context-degradation",
    "context-compression.md": "context-optimization",
    "memory-systems.md": "memory-systems",
    "filesystem-context.md": None,
}


@dataclass
class SkillInfo:
    """Skill 元信息"""
    name: str
    summary: str = ""
    version: str = "1.0.0"
    file: str = "SKILL.md"
    local_file: str = ""
    upstream_dir: str = ""
    triggers: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    hash: str = ""


@dataclass
class SyncResult:
    """同步结果"""
    skill: str
    action: str  # downloaded, updated, skipped, failed
    message: str = ""
    old_hash: str = ""
    new_hash: str = ""


# =============================================================================
# Utility Functions
# =============================================================================

def fetch_url(url: str, timeout: int = 30) -> Optional[str]:
    """从 URL 获取内容"""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "better-agents-skill-sync/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception as e:
        print(f"Warning: Failed to fetch {url}: {e}", file=sys.stderr)
        return None


def fetch_json(url: str, timeout: int = 30) -> Optional[Any]:
    """从 URL 获取 JSON 内容"""
    content = fetch_url(url, timeout)
    if content:
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            print(f"Warning: Failed to parse JSON from {url}: {e}", file=sys.stderr)
    return None


def compute_hash(content: str) -> str:
    """计算内容的短 hash"""
    return hashlib.sha256(content.encode()).hexdigest()[:12]


def extract_skill_metadata(content: str) -> Dict[str, Any]:
    """从 SKILL.md 内容中提取元数据"""
    metadata: Dict[str, Any] = {
        "summary": "",
        "version": "1.0.0",
        "triggers": [],
        "dependencies": [],
    }
    
    lines = content.split("\n")
    in_description = False
    description_lines = []
    
    for line in lines:
        if "Version:" in line:
            match = re.search(r"Version:\s*(\d+\.\d+\.\d+)", line)
            if match:
                metadata["version"] = match.group(1)
        
        if "should be used when" in line.lower():
            triggers = re.findall(r'"([^"]+)"', line)
            metadata["triggers"].extend(triggers)
        
        if "connects to:" in line.lower() or "builds on:" in line.lower():
            deps = re.findall(r'(\w+-\w+)', line)
            metadata["dependencies"].extend(deps)
        
        if line.startswith("# "):
            in_description = True
            continue
        if in_description and line.strip() and not line.startswith("#"):
            description_lines.append(line.strip())
            if len(description_lines) >= 3:
                break
    
    if description_lines:
        metadata["summary"] = " ".join(description_lines[:2])[:200]
    
    return metadata


def list_remote_skills() -> List[str]:
    """尝试从 GitHub API 获取 skills 列表，失败则使用已知列表"""
    try:
        url = f"{GITHUB_API_BASE}/contents/skills"
        data = fetch_json(url)
        if data:
            return [item["name"] for item in data if item["type"] == "dir"]
    except Exception as e:
        print(f"Warning: Could not fetch skill list from API: {e}", file=sys.stderr)
    
    return KNOWN_SKILLS


# =============================================================================
# Sync Logic
# =============================================================================

class SkillSyncer:
    """Skills 同步器
    
    支持两种模式:
    1. flat_structure=True (默认): 仅下载 SKILL.md 为扁平文件
    2. flat_structure=False (--full): 下载完整目录结构包括 scripts/, references/, assets/
    
    修复版本: 添加了动态发现子目录文件的功能
    """
    
    def __init__(
        self,
        target_dir: str = "skills",
        force: bool = False,
        dry_run: bool = False,
        include: Optional[Set[str]] = None,
        exclude: Optional[Set[str]] = None,
        flat_structure: bool = True,
    ):
        self.target_dir = Path(target_dir)
        self.force = force
        self.dry_run = dry_run
        self.include = include
        self.exclude = exclude or set()
        self.flat_structure = flat_structure
        self.results: List[SyncResult] = []
        self.file_results: List[SyncResult] = []
        # 缓存已发现的文件列表，避免重复 API 调用
        self._discovered_files_cache: Dict[str, List[Tuple[str, str]]] = {}
    
    def should_sync(self, skill_name: str) -> bool:
        """判断是否应该同步该 skill"""
        if self.include and skill_name not in self.include:
            return False
        if skill_name in self.exclude:
            return False
        return True
    
    def get_local_path(self, skill_name: str, subdir: str = "", filename: str = "SKILL.md") -> Path:
        """获取本地路径"""
        if self.flat_structure:
            return self.target_dir / f"{skill_name}.md"
        
        if subdir:
            return self.target_dir / skill_name / subdir / filename
        return self.target_dir / skill_name / filename
    
    def get_remote_url(self, skill_name: str, subdir: str = "", filename: str = "SKILL.md") -> str:
        """获取远程 URL"""
        if subdir:
            return f"{GITHUB_RAW_BASE}/skills/{skill_name}/{subdir}/{filename}"
        return f"{GITHUB_RAW_BASE}/skills/{skill_name}/{filename}"
    
    def _discover_skill_files(self, skill_name: str) -> List[Tuple[str, str]]:
        """动态发现 skill 子目录中的文件 (修复核心)
        
        通过 GitHub API 获取每个子目录的实际文件列表，
        而不是依赖硬编码的 KNOWN_SKILL_FILES。
        
        Returns:
            List of (subdir, filename) tuples
        """
        # 检查缓存
        if skill_name in self._discovered_files_cache:
            return self._discovered_files_cache[skill_name]
        
        discovered_files: List[Tuple[str, str]] = []
        
        for subdir in SKILL_SUBDIRS:
            # 使用 GitHub API 获取子目录内容
            url = f"{GITHUB_API_BASE}/contents/skills/{skill_name}/{subdir}"
            data = fetch_json(url)
            
            if data and isinstance(data, list):
                for item in data:
                    if item.get("type") == "file":
                        filename = item.get("name", "")
                        if filename:
                            discovered_files.append((subdir, filename))
                            
        # 缓存结果
        self._discovered_files_cache[skill_name] = discovered_files
        
        return discovered_files
    
    def _get_files_to_sync(self, skill_name: str) -> List[Tuple[str, str]]:
        """获取需要同步的文件列表
        
        优先使用动态发现，如果 API 调用失败则回退到已知文件列表。
        """
        # 1. 尝试动态发现
        discovered = self._discover_skill_files(skill_name)
        
        # 2. 获取已知文件作为后备
        known = KNOWN_SKILL_FILES.get(skill_name, [])
        
        # 3. 合并并去重
        all_files = list(discovered)
        for item in known:
            if item not in all_files:
                all_files.append(item)
        
        return all_files
    
    def sync_file(self, skill_name: str, subdir: str = "", filename: str = "SKILL.md") -> SyncResult:
        """同步单个文件"""
        local_path = self.get_local_path(skill_name, subdir, filename)
        remote_url = self.get_remote_url(skill_name, subdir, filename)
        
        # 获取远程内容
        remote_content = fetch_url(remote_url)
        if not remote_content:
            return SyncResult(
                f"{skill_name}/{subdir}/{filename}" if subdir else f"{skill_name}/{filename}",
                "failed", 
                f"Not found or inaccessible"
            )
        
        new_hash = compute_hash(remote_content)
        
        # 检查本地文件
        old_hash = ""
        if local_path.exists():
            try:
                local_content = local_path.read_text(encoding="utf-8")
                old_hash = compute_hash(local_content)
                
                if old_hash == new_hash and not self.force:
                    return SyncResult(
                        f"{skill_name}/{subdir}/{filename}" if subdir else f"{skill_name}/{filename}",
                        "skipped", 
                        "Up to date", 
                        old_hash, 
                        new_hash
                    )
            except Exception:
                pass
        
        # 执行写入
        if not self.dry_run:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(remote_content, encoding="utf-8")
            action = "updated" if old_hash else "downloaded"
        else:
            action = "would_update" if old_hash else "would_download"
        
        file_id = f"{skill_name}/{subdir}/{filename}" if subdir else f"{skill_name}/{filename}"
        return SyncResult(file_id, action, "", old_hash, new_hash)
    
    def sync_skill(self, skill_name: str) -> SyncResult:
        """同步单个 skill (包括关联目录)"""
        if not self.should_sync(skill_name):
            return SyncResult(skill_name, "skipped", "Excluded by filter")
        
        # 1. 同步 SKILL.md (必需)
        main_result = self.sync_file(skill_name)
        
        if main_result.action == "failed":
            return SyncResult(skill_name, "failed", main_result.message)
        
        # 2. 如果是完整模式，同步关联目录
        if not self.flat_structure:
            self._sync_skill_subdirs(skill_name)
        
        return main_result
    
    def _sync_skill_subdirs(self, skill_name: str) -> None:
        """同步 skill 的子目录 (scripts/, references/, assets/)
        
        修复版本: 使用动态发现而不是硬编码列表
        """
        # 获取需要同步的文件列表 (动态发现 + 已知文件)
        files_to_sync = self._get_files_to_sync(skill_name)
        
        if not files_to_sync:
            # 如果没有发现任何文件，尝试常见文件名作为最后手段
            common_files = [
                ("scripts", "example.py"),
                ("scripts", "demo.py"),
                ("scripts", "utils.py"),
                ("scripts", "main.py"),
                ("scripts", "__init__.py"),
                ("references", "README.md"),
                ("references", "examples.md"),
                ("references", "guide.md"),
                ("assets", "template.md"),
                ("assets", "config.yaml"),
                ("assets", "config.json"),
            ]
            files_to_sync = common_files
        
        synced_count = 0
        for subdir, filename in files_to_sync:
            result = self.sync_file(skill_name, subdir, filename)
            
            # 只记录非失败的结果（动态发现的文件应该都存在）
            if result.action != "failed":
                self.file_results.append(result)
                synced_count += 1
                
                if result.action not in ("skipped",):
                    icon = "  ✓" if result.action == "downloaded" else "  ↑"
                    print(f"    {icon} {subdir}/{filename}")
            elif result.action == "failed" and (subdir, filename) in KNOWN_SKILL_FILES.get(skill_name, []):
                # 只对已知文件报告失败
                self.file_results.append(result)
                print(f"    ✗ {subdir}/{filename}: {result.message}")
    
    def sync_all(self) -> List[SyncResult]:
        """同步所有 skills"""
        print(f"Fetching skill list from {GITHUB_REPO}...")
        skills = list_remote_skills()
        
        mode = "完整目录 (动态发现)" if not self.flat_structure else "扁平文件"
        print(f"Found {len(skills)} skills to sync (模式: {mode})")
        print("")
        
        for skill in skills:
            result = self.sync_skill(skill)
            self.results.append(result)
            
            icon = {
                "downloaded": "✓",
                "updated": "↑",
                "skipped": "·",
                "failed": "✗",
                "would_download": "?",
                "would_update": "?",
            }.get(result.action, "?")
            
            print(f"  {icon} {result.skill}: {result.action} {result.message}")
        
        return self.results
    
    def generate_index(self) -> Dict[str, Dict[str, Any]]:
        """生成 skill_index.json"""
        index: Dict[str, Dict[str, Any]] = {}
        
        if self.flat_structure:
            skill_files = list(self.target_dir.glob("*.md"))
        else:
            skill_files = list(self.target_dir.glob("*/SKILL.md"))
        
        for path in skill_files:
            if path.name == "README.md":
                continue
            
            skill_name = path.stem if self.flat_structure else path.parent.name
            content = path.read_text(encoding="utf-8")
            metadata = extract_skill_metadata(content)
            
            entry: Dict[str, Any] = {
                "summary": metadata["summary"],
                "file": path.name if self.flat_structure else f"{skill_name}/SKILL.md",
                "version": metadata["version"],
                "triggers": metadata["triggers"][:5],
            }
            
            if not self.flat_structure:
                skill_dir = self.target_dir / skill_name
                associated_files = []
                
                for subdir in SKILL_SUBDIRS:
                    subdir_path = skill_dir / subdir
                    if subdir_path.exists():
                        for f in subdir_path.iterdir():
                            if f.is_file():
                                associated_files.append(f"{subdir}/{f.name}")
                
                if associated_files:
                    entry["associated_files"] = associated_files
            
            index[skill_name] = entry
        
        return index
    
    def save_index(self) -> Path:
        """保存 skill_index.json"""
        index = self.generate_index()
        index_path = self.target_dir / "skill_index.json"
        
        if not self.dry_run:
            index_path.write_text(
                json.dumps(index, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
        
        return index_path
    
    def print_summary(self) -> None:
        """打印同步摘要"""
        downloaded = sum(1 for r in self.results if r.action in ("downloaded", "updated"))
        skipped = sum(1 for r in self.results if r.action == "skipped")
        failed = sum(1 for r in self.results if r.action == "failed")
        
        print(f"\n主文件摘要: {downloaded} 同步, {skipped} 跳过, {failed} 失败")
        
        if self.file_results:
            sub_downloaded = sum(1 for r in self.file_results if r.action in ("downloaded", "updated"))
            sub_skipped = sum(1 for r in self.file_results if r.action == "skipped")
            sub_failed = sum(1 for r in self.file_results if r.action == "failed")
            print(f"关联文件: {sub_downloaded} 同步, {sub_skipped} 跳过, {sub_failed} 失败")
            
            # 显示发现统计
            total_discovered = sum(len(files) for files in self._discovered_files_cache.values())
            if total_discovered > 0:
                print(f"动态发现: 共发现 {total_discovered} 个子目录文件")


# =============================================================================
# Necessity Analysis
# =============================================================================

def analyze_file_necessity() -> Dict[str, Any]:
    """分析关联文件的复制必要性"""
    analysis = {
        "必需复制": [],
        "建议复制": [],
        "可选复制": [],
        "无需复制": [],
    }
    
    file_necessity = {
        "scripts/progressive_disclosure.py": {
            "necessity": "建议复制",
            "reason": "演示渐进式加载模式，可作为 SkillsPack 实现的参考",
            "used_by": ["skills_loader.py"],
        },
        "scripts/compaction.py": {
            "necessity": "建议复制",
            "reason": "压缩算法示例，可作为 compressor.py 的参考",
            "used_by": ["compressor.py"],
        },
        "scripts/observation_masking.py": {
            "necessity": "可选复制",
            "reason": "观察掩码技术，当前代码未使用此模式",
            "used_by": [],
        },
        "scripts/vector_store.py": {
            "necessity": "可选复制",
            "reason": "向量存储示例，当前使用 RAGFlow 而非本地向量库",
            "used_by": [],
        },
        "scripts/knowledge_graph.py": {
            "necessity": "可选复制",
            "reason": "知识图谱示例，当前版本未实现",
            "used_by": [],
        },
        "scripts/tool_wrapper.py": {
            "necessity": "建议复制",
            "reason": "Tool 定义最佳实践，对 HTTP API 设计有参考价值",
            "used_by": ["http_service.py"],
        },
        "scripts/evaluator.py": {
            "necessity": "建议复制",
            "reason": "评估脚本，可用于测试 playbook 质量",
            "used_by": ["tests/", "critic.py"],
        },
        "scripts/orchestrator.py": {
            "necessity": "可选复制",
            "reason": "多代理编排，当前为单代理架构",
            "used_by": [],
        },
        "references/tool_patterns.md": {
            "necessity": "建议复制",
            "reason": "Tool 设计模式文档，补充 SKILL.md 的细节",
            "used_by": ["http_service.py 设计参考"],
        },
        "references/metrics.md": {
            "necessity": "建议复制",
            "reason": "评估指标定义，对 critic.py 有参考价值",
            "used_by": ["critic.py"],
        },
    }
    
    for file_path, info in file_necessity.items():
        analysis[info["necessity"]].append({
            "file": file_path,
            "reason": info["reason"],
            "used_by": info["used_by"],
        })
    
    return analysis


def print_necessity_report() -> None:
    """打印关联文件复制必要性报告"""
    analysis = analyze_file_necessity()
    
    print("\n" + "=" * 60)
    print("关联文件复制必要性分析")
    print("=" * 60)
    
    categories = [
        ("必需复制", "🔴"),
        ("建议复制", "🟡"),
        ("可选复制", "🟢"),
        ("无需复制", "⚪"),
    ]
    
    for category, icon in categories:
        files = analysis[category]
        if files:
            print(f"\n{icon} {category} ({len(files)} 个文件)")
            for f in files:
                print(f"   · {f['file']}")
                print(f"     理由: {f['reason']}")
                if f['used_by']:
                    print(f"     关联: {', '.join(f['used_by'])}")
    
    print("\n" + "-" * 60)
    print("建议:")
    print("  1. 使用 --flat (默认) 仅同步 SKILL.md 作为知识文档")
    print("  2. 使用 --full 同步完整目录以获取示例脚本")
    print("  3. scripts/ 中的代码主要用于演示概念，非生产依赖")
    print("")


# =============================================================================
# Analysis Functions
# =============================================================================

def analyze_skill_usage(project_dir: str = ".") -> Dict[str, Any]:
    """分析项目中 skills 的使用情况"""
    project = Path(project_dir)
    
    analysis = {
        "skills_loader_imported": False,
        "skills_loaded": [],
        "skills_referenced": [],
        "integration_points": [],
        "recommendations": [],
    }
    
    for py_file in project.rglob("*.py"):
        if "__pycache__" in str(py_file):
            continue
        
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            continue
        
        rel_path = str(py_file.relative_to(project))
        
        if "skills_loader" in content or "SkillsPack" in content:
            analysis["skills_loader_imported"] = True
            analysis["integration_points"].append({
                "file": rel_path,
                "type": "import",
                "note": "Skills loader imported but usage unclear"
            })
        
        for skill_name in KNOWN_SKILLS:
            if skill_name in content:
                analysis["skills_referenced"].append({
                    "file": rel_path,
                    "skill": skill_name
                })
        
        patterns = {
            "compression": r"compress|compaction|layered.?summar",
            "degradation": r"lost.?in.?middle|context.?poison|drift",
            "memory": r"short.?term.?memory|long.?term.?memory|knowledge.?graph",
            "progressive_disclosure": r"progressive.?disclosure|on.?demand",
        }
        
        for concept, pattern in patterns.items():
            if re.search(pattern, content, re.I):
                analysis["integration_points"].append({
                    "file": rel_path,
                    "type": "concept",
                    "concept": concept
                })
    
    if not analysis["skills_loader_imported"]:
        analysis["recommendations"].append(
            "SkillsPack 未被导入使用 - 考虑在 pipeline.py 或 agentic_pipeline.py 中集成"
        )
    
    if not analysis["skills_referenced"]:
        analysis["recommendations"].append(
            "代码中未直接引用任何 skill 名称 - skills 可能仅作为文档存在"
        )
    
    compressor_path = project / "app" / "code2doc" / "compressor.py"
    if compressor_path.exists():
        analysis["integration_points"].append({
            "file": "app/code2doc/compressor.py",
            "type": "implementation",
            "concept": "context-compression",
            "note": "Implements compression principles from context-optimization skill"
        })
    
    return analysis


def print_usage_report(analysis: Dict[str, Any]) -> None:
    """打印使用情况报告"""
    print("\n" + "=" * 60)
    print("Skills 使用情况分析报告")
    print("=" * 60)
    
    print(f"\n[Skills Loader 集成状态]")
    if analysis["skills_loader_imported"]:
        print("  ✓ SkillsPack 已被导入")
    else:
        print("  ✗ SkillsPack 未被导入")
    
    print(f"\n[Skills 引用情况]")
    if analysis["skills_referenced"]:
        for ref in analysis["skills_referenced"]:
            print(f"  · {ref['skill']} in {ref['file']}")
    else:
        print("  (无直接引用)")
    
    print(f"\n[概念实现点]")
    seen = set()
    for point in analysis["integration_points"]:
        key = f"{point['file']}:{point.get('concept', point.get('type'))}"
        if key not in seen:
            seen.add(key)
            note = point.get("note", "")
            print(f"  · {point['file']}: {point.get('concept', point['type'])}")
            if note:
                print(f"    → {note}")
    
    print(f"\n[建议]")
    for rec in analysis["recommendations"]:
        print(f"  ⚠ {rec}")
    
    print("")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Sync skills from Agent-Skills-for-Context-Engineering (修复版)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/sync_skills.py                    # 扁平模式同步 SKILL.md
  python scripts/sync_skills.py --full             # 完整目录同步 (含 scripts/) - 动态发现
  python scripts/sync_skills.py --analyze          # 分析项目中的使用情况
  python scripts/sync_skills.py --necessity        # 分析关联文件复制必要性
  python scripts/sync_skills.py --include context-optimization,tool-design

修复内容:
  - 添加动态发现子目录文件功能 (通过 GitHub API)
  - 不再仅依赖硬编码的 KNOWN_SKILL_FILES 列表
  - 所有 skill 的子目录文件都能被正确下载
"""
    )
    parser.add_argument(
        "--target-dir", "-t",
        default="skills",
        help="Target directory for skills (default: skills/)"
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Force overwrite existing files"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be done without making changes"
    )
    parser.add_argument(
        "--include",
        help="Only sync specified skills (comma-separated)"
    )
    parser.add_argument(
        "--exclude",
        help="Exclude specified skills (comma-separated)"
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Analyze skill usage in the project"
    )
    parser.add_argument(
        "--necessity",
        action="store_true",
        help="Analyze necessity of copying associated files"
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        default=True,
        help="Use flat file structure - only SKILL.md (default)"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Sync full directory structure including scripts/, references/, assets/"
    )
    
    args = parser.parse_args()
    
    if args.necessity:
        print_necessity_report()
        return 0
    
    if args.analyze:
        analysis = analyze_skill_usage(".")
        print_usage_report(analysis)
        return 0
    
    include = set(args.include.split(",")) if args.include else None
    exclude = set(args.exclude.split(",")) if args.exclude else None
    
    flat_structure = not args.full
    
    syncer = SkillSyncer(
        target_dir=args.target_dir,
        force=args.force,
        dry_run=args.dry_run,
        include=include,
        exclude=exclude,
        flat_structure=flat_structure,
    )
    
    mode = "扁平文件 (仅 SKILL.md)" if flat_structure else "完整目录 (动态发现子文件)"
    print(f"同步 Skills 到 {args.target_dir}/")
    print(f"模式: {mode}")
    if args.dry_run:
        print("(DRY RUN - 不实际修改)")
    print("")
    
    results = syncer.sync_all()
    
    syncer.print_summary()
    
    downloaded = sum(1 for r in results if r.action in ("downloaded", "updated"))
    if not args.dry_run and downloaded > 0:
        print("\n生成 skill_index.json...")
        index_path = syncer.save_index()
        print(f"  → {index_path}")
    
    print("\n[集成建议]")
    print("  要在 Code2Doc 中使用同步的 skills，请在 pipeline.py 中添加:")
    print("")
    print("    from app.code2doc.skills_loader import SkillsPack")
    print("    skills = SkillsPack('skills')")
    print("    compression_guide = skills.load('context-optimization')")
    print("")
    
    if flat_structure:
        print("  [提示] 使用 --full 可同步完整目录结构 (包含示例脚本)")
        print("         修复版本会自动发现所有子目录文件")
    
    failed = sum(1 for r in results if r.action == "failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())