#!/usr/bin/env python3
"""一键同步 wshobson/agents 仓库到本地 (优化版 - ZIP 下载).

此脚本通过下载 GitHub 仓库的 ZIP 文件来同步所有插件，
避免了 GitHub API 的速率限制问题 (60次/小时)。

只需 1 次 HTTP 请求即可下载全部内容！

Usage:
    python sync_claude_agents.py --target-dir .claude/agents
    python sync_claude_agents.py --target-dir .claude/agents --category python-development
    python sync_claude_agents.py --list-plugins
    
Options:
    --target-dir    目标目录，默认为 .claude/agents
    --force         强制覆盖已存在的文件
    --dry-run       仅显示将要执行的操作，不实际下载
    --list-plugins  列出所有可用插件
    --list-agents   列出所有可用代理
    --category      仅同步指定类别的插件 (逗号分隔)
    --plugin        仅同步指定的插件 (逗号分隔)
    --exclude       排除指定的插件 (逗号分隔)
    --agents-only   仅同步 agents 目录
    --skills-only   仅同步 skills 目录
    --commands-only 仅同步 commands 目录
    --keep-zip      保留下载的 ZIP 文件
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import urllib.error
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# =============================================================================
# Configuration
# =============================================================================

GITHUB_REPO = "wshobson/agents"
GITHUB_BRANCH = "main"
# ZIP 下载 URL - 只需要 1 次请求！
GITHUB_ZIP_URL = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/{GITHUB_BRANCH}.zip"

# 插件子目录类型
PLUGIN_SUBDIRS = ["agents", "commands", "skills"]

# 插件类别映射
PLUGIN_CATEGORIES = {
    "development": [
        "debugging-tools", "backend-development", 
        "frontend-development", "multi-platform-development",
    ],
    "documentation": [
        "code-documentation", "api-documentation", "architecture-diagrams",
    ],
    "workflows": [
        "git-workflows", "full-stack-orchestration", "tdd-workflows",
    ],
    "testing": ["unit-testing", "tdd-workflows"],
    "quality": [
        "code-review-ai", "comprehensive-review", "performance-optimization",
    ],
    "ai-ml": [
        "llm-applications", "agent-orchestration", "context-engineering", "mlops",
    ],
    "data": ["data-engineering", "data-validation"],
    "database": ["database-design", "database-migrations"],
    "operations": [
        "incident-response", "diagnostics", "distributed-debugging", "observability",
    ],
    "performance": ["application-performance", "database-optimization"],
    "infrastructure": [
        "deployment", "validation", "kubernetes-operations",
        "cloud-infrastructure", "ci-cd-pipelines",
    ],
    "security": [
        "security-scanning", "compliance", "backend-security", "frontend-security",
    ],
    "languages": [
        "python-development", "javascript-typescript", "systems-programming",
        "jvm-languages", "scripting-languages", "functional-programming",
        "embedded-systems",
    ],
    "blockchain": ["blockchain-web3"],
    "finance": ["quantitative-trading"],
    "payments": ["payment-processing"],
    "gaming": ["game-development"],
    "marketing": [
        "seo-content", "technical-seo", "seo-analysis", "content-marketing",
    ],
    "business": ["business-analytics", "hr-legal", "customer-sales"],
}

# 反向映射
PLUGIN_TO_CATEGORY = {}
for category, plugins in PLUGIN_CATEGORIES.items():
    for plugin in plugins:
        PLUGIN_TO_CATEGORY[plugin] = category


@dataclass
class PluginInfo:
    """插件信息"""
    name: str
    category: str = ""
    agents: List[str] = field(default_factory=list)
    commands: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    
    @property
    def total_files(self) -> int:
        return len(self.agents) + len(self.commands) + len(self.skills)


@dataclass
class SyncResult:
    """同步结果"""
    file_path: str
    action: str
    message: str = ""


# =============================================================================
# Utility Functions
# =============================================================================

def download_with_progress(url: str, desc: str = "Downloading") -> bytes:
    """带进度显示的下载"""
    print(f"{desc}...")
    print(f"  URL: {url}")
    
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "claude-agents-sync/2.0"}
        )
        
        with urllib.request.urlopen(req, timeout=120) as resp:
            total_size = resp.headers.get('Content-Length')
            if total_size:
                total_size = int(total_size)
                print(f"  大小: {total_size / 1024 / 1024:.1f} MB")
            
            # 分块下载并显示进度
            data = bytearray()
            downloaded = 0
            block_size = 8192
            
            while True:
                chunk = resp.read(block_size)
                if not chunk:
                    break
                data.extend(chunk)
                downloaded += len(chunk)
                
                if total_size:
                    percent = downloaded * 100 / total_size
                    bar_len = 40
                    filled = int(bar_len * downloaded / total_size)
                    bar = '█' * filled + '░' * (bar_len - filled)
                    print(f"\r  进度: [{bar}] {percent:.1f}%", end='', flush=True)
            
            print()  # 换行
            return bytes(data)
            
    except urllib.error.HTTPError as e:
        print(f"\n  错误: HTTP {e.code} - {e.reason}")
        raise
    except Exception as e:
        print(f"\n  错误: {e}")
        raise


def compute_hash(content: bytes) -> str:
    """计算内容的短 hash"""
    return hashlib.sha256(content).hexdigest()[:12]


# =============================================================================
# ZIP-based Sync Logic
# =============================================================================

class AgentsSyncer:
    """Claude Agents 同步器 (ZIP 下载版)
    
    通过下载整个仓库的 ZIP 文件来避免 GitHub API 限制。
    只需 1 次 HTTP 请求！
    """
    
    def __init__(
        self,
        target_dir: str = ".claude/agents",
        force: bool = False,
        dry_run: bool = False,
        plugins: Optional[Set[str]] = None,
        categories: Optional[Set[str]] = None,
        exclude: Optional[Set[str]] = None,
        agents_only: bool = False,
        skills_only: bool = False,
        commands_only: bool = False,
        keep_zip: bool = False,
    ):
        self.target_dir = Path(target_dir)
        self.force = force
        self.dry_run = dry_run
        self.plugins_filter = plugins
        self.categories_filter = categories
        self.exclude = exclude or set()
        self.agents_only = agents_only
        self.skills_only = skills_only
        self.commands_only = commands_only
        self.keep_zip = keep_zip
        
        self.results: List[SyncResult] = []
        self.all_plugins: Dict[str, PluginInfo] = {}
        
        self.stats = {
            "plugins": 0,
            "agents": 0,
            "commands": 0,
            "skills": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
        }
    
    def should_sync_plugin(self, plugin_name: str) -> bool:
        """判断是否应该同步该插件"""
        if plugin_name in self.exclude:
            return False
        if self.plugins_filter and plugin_name not in self.plugins_filter:
            return False
        if self.categories_filter:
            plugin_category = PLUGIN_TO_CATEGORY.get(plugin_name, "other")
            if plugin_category not in self.categories_filter:
                return False
        return True
    
    def get_subdirs_to_sync(self) -> List[str]:
        """获取需要同步的子目录类型"""
        if self.agents_only:
            return ["agents"]
        if self.skills_only:
            return ["skills"]
        if self.commands_only:
            return ["commands"]
        return PLUGIN_SUBDIRS
    
    def analyze_zip(self, zf: zipfile.ZipFile) -> Dict[str, PluginInfo]:
        """分析 ZIP 文件内容，发现所有插件"""
        plugins: Dict[str, PluginInfo] = {}
        
        # ZIP 内的根目录名（如 agents-main）
        root_prefix = None
        
        for name in zf.namelist():
            # 找到根目录前缀
            if root_prefix is None:
                parts = name.split("/")
                if len(parts) > 1:
                    root_prefix = parts[0]
            
            # 解析路径: root/plugins/plugin-name/subdir/file.md
            if "/plugins/" not in name:
                continue
            if not name.endswith(".md"):
                continue
            
            parts = name.split("/")
            try:
                plugins_idx = parts.index("plugins")
                if len(parts) < plugins_idx + 4:
                    continue
                
                plugin_name = parts[plugins_idx + 1]
                subdir = parts[plugins_idx + 2]
                filename = parts[-1].replace(".md", "")
                
                if subdir not in PLUGIN_SUBDIRS:
                    continue
                
                if plugin_name not in plugins:
                    plugins[plugin_name] = PluginInfo(
                        name=plugin_name,
                        category=PLUGIN_TO_CATEGORY.get(plugin_name, "other"),
                    )
                
                if subdir == "agents":
                    plugins[plugin_name].agents.append(filename)
                elif subdir == "commands":
                    plugins[plugin_name].commands.append(filename)
                elif subdir == "skills":
                    plugins[plugin_name].skills.append(filename)
                    
            except (ValueError, IndexError):
                continue
        
        return plugins, root_prefix
    
    def extract_plugin(
        self, 
        zf: zipfile.ZipFile, 
        plugin_name: str, 
        info: PluginInfo,
        root_prefix: str
    ) -> List[SyncResult]:
        """从 ZIP 中提取单个插件的文件"""
        results = []
        subdirs = self.get_subdirs_to_sync()
        
        for subdir in subdirs:
            if subdir == "agents":
                files = info.agents
            elif subdir == "commands":
                files = info.commands
            elif subdir == "skills":
                files = info.skills
            else:
                continue
            
            for filename in files:
                # ZIP 内路径
                zip_path = f"{root_prefix}/plugins/{plugin_name}/{subdir}/{filename}.md"
                # 本地路径
                local_path = self.target_dir / plugin_name / subdir / f"{filename}.md"
                
                try:
                    content = zf.read(zip_path)
                    new_hash = compute_hash(content)
                    
                    # 检查是否需要更新
                    if local_path.exists() and not self.force:
                        local_hash = compute_hash(local_path.read_bytes())
                        if local_hash == new_hash:
                            results.append(SyncResult(str(local_path), "skipped", "Up to date"))
                            self.stats["skipped"] += 1
                            continue
                    
                    # 写入文件
                    if not self.dry_run:
                        local_path.parent.mkdir(parents=True, exist_ok=True)
                        local_path.write_bytes(content)
                    
                    action = "would_download" if self.dry_run else "downloaded"
                    results.append(SyncResult(str(local_path), action))
                    self.stats["downloaded"] += 1
                    self.stats[subdir] += 1
                    
                except KeyError:
                    results.append(SyncResult(str(local_path), "failed", "Not found in ZIP"))
                    self.stats["failed"] += 1
                except Exception as e:
                    results.append(SyncResult(str(local_path), "failed", str(e)))
                    self.stats["failed"] += 1
        
        return results
    
    def sync_all(self) -> List[SyncResult]:
        """同步所有插件"""
        print("=" * 60)
        print("Claude Agents 同步器 (ZIP 下载版)")
        print(f"源: https://github.com/{GITHUB_REPO}")
        print("=" * 60)
        print()
        
        # 步骤 1: 下载 ZIP
        print("[1/4] 下载仓库 ZIP 文件...")
        try:
            zip_data = download_with_progress(GITHUB_ZIP_URL, "下载中")
        except Exception as e:
            print(f"下载失败: {e}")
            return []
        
        print(f"  ✓ 下载完成 ({len(zip_data) / 1024 / 1024:.1f} MB)")
        
        # 可选保存 ZIP
        if self.keep_zip:
            zip_path = Path("agents-repo.zip")
            zip_path.write_bytes(zip_data)
            print(f"  ✓ ZIP 已保存: {zip_path}")
        
        # 步骤 2: 分析 ZIP 内容
        print("\n[2/4] 分析仓库结构...")
        
        try:
            with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                self.all_plugins, root_prefix = self.analyze_zip(zf)
        except zipfile.BadZipFile as e:
            print(f"  ✗ ZIP 文件损坏: {e}")
            return []
        
        print(f"  ✓ 发现 {len(self.all_plugins)} 个插件")
        
        # 步骤 3: 过滤插件
        print("\n[3/4] 筛选插件...")
        
        filtered_plugins = {
            name: info for name, info in self.all_plugins.items()
            if self.should_sync_plugin(name)
        }
        
        total_files = sum(p.total_files for p in filtered_plugins.values())
        print(f"  ✓ 筛选后: {len(filtered_plugins)} 个插件, {total_files} 个文件")
        
        if self.dry_run:
            print("  (DRY RUN - 不实际写入)")
        
        # 步骤 4: 提取文件
        print(f"\n[4/4] 提取文件到 {self.target_dir}...")
        print()
        
        # 按类别分组显示
        by_category: Dict[str, List[str]] = {}
        for plugin_name, info in filtered_plugins.items():
            category = info.category or "other"
            if category not in by_category:
                by_category[category] = []
            by_category[category].append(plugin_name)
        
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            for category in sorted(by_category.keys()):
                plugin_list = by_category[category]
                print(f"[{category}] ({len(plugin_list)} 插件)")
                
                for plugin_name in sorted(plugin_list):
                    info = filtered_plugins[plugin_name]
                    results = self.extract_plugin(zf, plugin_name, info, root_prefix)
                    self.results.extend(results)
                    self.stats["plugins"] += 1
                    
                    # 显示进度
                    success = sum(1 for r in results if r.action in ("downloaded", "would_download"))
                    skipped = sum(1 for r in results if r.action == "skipped")
                    failed = sum(1 for r in results if r.action == "failed")
                    
                    status_parts = []
                    if success > 0:
                        status_parts.append(f"✓{success}")
                    if skipped > 0:
                        status_parts.append(f"·{skipped}")
                    if failed > 0:
                        status_parts.append(f"✗{failed}")
                    
                    status = " ".join(status_parts) if status_parts else "empty"
                    
                    components = []
                    if info.agents:
                        components.append(f"{len(info.agents)}A")
                    if info.commands:
                        components.append(f"{len(info.commands)}C")
                    if info.skills:
                        components.append(f"{len(info.skills)}S")
                    
                    comp_str = "/".join(components) if components else "0"
                    print(f"  {plugin_name}: [{comp_str}] {status}")
                
                print()
        
        return self.results
    
    def generate_index(self) -> Dict[str, Any]:
        """生成索引文件"""
        index = {
            "generated_at": datetime.now().isoformat(),
            "source": f"https://github.com/{GITHUB_REPO}",
            "stats": {
                "plugins": self.stats["plugins"],
                "agents": self.stats["agents"],
                "commands": self.stats["commands"],
                "skills": self.stats["skills"],
            },
            "plugins": {},
            "agents": {},
            "skills": {},
            "commands": {},
        }
        
        for plugin_name, info in self.all_plugins.items():
            if not self.should_sync_plugin(plugin_name):
                continue
                
            index["plugins"][plugin_name] = {
                "category": info.category,
                "agents": info.agents,
                "commands": info.commands,
                "skills": info.skills,
            }
            
            for agent in info.agents:
                index["agents"][agent] = {
                    "plugin": plugin_name,
                    "file": f"{plugin_name}/agents/{agent}.md",
                }
            
            for skill in info.skills:
                index["skills"][skill] = {
                    "plugin": plugin_name,
                    "file": f"{plugin_name}/skills/{skill}.md",
                }
            
            for command in info.commands:
                index["commands"][command] = {
                    "plugin": plugin_name,
                    "file": f"{plugin_name}/commands/{command}.md",
                }
        
        return index
    
    def save_index(self) -> Path:
        """保存索引文件"""
        index = self.generate_index()
        index_path = self.target_dir / "agents_index.json"
        
        if not self.dry_run:
            self.target_dir.mkdir(parents=True, exist_ok=True)
            index_path.write_text(
                json.dumps(index, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
        
        return index_path
    
    def print_summary(self) -> None:
        """打印同步摘要"""
        print("=" * 60)
        print("同步摘要")
        print("=" * 60)
        print(f"  插件: {self.stats['plugins']}")
        print(f"  代理 (agents): {self.stats['agents']}")
        print(f"  命令 (commands): {self.stats['commands']}")
        print(f"  技能 (skills): {self.stats['skills']}")
        print(f"  ---")
        print(f"  已下载: {self.stats['downloaded']}")
        print(f"  已跳过: {self.stats['skipped']}")
        print(f"  失败: {self.stats['failed']}")


# =============================================================================
# List Functions (使用本地分析，不调用 API)
# =============================================================================

def list_all_plugins() -> None:
    """列出所有可用插件 (通过下载 ZIP 分析)"""
    print("正在下载仓库以分析插件列表...\n")
    
    try:
        zip_data = download_with_progress(GITHUB_ZIP_URL, "下载中")
    except Exception as e:
        print(f"下载失败: {e}")
        return
    
    syncer = AgentsSyncer()
    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        plugins, _ = syncer.analyze_zip(zf)
    
    # 按类别分组
    by_category: Dict[str, List[str]] = {"other": []}
    for plugin_name, info in plugins.items():
        category = info.category or "other"
        if category not in by_category:
            by_category[category] = []
        by_category[category].append(plugin_name)
    
    total_agents = sum(len(p.agents) for p in plugins.values())
    total_skills = sum(len(p.skills) for p in plugins.values())
    total_commands = sum(len(p.commands) for p in plugins.values())
    
    print(f"\n共 {len(plugins)} 个插件 ({total_agents} agents, {total_skills} skills, {total_commands} commands):\n")
    
    for category in sorted(by_category.keys()):
        plugin_list = by_category[category]
        if plugin_list:
            print(f"[{category}] ({len(plugin_list)})")
            for plugin_name in sorted(plugin_list):
                info = plugins[plugin_name]
                components = []
                if info.agents:
                    components.append(f"{len(info.agents)}A")
                if info.skills:
                    components.append(f"{len(info.skills)}S")
                if info.commands:
                    components.append(f"{len(info.commands)}C")
                comp_str = "/".join(components) if components else "empty"
                print(f"  · {plugin_name} [{comp_str}]")
            print()


def list_all_agents() -> None:
    """列出所有可用代理"""
    print("正在下载仓库以分析代理列表...\n")
    
    try:
        zip_data = download_with_progress(GITHUB_ZIP_URL, "下载中")
    except Exception as e:
        print(f"下载失败: {e}")
        return
    
    syncer = AgentsSyncer()
    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        plugins, _ = syncer.analyze_zip(zf)
    
    all_agents: Dict[str, str] = {}
    for plugin_name, info in plugins.items():
        for agent in info.agents:
            all_agents[agent] = plugin_name
    
    print(f"\n共 {len(all_agents)} 个代理:\n")
    
    # 按插件分组
    by_plugin: Dict[str, List[str]] = {}
    for agent, plugin in all_agents.items():
        if plugin not in by_plugin:
            by_plugin[plugin] = []
        by_plugin[plugin].append(agent)
    
    for plugin in sorted(by_plugin.keys()):
        agents = by_plugin[plugin]
        category = PLUGIN_TO_CATEGORY.get(plugin, "other")
        print(f"[{plugin}] ({category})")
        for agent in sorted(agents):
            print(f"  · {agent}")
        print()


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="一键同步 wshobson/agents 到本地 (ZIP 下载版 - 无 API 限制)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python sync_claude_agents.py                           # 同步所有插件
  python sync_claude_agents.py --target-dir .claude/agents
  python sync_claude_agents.py --category languages,ai-ml   # 仅同步指定类别
  python sync_claude_agents.py --plugin python-development,kubernetes-operations
  python sync_claude_agents.py --agents-only             # 仅同步 agents
  python sync_claude_agents.py --list-plugins            # 列出所有插件
  python sync_claude_agents.py --list-agents             # 列出所有代理

类别:
  development, documentation, workflows, testing, quality,
  ai-ml, data, database, operations, performance, infrastructure,
  security, languages, blockchain, finance, payments, gaming,
  marketing, business

优势:
  - 使用 ZIP 下载，只需 1 次 HTTP 请求
  - 完全避免 GitHub API 速率限制 (60次/小时)
  - 下载速度更快，更稳定
"""
    )
    
    parser.add_argument(
        "--target-dir", "-t",
        default=".claude/agents",
        help="目标目录 (default: .claude/agents)"
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="强制覆盖已存在的文件"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="仅显示将要执行的操作，不实际下载"
    )
    parser.add_argument(
        "--plugin", "-p",
        help="仅同步指定的插件 (逗号分隔)"
    )
    parser.add_argument(
        "--category", "-c",
        help="仅同步指定类别的插件 (逗号分隔)"
    )
    parser.add_argument(
        "--exclude", "-e",
        help="排除指定的插件 (逗号分隔)"
    )
    parser.add_argument(
        "--agents-only",
        action="store_true",
        help="仅同步 agents 目录"
    )
    parser.add_argument(
        "--skills-only",
        action="store_true",
        help="仅同步 skills 目录"
    )
    parser.add_argument(
        "--commands-only",
        action="store_true",
        help="仅同步 commands 目录"
    )
    parser.add_argument(
        "--list-plugins",
        action="store_true",
        help="列出所有可用插件"
    )
    parser.add_argument(
        "--list-agents",
        action="store_true",
        help="列出所有可用代理"
    )
    parser.add_argument(
        "--keep-zip",
        action="store_true",
        help="保留下载的 ZIP 文件"
    )
    
    args = parser.parse_args()
    
    # 列表模式
    if args.list_plugins:
        list_all_plugins()
        return 0
    
    if args.list_agents:
        list_all_agents()
        return 0
    
    # 同步模式
    plugins = set(args.plugin.split(",")) if args.plugin else None
    categories = set(args.category.split(",")) if args.category else None
    exclude = set(args.exclude.split(",")) if args.exclude else None
    
    syncer = AgentsSyncer(
        target_dir=args.target_dir,
        force=args.force,
        dry_run=args.dry_run,
        plugins=plugins,
        categories=categories,
        exclude=exclude,
        agents_only=args.agents_only,
        skills_only=args.skills_only,
        commands_only=args.commands_only,
        keep_zip=args.keep_zip,
    )
    
    results = syncer.sync_all()
    
    if not results:
        return 1
    
    syncer.print_summary()
    
    # 生成索引
    if not args.dry_run and syncer.stats["downloaded"] > 0:
        print("\n生成索引文件...")
        index_path = syncer.save_index()
        print(f"  → {index_path}")
    
    # 使用建议
    print("\n" + "=" * 60)
    print("使用建议")
    print("=" * 60)
    print(f"""
1. 在 CLAUDE.md 中引用:
   
   ## Available Agents
   See .claude/agents/agents_index.json for all available agents.
   
2. 在 Playbook 中使用:
   
   agents:
     - python-pro
     - security-auditor
   
3. 查看特定代理:
   
   cat {args.target_dir}/python-development/agents/python-pro.md
""")
    
    failed = syncer.stats["failed"]
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
