#!/usr/bin/env python3
"""从本地 ZIP 文件同步 skills 到目标目录.

此脚本从本地 ZIP 文件中解压 skills 到指定目标目录，
支持完整目录结构或扁平文件结构。

Usage:
    python sync_skills_from_zip.py --source ../claudeskills/myskills --target-dir .claude/skills --full
    python sync_skills_from_zip.py --source ./skills.zip --target-dir skills/ --force
    
Options:
    --source        ZIP 文件路径或包含 ZIP 文件的目录
    --target-dir    目标目录，默认为 skills/
    --force         强制覆盖已存在的文件
    --dry-run       仅显示将要执行的操作，不实际解压
    --include       仅同步指定的 skills (逗号分隔)
    --exclude       排除指定的 skills (逗号分隔)
    --full          保持完整目录结构 (包括 scripts/, references/, assets/)
    --flat          仅提取 SKILL.md 为扁平结构 (默认)
    --list          列出 ZIP 文件中的 skills，不执行解压

ZIP 文件结构要求:
    skills.zip
    └── skills/          # 或直接是 skill 目录
        ├── skill-name-1/
        │   ├── SKILL.md
        │   ├── scripts/
        │   └── references/
        └── skill-name-2/
            └── SKILL.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# =============================================================================
# Configuration
# =============================================================================

# 每个 skill 目录下可能包含的子目录
SKILL_SUBDIRS = ["scripts", "references", "assets"]

# 支持的 ZIP 文件扩展名
ZIP_EXTENSIONS = [".zip", ".ZIP"]


@dataclass
class SkillInfo:
    """Skill 元信息"""
    name: str
    summary: str = ""
    version: str = "1.0.0"
    file: str = "SKILL.md"
    files: List[str] = field(default_factory=list)  # ZIP 中的所有文件
    hash: str = ""


@dataclass
class SyncResult:
    """同步结果"""
    skill: str
    action: str  # extracted, updated, skipped, failed
    message: str = ""
    files_count: int = 0


# =============================================================================
# Utility Functions
# =============================================================================

def compute_hash(content: bytes) -> str:
    """计算内容的短 hash"""
    return hashlib.sha256(content).hexdigest()[:12]


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


def find_zip_files(source: str) -> List[Path]:
    """查找 ZIP 文件
    
    Args:
        source: ZIP 文件路径或包含 ZIP 文件的目录
        
    Returns:
        ZIP 文件路径列表
    """
    source_path = Path(source)
    
    if source_path.is_file():
        if source_path.suffix in ZIP_EXTENSIONS:
            return [source_path]
        else:
            print(f"Warning: {source} is not a ZIP file", file=sys.stderr)
            return []
    
    if source_path.is_dir():
        zip_files = []
        for ext in ZIP_EXTENSIONS:
            zip_files.extend(source_path.glob(f"*{ext}"))
        return sorted(zip_files)
    
    print(f"Error: {source} does not exist", file=sys.stderr)
    return []


def normalize_zip_path(zip_path: str) -> str:
    """标准化 ZIP 内部路径，去除前导目录"""
    # 去除常见的前导目录如 "skills/", "Agent-Skills-xxx/"
    parts = Path(zip_path).parts
    
    # 查找 skill 目录的起始位置
    for i, part in enumerate(parts):
        # 如果是 skill 名称模式 (包含连字符的目录名)
        if "-" in part and i < len(parts) - 1:
            # 检查下一级是否是 SKILL.md 或子目录
            remaining = parts[i:]
            if len(remaining) >= 2:
                next_part = remaining[1]
                if next_part == "SKILL.md" or next_part in SKILL_SUBDIRS:
                    return str(Path(*remaining))
        # 如果当前目录下直接是 SKILL.md
        if part == "SKILL.md":
            if i > 0:
                return str(Path(*parts[i-1:]))
            return zip_path
    
    return zip_path


# =============================================================================
# ZIP Analysis
# =============================================================================

class ZipAnalyzer:
    """分析 ZIP 文件中的 skills 结构"""
    
    def __init__(self, zip_path: Path):
        self.zip_path = zip_path
        self.skills: Dict[str, SkillInfo] = {}
        self._analyze()
    
    def _analyze(self) -> None:
        """分析 ZIP 文件结构"""
        try:
            with zipfile.ZipFile(self.zip_path, 'r') as zf:
                all_files = zf.namelist()
                
                # 查找所有 SKILL.md 文件
                skill_files = [f for f in all_files if f.endswith("SKILL.md")]
                
                for skill_file in skill_files:
                    # 获取 skill 名称 (SKILL.md 的父目录)
                    parts = Path(skill_file).parts
                    if len(parts) < 2:
                        continue
                    
                    # 找到 SKILL.md 的直接父目录作为 skill 名称
                    skill_name = parts[-2]
                    
                    # 跳过非 skill 目录
                    if skill_name in ["skills", "."]:
                        continue
                    
                    # 计算 skill 的根路径前缀
                    skill_prefix = "/".join(parts[:-1]) + "/"
                    
                    # 收集该 skill 下的所有文件
                    skill_files_list = [
                        f for f in all_files 
                        if f.startswith(skill_prefix) and not f.endswith("/")
                    ]
                    
                    # 读取 SKILL.md 内容获取元数据
                    try:
                        content = zf.read(skill_file).decode("utf-8")
                        metadata = extract_skill_metadata(content)
                        content_hash = compute_hash(content.encode())
                    except Exception:
                        metadata = {"summary": "", "version": "1.0.0"}
                        content_hash = ""
                    
                    self.skills[skill_name] = SkillInfo(
                        name=skill_name,
                        summary=metadata.get("summary", ""),
                        version=metadata.get("version", "1.0.0"),
                        file=skill_file,
                        files=skill_files_list,
                        hash=content_hash,
                    )
                    
        except zipfile.BadZipFile as e:
            print(f"Error: Invalid ZIP file {self.zip_path}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"Error analyzing {self.zip_path}: {e}", file=sys.stderr)
    
    def list_skills(self) -> List[str]:
        """列出所有 skill 名称"""
        return sorted(self.skills.keys())
    
    def get_skill_files(self, skill_name: str) -> List[str]:
        """获取指定 skill 的所有文件"""
        if skill_name in self.skills:
            return self.skills[skill_name].files
        return []
    
    def print_summary(self) -> None:
        """打印 ZIP 文件内容摘要"""
        print(f"\nZIP 文件: {self.zip_path}")
        print(f"发现 {len(self.skills)} 个 skills:")
        print("")
        
        for name, info in sorted(self.skills.items()):
            file_count = len(info.files)
            subdirs = set()
            for f in info.files:
                rel_path = f.replace(str(Path(info.file).parent) + "/", "")
                if "/" in rel_path:
                    subdirs.add(rel_path.split("/")[0])
            
            subdir_str = f" [{', '.join(sorted(subdirs))}]" if subdirs else ""
            print(f"  · {name}: {file_count} 文件{subdir_str}")
            if info.summary:
                print(f"    {info.summary[:60]}...")


# =============================================================================
# Sync Logic
# =============================================================================

class ZipSkillSyncer:
    """从 ZIP 文件同步 skills
    
    支持两种模式:
    1. flat_structure=True (默认): 仅提取 SKILL.md 为扁平文件
    2. flat_structure=False (--full): 提取完整目录结构
    """
    
    def __init__(
        self,
        source: str,
        target_dir: str = "skills",
        force: bool = False,
        dry_run: bool = False,
        include: Optional[Set[str]] = None,
        exclude: Optional[Set[str]] = None,
        flat_structure: bool = True,
    ):
        self.source = source
        self.target_dir = Path(target_dir)
        self.force = force
        self.dry_run = dry_run
        self.include = include
        self.exclude = exclude or set()
        self.flat_structure = flat_structure
        self.results: List[SyncResult] = []
        self.zip_files: List[Path] = []
        self.all_skills: Dict[str, Tuple[Path, SkillInfo]] = {}  # skill_name -> (zip_path, info)
    
    def should_sync(self, skill_name: str) -> bool:
        """判断是否应该同步该 skill"""
        if self.include and skill_name not in self.include:
            return False
        if skill_name in self.exclude:
            return False
        return True
    
    def discover_skills(self) -> Dict[str, Tuple[Path, SkillInfo]]:
        """发现所有 ZIP 文件中的 skills"""
        self.zip_files = find_zip_files(self.source)
        
        if not self.zip_files:
            print(f"Error: No ZIP files found in {self.source}", file=sys.stderr)
            return {}
        
        print(f"找到 {len(self.zip_files)} 个 ZIP 文件:")
        for zf in self.zip_files:
            print(f"  · {zf.name}")
        print("")
        
        for zip_path in self.zip_files:
            analyzer = ZipAnalyzer(zip_path)
            for skill_name, info in analyzer.skills.items():
                # 如果同一个 skill 在多个 ZIP 中存在，使用最新的
                if skill_name not in self.all_skills:
                    self.all_skills[skill_name] = (zip_path, info)
                else:
                    # 比较版本或 hash，保留较新的
                    existing_info = self.all_skills[skill_name][1]
                    if info.version > existing_info.version:
                        self.all_skills[skill_name] = (zip_path, info)
        
        return self.all_skills
    
    def get_local_path(self, skill_name: str, subpath: str = "SKILL.md") -> Path:
        """获取本地目标路径"""
        if self.flat_structure:
            # 扁平模式: skills/context-fundamentals.md
            return self.target_dir / f"{skill_name}.md"
        
        # 完整目录模式: skills/context-fundamentals/SKILL.md
        #              skills/context-fundamentals/scripts/example.py
        return self.target_dir / skill_name / subpath
    
    def extract_file(
        self, 
        zf: zipfile.ZipFile, 
        zip_file_path: str, 
        local_path: Path
    ) -> bool:
        """提取单个文件
        
        Returns:
            True if file was extracted/updated, False if skipped
        """
        try:
            # 读取 ZIP 中的内容
            content = zf.read(zip_file_path)
            new_hash = compute_hash(content)
            
            # 检查本地文件是否需要更新
            if local_path.exists() and not self.force:
                try:
                    local_content = local_path.read_bytes()
                    local_hash = compute_hash(local_content)
                    if local_hash == new_hash:
                        return False  # 跳过，内容相同
                except Exception:
                    pass
            
            # 执行写入
            if not self.dry_run:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_bytes(content)
            
            return True
            
        except Exception as e:
            print(f"    Error extracting {zip_file_path}: {e}", file=sys.stderr)
            return False
    
    def sync_skill(self, skill_name: str, zip_path: Path, info: SkillInfo) -> SyncResult:
        """同步单个 skill"""
        if not self.should_sync(skill_name):
            return SyncResult(skill_name, "skipped", "Excluded by filter")
        
        extracted_count = 0
        skipped_count = 0
        
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                # 获取 skill 的根目录前缀
                skill_root = str(Path(info.file).parent)
                
                if self.flat_structure:
                    # 扁平模式: 只提取 SKILL.md
                    local_path = self.get_local_path(skill_name)
                    if self.extract_file(zf, info.file, local_path):
                        extracted_count += 1
                    else:
                        skipped_count += 1
                else:
                    # 完整目录模式: 提取所有文件
                    for zip_file_path in info.files:
                        # 计算相对路径
                        rel_path = zip_file_path[len(skill_root):].lstrip("/")
                        if not rel_path:
                            continue
                        
                        local_path = self.get_local_path(skill_name, rel_path)
                        
                        if self.extract_file(zf, zip_file_path, local_path):
                            extracted_count += 1
                            if not self.dry_run:
                                print(f"    ✓ {rel_path}")
                        else:
                            skipped_count += 1
        
        except Exception as e:
            return SyncResult(skill_name, "failed", str(e))
        
        if extracted_count > 0:
            action = "would_extract" if self.dry_run else "extracted"
            return SyncResult(skill_name, action, "", extracted_count)
        elif skipped_count > 0:
            return SyncResult(skill_name, "skipped", "Up to date", skipped_count)
        else:
            return SyncResult(skill_name, "failed", "No files found")
    
    def sync_all(self) -> List[SyncResult]:
        """同步所有 skills"""
        skills = self.discover_skills()
        
        if not skills:
            print("No skills found to sync")
            return []
        
        mode = "完整目录" if not self.flat_structure else "扁平文件"
        print(f"发现 {len(skills)} 个 skills (模式: {mode})")
        print("")
        
        for skill_name in sorted(skills.keys()):
            zip_path, info = skills[skill_name]
            result = self.sync_skill(skill_name, zip_path, info)
            self.results.append(result)
            
            icon = {
                "extracted": "✓",
                "updated": "↑",
                "skipped": "·",
                "failed": "✗",
                "would_extract": "?",
            }.get(result.action, "?")
            
            msg = f" ({result.files_count} files)" if result.files_count else ""
            msg += f" {result.message}" if result.message else ""
            print(f"  {icon} {result.skill}: {result.action}{msg}")
        
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
            
            try:
                content = path.read_text(encoding="utf-8")
                metadata = extract_skill_metadata(content)
            except Exception:
                metadata = {"summary": "", "version": "1.0.0", "triggers": []}
            
            entry: Dict[str, Any] = {
                "summary": metadata.get("summary", ""),
                "file": path.name if self.flat_structure else f"{skill_name}/SKILL.md",
                "version": metadata.get("version", "1.0.0"),
                "triggers": metadata.get("triggers", [])[:5],
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
        extracted = sum(1 for r in self.results if r.action in ("extracted", "updated"))
        skipped = sum(1 for r in self.results if r.action == "skipped")
        failed = sum(1 for r in self.results if r.action == "failed")
        
        total_files = sum(r.files_count for r in self.results if r.action in ("extracted", "updated"))
        
        print(f"\n摘要: {extracted} 同步, {skipped} 跳过, {failed} 失败")
        if total_files > 0:
            print(f"共提取 {total_files} 个文件")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="从本地 ZIP 文件同步 skills",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python sync_skills_from_zip.py --source ./downloads --target-dir skills/
  python sync_skills_from_zip.py --source skills.zip --full --force
  python sync_skills_from_zip.py --source ./zips --list
  python sync_skills_from_zip.py --source ./zips --include context-optimization,tool-design

ZIP 文件结构:
  脚本会自动识别以下结构:
  
  1. 标准结构:
     skills.zip
     └── skills/
         └── skill-name/
             ├── SKILL.md
             └── scripts/
  
  2. 扁平结构:
     skills.zip
     └── skill-name/
         ├── SKILL.md
         └── scripts/
"""
    )
    parser.add_argument(
        "--source", "-s",
        required=True,
        help="ZIP 文件路径或包含 ZIP 文件的目录"
    )
    parser.add_argument(
        "--target-dir", "-t",
        default="skills",
        help="目标目录 (default: skills/)"
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="强制覆盖已存在的文件"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="仅显示将要执行的操作，不实际解压"
    )
    parser.add_argument(
        "--include",
        help="仅同步指定的 skills (逗号分隔)"
    )
    parser.add_argument(
        "--exclude",
        help="排除指定的 skills (逗号分隔)"
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="列出 ZIP 文件中的 skills，不执行解压"
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        default=True,
        help="仅提取 SKILL.md 为扁平结构 (默认)"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="保持完整目录结构 (包括 scripts/, references/, assets/)"
    )
    
    args = parser.parse_args()
    
    # 列表模式
    if args.list:
        zip_files = find_zip_files(args.source)
        if not zip_files:
            print(f"No ZIP files found in {args.source}")
            return 1
        
        for zip_path in zip_files:
            analyzer = ZipAnalyzer(zip_path)
            analyzer.print_summary()
        return 0
    
    # 同步模式
    include = set(args.include.split(",")) if args.include else None
    exclude = set(args.exclude.split(",")) if args.exclude else None
    flat_structure = not args.full
    
    syncer = ZipSkillSyncer(
        source=args.source,
        target_dir=args.target_dir,
        force=args.force,
        dry_run=args.dry_run,
        include=include,
        exclude=exclude,
        flat_structure=flat_structure,
    )
    
    mode = "扁平文件 (仅 SKILL.md)" if flat_structure else "完整目录结构"
    print(f"从 ZIP 同步 Skills 到 {args.target_dir}/")
    print(f"模式: {mode}")
    if args.dry_run:
        print("(DRY RUN - 不实际修改)")
    print("")
    
    results = syncer.sync_all()
    
    if not results:
        return 1
    
    syncer.print_summary()
    
    # 生成索引
    extracted = sum(1 for r in results if r.action in ("extracted", "updated"))
    if not args.dry_run and extracted > 0:
        print("\n生成 skill_index.json...")
        index_path = syncer.save_index()
        print(f"  → {index_path}")
    
    failed = sum(1 for r in results if r.action == "failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
