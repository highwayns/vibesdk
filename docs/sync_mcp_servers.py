#!/usr/bin/env python3
"""一键同步 MCP (Model Context Protocol) 服务器到本地.

此脚本从多个来源同步 MCP 服务器定义：
1. modelcontextprotocol/servers (官方参考实现)
2. punkpeye/awesome-mcp-servers (社区精选)
3. 解析 README 提取所有 MCP 服务器元数据

下载后可供 Playbook 转换和执行时使用。

Usage:
    python sync_mcp_servers.py --target-dir .claude/mcps
    python sync_mcp_servers.py --target-dir .claude/mcps --official-only
    python sync_mcp_servers.py --list-servers
    python sync_mcp_servers.py --category database
    
Options:
    --target-dir      目标目录，默认为 .claude/mcps
    --official-only   仅同步官方参考实现
    --community-only  仅同步社区 MCP 索引
    --category        筛选特定类别
    --list-servers    列出所有可用 MCP 服务器
    --list-categories 列出所有类别
    --force           强制覆盖已存在的文件
    --dry-run         仅显示将要执行的操作
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
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

# 官方 MCP 仓库
OFFICIAL_REPO = "modelcontextprotocol/servers"
OFFICIAL_BRANCH = "main"
OFFICIAL_ZIP_URL = f"https://github.com/{OFFICIAL_REPO}/archive/refs/heads/{OFFICIAL_BRANCH}.zip"

# 社区精选仓库
AWESOME_REPO = "punkpeye/awesome-mcp-servers"
AWESOME_BRANCH = "main"
AWESOME_README_URL = f"https://raw.githubusercontent.com/{AWESOME_REPO}/{AWESOME_BRANCH}/README.md"

# 官方参考服务器列表
OFFICIAL_SERVERS = [
    "everything",      # Reference / test server
    "fetch",           # Web content fetching
    "filesystem",      # Secure file operations
    "git",             # Git repository tools
    "memory",          # Knowledge graph memory
    "sequentialthinking",  # Problem-solving
    "time",            # Time and timezone
]

# MCP 服务器类别 (基于 awesome-mcp-servers)
MCP_CATEGORIES = {
    "ai-platforms": "AI Platforms & Models",
    "browser-automation": "Browser Automation",
    "cloud-platforms": "Cloud Platforms",
    "code-execution": "Code Execution",
    "communication": "Communication",
    "customer-data": "Customer Data Platforms",
    "data-science": "Data Science & Analytics",
    "database": "Databases",
    "developer-tools": "Developer Tools",
    "file-systems": "File Systems",
    "finance-fintech": "Finance & Fintech",
    "gaming": "Gaming",
    "knowledge-memory": "Knowledge & Memory",
    "location-travel": "Location & Travel",
    "marketing": "Marketing",
    "media-content": "Media & Content",
    "monitoring": "Monitoring & Observability",
    "productivity": "Productivity",
    "search": "Search",
    "security": "Security",
    "version-control": "Version Control",
    "web-scraping": "Web Scraping",
    "other": "Other",
}


@dataclass
class MCPServerInfo:
    """MCP 服务器信息"""
    name: str
    description: str = ""
    repo: str = ""  # GitHub 仓库
    category: str = "other"
    official: bool = False
    npm_package: str = ""
    pypi_package: str = ""
    language: str = ""  # typescript, python, go, etc.
    features: List[str] = field(default_factory=list)
    config_example: Dict[str, Any] = field(default_factory=dict)


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
            headers={"User-Agent": "mcp-servers-sync/1.0"}
        )
        
        with urllib.request.urlopen(req, timeout=120) as resp:
            total_size = resp.headers.get('Content-Length')
            if total_size:
                total_size = int(total_size)
                print(f"  大小: {total_size / 1024:.1f} KB")
            
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
            
            print()
            return bytes(data)
            
    except urllib.error.HTTPError as e:
        print(f"\n  错误: HTTP {e.code} - {e.reason}")
        raise
    except Exception as e:
        print(f"\n  错误: {e}")
        raise


def fetch_text(url: str, timeout: int = 30) -> Optional[str]:
    """获取文本内容"""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "mcp-servers-sync/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        print(f"Warning: Failed to fetch {url}: {e}", file=sys.stderr)
        return None


def compute_hash(content: bytes) -> str:
    """计算内容的短 hash"""
    return hashlib.sha256(content).hexdigest()[:12]


# =============================================================================
# README Parser
# =============================================================================

class AwesomeMCPParser:
    """解析 awesome-mcp-servers README"""
    
    def __init__(self, readme_content: str):
        self.content = readme_content
        self.servers: Dict[str, MCPServerInfo] = {}
    
    def parse(self) -> Dict[str, MCPServerInfo]:
        """解析 README 提取所有 MCP 服务器"""
        lines = self.content.split('\n')
        current_category = "other"
        
        # 类别映射
        category_keywords = {
            "ai": "ai-platforms",
            "llm": "ai-platforms",
            "browser": "browser-automation",
            "automation": "browser-automation",
            "playwright": "browser-automation",
            "cloud": "cloud-platforms",
            "aws": "cloud-platforms",
            "azure": "cloud-platforms",
            "gcp": "cloud-platforms",
            "code": "code-execution",
            "execution": "code-execution",
            "sandbox": "code-execution",
            "communication": "communication",
            "slack": "communication",
            "email": "communication",
            "discord": "communication",
            "database": "database",
            "sql": "database",
            "postgres": "database",
            "mysql": "database",
            "mongodb": "database",
            "redis": "database",
            "developer": "developer-tools",
            "git": "version-control",
            "github": "version-control",
            "gitlab": "version-control",
            "file": "file-systems",
            "filesystem": "file-systems",
            "finance": "finance-fintech",
            "payment": "finance-fintech",
            "crypto": "finance-fintech",
            "trading": "finance-fintech",
            "game": "gaming",
            "knowledge": "knowledge-memory",
            "memory": "knowledge-memory",
            "rag": "knowledge-memory",
            "location": "location-travel",
            "map": "location-travel",
            "travel": "location-travel",
            "marketing": "marketing",
            "seo": "marketing",
            "media": "media-content",
            "image": "media-content",
            "video": "media-content",
            "audio": "media-content",
            "monitor": "monitoring",
            "observability": "monitoring",
            "log": "monitoring",
            "productivity": "productivity",
            "calendar": "productivity",
            "task": "productivity",
            "search": "search",
            "security": "security",
            "auth": "security",
            "scraping": "web-scraping",
            "crawl": "web-scraping",
        }
        
        for line in lines:
            # 检测类别标题
            if line.startswith('##'):
                header = line.lower()
                for keyword, category in category_keywords.items():
                    if keyword in header:
                        current_category = category
                        break
            
            # 解析服务器条目
            # 格式: - [Name](url) - Description
            # 或: - **[Name](url)** - Description
            match = re.match(
                r'[-*]\s+\*?\*?\[([^\]]+)\]\(([^)]+)\)\*?\*?\s*[-–—]?\s*(.*)',
                line.strip()
            )
            
            if match:
                name = match.group(1).strip()
                url = match.group(2).strip()
                description = match.group(3).strip()
                
                # 提取仓库信息
                repo = ""
                if "github.com" in url:
                    repo_match = re.search(r'github\.com/([^/]+/[^/]+)', url)
                    if repo_match:
                        repo = repo_match.group(1)
                
                # 清理名称
                name_clean = re.sub(r'[^\w\-]', '-', name.lower())
                name_clean = re.sub(r'-+', '-', name_clean).strip('-')
                
                if name_clean and len(name_clean) > 2:
                    # 检测语言
                    language = "typescript"  # 默认
                    if "🐍" in line or "python" in line.lower():
                        language = "python"
                    elif "🏎️" in line or "go" in line.lower():
                        language = "go"
                    elif "🦀" in line or "rust" in line.lower():
                        language = "rust"
                    
                    self.servers[name_clean] = MCPServerInfo(
                        name=name,
                        description=description[:500] if description else "",
                        repo=repo,
                        category=current_category,
                        language=language,
                    )
        
        return self.servers


# =============================================================================
# MCP Syncer
# =============================================================================

class MCPSyncer:
    """MCP 服务器同步器"""
    
    def __init__(
        self,
        target_dir: str = ".claude/mcps",
        official_only: bool = False,
        community_only: bool = False,
        categories: Optional[Set[str]] = None,
        force: bool = False,
        dry_run: bool = False,
    ):
        self.target_dir = Path(target_dir)
        self.official_only = official_only
        self.community_only = community_only
        self.categories = categories
        self.force = force
        self.dry_run = dry_run
        
        self.results: List[SyncResult] = []
        self.all_servers: Dict[str, MCPServerInfo] = {}
        
        self.stats = {
            "official": 0,
            "community": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
        }
    
    def sync_official_servers(self) -> Dict[str, MCPServerInfo]:
        """同步官方 MCP 服务器"""
        print("\n[1/3] 下载官方 MCP 仓库...")
        
        try:
            zip_data = download_with_progress(OFFICIAL_ZIP_URL, "下载官方仓库")
        except Exception as e:
            print(f"  ✗ 下载失败: {e}")
            return {}
        
        servers: Dict[str, MCPServerInfo] = {}
        
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            # 找到根目录
            root_prefix = None
            for name in zf.namelist():
                if "/" in name:
                    root_prefix = name.split("/")[0]
                    break
            
            if not root_prefix:
                print("  ✗ 无法解析 ZIP 结构")
                return {}
            
            # 提取每个官方服务器
            for server_name in OFFICIAL_SERVERS:
                server_path = f"{root_prefix}/src/{server_name}"
                readme_path = f"{server_path}/README.md"
                
                # 读取 README
                description = ""
                try:
                    readme_content = zf.read(readme_path).decode("utf-8")
                    # 提取第一段作为描述
                    lines = readme_content.split('\n')
                    for line in lines:
                        if line.strip() and not line.startswith('#'):
                            description = line.strip()[:300]
                            break
                except:
                    pass
                
                # 读取 package.json 获取 npm 包名
                npm_package = ""
                try:
                    pkg_content = zf.read(f"{server_path}/package.json").decode("utf-8")
                    pkg_data = json.loads(pkg_content)
                    npm_package = pkg_data.get("name", "")
                except:
                    npm_package = f"@modelcontextprotocol/server-{server_name}"
                
                servers[server_name] = MCPServerInfo(
                    name=server_name,
                    description=description,
                    repo=OFFICIAL_REPO,
                    category=self._categorize_official(server_name),
                    official=True,
                    npm_package=npm_package,
                    language="typescript",
                    config_example=self._get_config_example(server_name, npm_package),
                )
                
                # 保存 README
                if not self.dry_run:
                    local_path = self.target_dir / "official" / server_name / "README.md"
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        readme_data = zf.read(readme_path)
                        local_path.write_bytes(readme_data)
                        self.stats["downloaded"] += 1
                    except:
                        pass
                
                self.stats["official"] += 1
                print(f"  ✓ {server_name}")
        
        return servers
    
    def _categorize_official(self, name: str) -> str:
        """为官方服务器分类"""
        categories = {
            "everything": "developer-tools",
            "fetch": "web-scraping",
            "filesystem": "file-systems",
            "git": "version-control",
            "memory": "knowledge-memory",
            "sequentialthinking": "ai-platforms",
            "time": "productivity",
        }
        return categories.get(name, "other")
    
    def _get_config_example(self, name: str, npm_package: str) -> Dict[str, Any]:
        """生成配置示例"""
        base_config = {
            "command": "npx",
            "args": ["-y", npm_package],
        }
        
        # 特殊配置
        if name == "filesystem":
            base_config["args"].append("/path/to/allowed/files")
        elif name == "git":
            base_config["args"].append("/path/to/repo")
        
        return base_config
    
    def sync_community_servers(self) -> Dict[str, MCPServerInfo]:
        """同步社区 MCP 服务器索引"""
        print("\n[2/3] 解析社区 MCP 索引...")
        
        readme = fetch_text(AWESOME_README_URL)
        if not readme:
            print("  ✗ 无法获取 awesome-mcp-servers README")
            return {}
        
        parser = AwesomeMCPParser(readme)
        servers = parser.parse()
        
        print(f"  ✓ 发现 {len(servers)} 个社区 MCP 服务器")
        
        # 按类别统计
        by_category: Dict[str, int] = {}
        for server in servers.values():
            cat = server.category
            by_category[cat] = by_category.get(cat, 0) + 1
        
        for cat, count in sorted(by_category.items(), key=lambda x: -x[1])[:10]:
            cat_name = MCP_CATEGORIES.get(cat, cat)
            print(f"    · {cat_name}: {count}")
        
        self.stats["community"] = len(servers)
        
        return servers
    
    def filter_servers(self, servers: Dict[str, MCPServerInfo]) -> Dict[str, MCPServerInfo]:
        """按类别筛选服务器"""
        if not self.categories:
            return servers
        
        filtered = {
            name: info for name, info in servers.items()
            if info.category in self.categories
        }
        
        print(f"\n筛选后: {len(filtered)} 个服务器 (类别: {', '.join(self.categories)})")
        return filtered
    
    def sync_all(self) -> List[SyncResult]:
        """同步所有 MCP 服务器"""
        print("=" * 60)
        print("MCP 服务器同步工具")
        print(f"源: {OFFICIAL_REPO}, {AWESOME_REPO}")
        print("=" * 60)
        
        # 同步官方服务器
        if not self.community_only:
            official = self.sync_official_servers()
            self.all_servers.update(official)
        
        # 同步社区服务器
        if not self.official_only:
            community = self.sync_community_servers()
            # 不覆盖官方服务器
            for name, info in community.items():
                if name not in self.all_servers:
                    self.all_servers[name] = info
        
        # 筛选
        self.all_servers = self.filter_servers(self.all_servers)
        
        return self.results
    
    def generate_index(self) -> Dict[str, Any]:
        """生成索引文件"""
        index = {
            "generated_at": datetime.now().isoformat(),
            "sources": [
                f"https://github.com/{OFFICIAL_REPO}",
                f"https://github.com/{AWESOME_REPO}",
            ],
            "stats": {
                "total": len(self.all_servers),
                "official": self.stats["official"],
                "community": self.stats["community"],
            },
            "categories": {},
            "servers": {},
        }
        
        # 按类别分组
        for name, info in self.all_servers.items():
            cat = info.category
            if cat not in index["categories"]:
                index["categories"][cat] = {
                    "name": MCP_CATEGORIES.get(cat, cat),
                    "servers": [],
                }
            index["categories"][cat]["servers"].append(name)
            
            # 服务器详情
            index["servers"][name] = {
                "name": info.name,
                "description": info.description,
                "repo": info.repo,
                "category": info.category,
                "official": info.official,
                "npm_package": info.npm_package,
                "pypi_package": info.pypi_package,
                "language": info.language,
                "config_example": info.config_example,
            }
        
        return index
    
    def generate_claude_config(self) -> Dict[str, Any]:
        """生成 Claude Desktop 配置片段"""
        config = {"mcpServers": {}}
        
        for name, info in self.all_servers.items():
            if info.config_example:
                config["mcpServers"][name] = info.config_example
            elif info.npm_package:
                config["mcpServers"][name] = {
                    "command": "npx",
                    "args": ["-y", info.npm_package],
                }
        
        return config
    
    def save_index(self) -> Path:
        """保存索引文件"""
        if self.dry_run:
            return self.target_dir / "mcp_index.json"
        
        self.target_dir.mkdir(parents=True, exist_ok=True)
        
        # 主索引
        index = self.generate_index()
        index_path = self.target_dir / "mcp_index.json"
        index_path.write_text(
            json.dumps(index, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        
        # Claude 配置片段
        config = self.generate_claude_config()
        config_path = self.target_dir / "claude_mcp_config.json"
        config_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        
        # 按类别生成单独文件
        categories_dir = self.target_dir / "categories"
        categories_dir.mkdir(exist_ok=True)
        
        for cat, cat_data in index["categories"].items():
            cat_servers = {
                name: index["servers"][name]
                for name in cat_data["servers"]
            }
            cat_file = categories_dir / f"{cat}.json"
            cat_file.write_text(
                json.dumps({
                    "category": cat,
                    "name": cat_data["name"],
                    "servers": cat_servers,
                }, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
        
        return index_path
    
    def print_summary(self) -> None:
        """打印同步摘要"""
        print("\n" + "=" * 60)
        print("同步摘要")
        print("=" * 60)
        print(f"  官方服务器: {self.stats['official']}")
        print(f"  社区服务器: {self.stats['community']}")
        print(f"  总计: {len(self.all_servers)}")
        print(f"  ---")
        print(f"  已下载: {self.stats['downloaded']}")


# =============================================================================
# List Functions
# =============================================================================

def list_all_servers() -> None:
    """列出所有可用 MCP 服务器"""
    print("正在获取 MCP 服务器列表...\n")
    
    # 获取社区列表
    readme = fetch_text(AWESOME_README_URL)
    if not readme:
        print("无法获取服务器列表")
        return
    
    parser = AwesomeMCPParser(readme)
    servers = parser.parse()
    
    # 添加官方服务器
    for name in OFFICIAL_SERVERS:
        servers[name] = MCPServerInfo(
            name=name,
            repo=OFFICIAL_REPO,
            category="official",
            official=True,
        )
    
    # 按类别分组
    by_category: Dict[str, List[str]] = {}
    for name, info in servers.items():
        cat = info.category
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(name)
    
    print(f"共 {len(servers)} 个 MCP 服务器:\n")
    
    # 先显示官方
    if "official" in by_category:
        print("[official] 官方参考实现")
        for name in sorted(by_category["official"]):
            print(f"  · {name}")
        print()
        del by_category["official"]
    
    # 显示其他类别
    for cat in sorted(by_category.keys()):
        cat_name = MCP_CATEGORIES.get(cat, cat)
        server_list = by_category[cat]
        print(f"[{cat}] {cat_name} ({len(server_list)})")
        for name in sorted(server_list)[:20]:  # 每类最多显示20个
            info = servers[name]
            desc = info.description[:50] + "..." if len(info.description) > 50 else info.description
            print(f"  · {name}" + (f" - {desc}" if desc else ""))
        if len(server_list) > 20:
            print(f"  ... 还有 {len(server_list) - 20} 个")
        print()


def list_categories() -> None:
    """列出所有类别"""
    print("MCP 服务器类别:\n")
    for cat_id, cat_name in sorted(MCP_CATEGORIES.items()):
        print(f"  {cat_id}: {cat_name}")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="一键同步 MCP 服务器到本地",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python sync_mcp_servers.py                              # 同步所有
  python sync_mcp_servers.py --target-dir .claude/mcps
  python sync_mcp_servers.py --official-only              # 仅官方
  python sync_mcp_servers.py --category database,search   # 指定类别
  python sync_mcp_servers.py --list-servers               # 列出所有服务器
  python sync_mcp_servers.py --list-categories            # 列出所有类别

类别:
  ai-platforms, browser-automation, cloud-platforms, code-execution,
  communication, database, developer-tools, file-systems, finance-fintech,
  gaming, knowledge-memory, location-travel, marketing, media-content,
  monitoring, productivity, search, security, version-control, web-scraping
"""
    )
    
    parser.add_argument(
        "--target-dir", "-t",
        default=".claude/mcps",
        help="目标目录 (default: .claude/mcps)"
    )
    parser.add_argument(
        "--official-only",
        action="store_true",
        help="仅同步官方参考实现"
    )
    parser.add_argument(
        "--community-only",
        action="store_true",
        help="仅同步社区 MCP 索引"
    )
    parser.add_argument(
        "--category", "-c",
        help="筛选特定类别 (逗号分隔)"
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="强制覆盖已存在的文件"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="仅显示将要执行的操作"
    )
    parser.add_argument(
        "--list-servers",
        action="store_true",
        help="列出所有可用 MCP 服务器"
    )
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="列出所有类别"
    )
    
    args = parser.parse_args()
    
    # 列表模式
    if args.list_servers:
        list_all_servers()
        return 0
    
    if args.list_categories:
        list_categories()
        return 0
    
    # 同步模式
    categories = set(args.category.split(",")) if args.category else None
    
    syncer = MCPSyncer(
        target_dir=args.target_dir,
        official_only=args.official_only,
        community_only=args.community_only,
        categories=categories,
        force=args.force,
        dry_run=args.dry_run,
    )
    
    syncer.sync_all()
    syncer.print_summary()
    
    # 生成索引
    if not args.dry_run:
        print("\n[3/3] 生成索引文件...")
        index_path = syncer.save_index()
        print(f"  → {index_path}")
        print(f"  → {syncer.target_dir}/claude_mcp_config.json")
        print(f"  → {syncer.target_dir}/categories/")
    
    # 使用建议
    print("\n" + "=" * 60)
    print("使用建议")
    print("=" * 60)
    print(f"""
1. 查看索引:
   cat {args.target_dir}/mcp_index.json

2. 在 Claude Desktop 配置中使用:
   将 {args.target_dir}/claude_mcp_config.json 的内容
   合并到 ~/.config/claude/claude_desktop_config.json

3. 在 Playbook 中引用:
   mcps:
     - filesystem
     - memory
     - git

4. 查看特定类别:
   cat {args.target_dir}/categories/database.json
""")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
