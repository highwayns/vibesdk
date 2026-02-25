#!/usr/bin/env python3
"""
Playbook Parser and Executor for Claude Code — v3

This executor is designed to:
- Parse YAML / Markdown playbooks.
- Track step state in a run directory.
- Identify *all* runnable steps (to enable parallel execution with Claude Agent Teams).
- Evaluate step conditions safely (auto-skip when condition is definitively false).
- Support basic output validation (file existence / non-empty / directory not empty / simple mermaid checks).
- Generate prompts for:
  - A single step (serial execution)
  - A parallel "batch" (ready steps) to dispatch to an Agent Team

Notes:
- This executor does NOT "run" the steps itself; it helps you orchestrate work in Claude Code.
- To avoid merge conflicts during parallel work, this version defaults to per-step evidence/decision files:
    evidence/<step_id>.md
    decisions/<step_id>.md
  The final review step can merge these into EvidenceRegistry.md / DecisionLog.md if you still want global logs.

Changelog v2 → v3:
  [P0] Added --resume, --reset, --status, --step, --dry-run CLI options
  [P0] Fixed allow_blocked dead code in get_ready_steps()
  [P0] Added file-locking for state writes (parallel safety)
  [P1] Added variables, errors, playbook_file to PlaybookState
  [P1] Unified --max-parallel default to 4
  [P1] Added run directory initialization (subdirs + template stubs)
  [P2] Added events.jsonl event logging
  [P2] Hardened _safe_eval_expr (removed BinOp, added size/timeout guard)
  [P2] Replaced format_map with regex-based template rendering
  [P2] Added field filtering in PlaybookState.from_dict
  [P3] Added condition evaluation caching
  [P3] Enhanced Markdown parser (condition, validation, routing, custom IDs)
  [P3] Added playbook version compatibility check
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime
import fcntl          # [P0] file locking (Unix); see _FileLock fallback for Windows
import json
import re
import shutil
import signal         # [P2] eval timeout
import textwrap
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXECUTOR_VERSION = "3.0.0"
# [P3] Playbook schema versions this executor supports
SUPPORTED_PLAYBOOK_VERSIONS = {"1", "1.0", "1.1", "2", "2.0", "2.1", "3", "3.0", None}

# [P1] Default max-parallel aligned with SKILL (was 8, SKILL says 4)
DEFAULT_MAX_PARALLEL = 4

# [P2] Condition eval timeout in seconds
CONDITION_EVAL_TIMEOUT_SEC = 2

# [P1] Directories to create during init
RUN_SUBDIRS = [
    "inventory", "diagrams", "docs", "review", "artifacts",
    "evidence", "decisions",
]

# [P1] Template stub files to create if missing
TEMPLATE_STUBS: Dict[str, str] = {
    "RunSpec.md": "# Run Specification\n\n> Auto-generated stub. Fill in during intake step.\n",
    "EvidenceRegistry.md": textwrap.dedent("""\
        # Evidence Registry
        
        | ID | Statement | Source | Status | Impact | Notes |
        |----|-----------|--------|--------|--------|-------|
    """),
    "DecisionLog.md": textwrap.dedent("""\
        # Decision Log
        
        | Time | Decision/Question | Options | Chosen | Rationale | Evidence | Follow-up |
        |------|-------------------|---------|--------|-----------|----------|-----------|
    """),
    "review/report.md": "# Review Report\n\n> Compiled at end of playbook run.\n",
    "review/issues.md": "# Issues\n\n> Discovered issues listed here.\n",
    "deliverables.md": textwrap.dedent("""\
        # Deliverables
        
        ## Created/Updated Files
        
        ## Verification Steps
        
        ## Rollback Notes
        
        ## Recommended Next Actions
    """),
}


# ---------------------------------------------------------------------------
# [P0] Cross-platform file lock
# ---------------------------------------------------------------------------

class _FileLock:
    """
    Simple file lock using fcntl (Unix).
    On Windows, falls back to a no-op (or replace with msvcrt / filelock package).
    """

    def __init__(self, path: Path):
        self._path = path.with_suffix(path.suffix + ".lock")

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(self._path, "w")
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except (OSError, AttributeError):
            pass  # Windows fallback: no-op
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except (OSError, AttributeError):
            pass
        self._fd.close()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"  # waiting for dependency / condition resolution


@dataclass
class PlaybookStep:
    id: str
    goal: str
    inputs: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    exit_criteria: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    routing: Optional[str] = None
    condition: Optional[str] = None
    validation: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Playbook:
    name: str
    goal: str
    steps: List[PlaybookStep]
    source_file: str

    version: Optional[str] = None
    description: Optional[str] = None
    variables: Dict[str, Any] = field(default_factory=dict)
    constraints: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "goal": self.goal,
            "version": self.version,
            "description": self.description,
            "steps": [s.to_dict() for s in self.steps],
            "source_file": self.source_file,
            "variables": self.variables,
            "constraints": self.constraints,
        }


@dataclass
class PlaybookState:
    run_id: str
    playbook_name: str
    current_step: Optional[str] = None
    steps: Dict[str, dict] = field(default_factory=dict)
    started_at: Optional[str] = None
    updated_at: Optional[str] = None
    completed_at: Optional[str] = None
    overall_status: str = "pending"
    # --- [P1] New fields ---
    playbook_file: Optional[str] = None
    variables: Dict[str, Any] = field(default_factory=dict)
    errors: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PlaybookState":
        # [P2] Filter unknown keys to prevent TypeError on schema evolution
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    # --- state helpers ---

    @staticmethod
    def _now() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _dep_done(status: str) -> bool:
        return status in (StepStatus.COMPLETED.value, StepStatus.SKIPPED.value)

    def add_error(self, step_id: Optional[str], message: str):
        """[P1] Record a structured error."""
        self.errors.append({
            "at": self._now(),
            "step_id": step_id,
            "message": message,
        })

    def update_step(self, step_id: str, status: StepStatus,
                    outputs: Optional[List[str]] = None,
                    note: Optional[str] = None):
        now = self._now()
        if step_id not in self.steps:
            self.steps[step_id] = {
                "status": StepStatus.PENDING.value,
                "started_at": None,
                "completed_at": None,
                "outputs": [],
                "evidence": [],
                "notes": [],
            }

        self.steps[step_id]["status"] = status.value
        self.updated_at = now

        if note:
            self.steps[step_id].setdefault("notes", []).append({"at": now, "note": note})

        if status == StepStatus.RUNNING:
            self.steps[step_id]["started_at"] = now
            self.current_step = step_id
            self.overall_status = "running"
        elif status in (StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED):
            self.steps[step_id]["completed_at"] = now
            if outputs:
                self.steps[step_id].setdefault("outputs", []).extend(outputs)

    def deps_satisfied(self, step: PlaybookStep) -> bool:
        for dep_id in step.depends_on:
            dep_state = self.steps.get(dep_id, {})
            dep_status = dep_state.get("status", StepStatus.PENDING.value)
            if not self._dep_done(dep_status):
                return False
        return True

    def is_complete(self, playbook: Playbook) -> bool:
        for step in playbook.steps:
            status = self.steps.get(step.id, {}).get("status", StepStatus.PENDING.value)
            if status in (StepStatus.PENDING.value, StepStatus.RUNNING.value, StepStatus.BLOCKED.value):
                return False
        return True

    def get_next_step(self, playbook: Playbook, *, allow_blocked: bool = False) -> Optional[PlaybookStep]:
        ready = self.get_ready_steps(playbook, allow_blocked=allow_blocked)
        return ready[0] if ready else None

    def get_ready_steps(self, playbook: Playbook, *, allow_blocked: bool = False, max_parallel: int = 99) -> List[PlaybookStep]:
        """
        Return all steps that are runnable now.

        [P0] Fixed: allow_blocked now correctly handles BLOCKED status.
        Previously BLOCKED was filtered by `status != PENDING` before the
        allow_blocked check could ever fire, and both branches did the same
        thing (append).
        """
        ready: List[PlaybookStep] = []
        for step in playbook.steps:
            st = self.steps.get(step.id, {})
            status = st.get("status", StepStatus.PENDING.value)

            # [P0] Handle BLOCKED separately so allow_blocked works
            if status == StepStatus.BLOCKED.value:
                if allow_blocked and self.deps_satisfied(step):
                    ready.append(step)
                continue

            if status != StepStatus.PENDING.value:
                continue
            if not self.deps_satisfied(step):
                continue

            ready.append(step)

            if len(ready) >= max_parallel:
                break

        return ready


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class PlaybookParser:
    """Parse playbooks from YAML or Markdown format."""

    @classmethod
    def parse(cls, filepath: Path) -> Playbook:
        content = filepath.read_text(encoding="utf-8")
        if filepath.suffix in (".yml", ".yaml"):
            pb = cls._parse_yaml(content, str(filepath))
        elif filepath.suffix == ".md":
            pb = cls._parse_markdown(content, str(filepath))
        else:
            raise ValueError(f"Unsupported playbook format: {filepath.suffix}")

        # [P3] Version compatibility check
        cls._check_version(pb)
        return pb

    @classmethod
    def _check_version(cls, pb: Playbook):
        """[P3] Warn if playbook version is not in supported set."""
        if pb.version is not None and pb.version not in SUPPORTED_PLAYBOOK_VERSIONS:
            import sys
            print(
                f"WARNING: Playbook version '{pb.version}' is not in the supported set "
                f"{SUPPORTED_PLAYBOOK_VERSIONS}. Executor v{EXECUTOR_VERSION} may produce "
                f"incorrect results. Consider upgrading the executor or downgrading the playbook.",
                file=sys.stderr,
            )

    @classmethod
    def _parse_yaml(cls, content: str, source: str) -> Playbook:
        # [P3] Guard against oversized YAML (billion laughs etc.)
        MAX_YAML_SIZE = 10 * 1024 * 1024  # 10 MB
        if len(content) > MAX_YAML_SIZE:
            raise ValueError(f"Playbook YAML exceeds {MAX_YAML_SIZE} bytes — refusing to parse.")

        data = yaml.safe_load(content) or {}

        steps: List[PlaybookStep] = []
        for s in data.get("steps", []) or []:
            step = PlaybookStep(
                id=s.get("id", f"step_{len(steps)}"),
                goal=s.get("goal", ""),
                inputs=s.get("inputs", []) or [],
                actions=s.get("actions", []) or [],
                outputs=s.get("outputs", []) or [],
                exit_criteria=s.get("exit_criteria", []) or [],
                depends_on=s.get("depends_on", []) or [],
                routing=s.get("routing"),
                condition=s.get("condition"),
                validation=s.get("validation", []) or [],
                meta={k: v for k, v in (s.items() if isinstance(s, dict) else []) if k not in {
                    "id", "goal", "inputs", "actions", "outputs", "exit_criteria",
                    "depends_on", "routing", "condition", "validation",
                }},
            )
            steps.append(step)

        return Playbook(
            name=data.get("name", "unnamed"),
            goal=data.get("goal", ""),
            version=str(data.get("version")) if data.get("version") is not None else None,
            description=data.get("description"),
            steps=steps,
            source_file=source,
            variables=data.get("variables", {}) or {},
            constraints=data.get("constraints", {}) or {},
        )

    @classmethod
    def _parse_markdown(cls, content: str, source: str) -> Playbook:
        """
        Parse markdown playbook.

        [P3] Enhanced: now supports condition, validation, routing, custom IDs,
        and variables/constraints sections.
        """
        lines = content.split("\n")

        name = "unnamed"
        goal = ""
        variables: Dict[str, Any] = {}
        constraints: Dict[str, Any] = {}
        steps: List[PlaybookStep] = []
        current_step: Optional[PlaybookStep] = None
        current_section: Optional[str] = None

        # Extract name from first H1
        for line in lines:
            if line.startswith("# "):
                name = line[2:].strip()
                break

        # Extract goal from ## Purpose or ## Goal
        in_purpose = False
        for line in lines:
            if re.match(r"^##\s+(Purpose|Goal)", line, re.IGNORECASE):
                in_purpose = True
                continue
            if in_purpose:
                if line.startswith("##"):
                    break
                if line.strip():
                    goal = line.strip()
                    break

        # [P3] Extract variables from ## Variables section
        in_vars = False
        for line in lines:
            if re.match(r"^##\s+Variables", line, re.IGNORECASE):
                in_vars = True
                continue
            if in_vars:
                if line.startswith("##"):
                    break
                kv_match = re.match(r"^\s*[-*]\s+(\w+)\s*[:=]\s*(.*)", line)
                if kv_match:
                    k, v = kv_match.group(1).strip(), kv_match.group(2).strip()
                    try:
                        variables[k] = json.loads(v)
                    except Exception:
                        variables[k] = v

        # Parse steps — [P3] support "## Step: <id> — <goal>" and "## Step N: <goal>"
        for line in lines:
            # Pattern 1: ## Step N: goal
            step_match = re.match(r"^##\s+Step\s+(\d+)\s*:\s*(.*)", line, re.IGNORECASE)
            # Pattern 2: ## Step: custom_id — goal  (or — or -)
            step_match2 = re.match(r"^##\s+Step\s*:\s*(\S+)\s*[—–-]\s*(.*)", line, re.IGNORECASE)

            if step_match or step_match2:
                if current_step:
                    steps.append(current_step)
                if step_match:
                    step_id = f"step_{step_match.group(1)}"
                    step_goal = step_match.group(2).strip()
                else:
                    step_id = step_match2.group(1).strip()  # type: ignore[union-attr]
                    step_goal = step_match2.group(2).strip()  # type: ignore[union-attr]
                current_step = PlaybookStep(id=step_id, goal=step_goal)
                current_section = None
                continue

            if current_step:
                # [P3] Extended section keywords
                section_match = re.match(
                    r"^\*\*(Inputs|Actions|Outputs|Exit Criteria|Depends On|"
                    r"Condition|Routing|Validation)\*\*",
                    line, re.IGNORECASE,
                )
                if section_match:
                    current_section = section_match.group(1).lower()
                    # [P3] Inline value for single-value sections
                    rest = line[section_match.end():].strip().lstrip(":").strip()
                    if rest and current_section == "condition":
                        current_step.condition = rest
                        current_section = None
                    elif rest and current_section == "routing":
                        current_step.routing = rest
                        current_section = None
                    continue

                bullet_match = re.match(r"^\s*[-*]\s+(.*)", line)
                if bullet_match and current_section:
                    item = bullet_match.group(1).strip()
                    if current_section == "inputs":
                        current_step.inputs.append(item)
                    elif current_section == "actions":
                        current_step.actions.append(item)
                    elif current_section == "outputs":
                        current_step.outputs.append(item)
                    elif current_section == "exit criteria":
                        current_step.exit_criteria.append(item)
                    elif current_section == "depends on":
                        current_step.depends_on.append(item)
                    elif current_section == "validation":
                        # [P3] Try to parse "kind: value" format
                        vm = re.match(r"^(\w+)\s*:\s*(.*)", item)
                        if vm:
                            current_step.validation.append({vm.group(1): vm.group(2).strip()})
                        else:
                            current_step.validation.append({"manual": item})

        if current_step:
            steps.append(current_step)

        return Playbook(
            name=name,
            goal=goal,
            steps=steps,
            source_file=source,
            variables=variables,
            constraints=constraints,
        )


# ---------------------------------------------------------------------------
# Condition evaluation (safe)
# ---------------------------------------------------------------------------

# [P2] Removed ast.BinOp to prevent 2**9999999 style DoS
_ALLOWED_AST_NODES = (
    ast.Expression, ast.BoolOp, ast.UnaryOp, ast.Compare,
    ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple, ast.Set,
    ast.And, ast.Or, ast.Not,
    ast.Eq, ast.NotEq, ast.In, ast.NotIn,
)

# [P2] Max length for condition expression string
_MAX_CONDITION_LEN = 500


class _EvalTimeout(Exception):
    pass


def _timeout_handler(signum, frame):
    raise _EvalTimeout("condition evaluation timed out")


def _safe_eval_expr(expr: str, env: Dict[str, Any]) -> Tuple[Optional[bool], Optional[str]]:
    """
    Safely evaluate a limited boolean expression used by playbook conditions.

    [P2] Hardened:
    - Removed ast.BinOp (no arithmetic)
    - Added expression length limit
    - Added signal-based timeout (Unix)
    - Validates literal sizes
    """
    if len(expr) > _MAX_CONDITION_LEN:
        return None, f"condition_too_long ({len(expr)} > {_MAX_CONDITION_LEN})"

    try:
        tree = ast.parse(expr, mode="eval")
    except Exception as e:
        return None, f"parse_error: {e}"

    # Validate allowed nodes + literal size
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_AST_NODES):
            return None, f"disallowed_ast_node: {type(node).__name__}"
        # [P2] Guard against huge literals
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str) and len(node.value) > 10000:
                return None, "string_literal_too_large"
            if isinstance(node.value, (int, float)) and abs(node.value) > 1e15:
                return None, "numeric_literal_too_large"

    # [P2] Timeout guard (Unix only; silently skip on Windows)
    old_handler = None
    try:
        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(CONDITION_EVAL_TIMEOUT_SEC)
    except (OSError, AttributeError, ValueError):
        pass  # Windows or non-main-thread

    try:
        value = eval(compile(tree, "<condition>", "eval"), {"__builtins__": {}}, dict(env))
    except _EvalTimeout:
        return None, "eval_timeout"
    except Exception as e:
        return None, f"eval_error: {e}"
    finally:
        try:
            signal.alarm(0)
            if old_handler is not None:
                signal.signal(signal.SIGALRM, old_handler)
        except (OSError, AttributeError, ValueError):
            pass

    if isinstance(value, bool):
        return value, None
    return None, "condition_not_boolean"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _render_template(s: str, ctx: Dict[str, Any]) -> str:
    """
    Render placeholders like {run_dir} / {project_name}.

    [P2] Replaced format_map with regex to prevent attribute access injection
    (e.g. {0.__class__.__subclasses__()}).
    """
    if not isinstance(s, str):
        return str(s)

    def _repl(m: re.Match) -> str:
        key = m.group(1)
        if key in ctx:
            return str(ctx[key])
        return m.group(0)  # leave as-is

    return re.sub(r"\{(\w+)\}", _repl, s)


def _is_mermaid_like(text: str) -> bool:
    head = (text or "").lstrip()
    return head.startswith((
        "flowchart", "sequenceDiagram", "classDiagram",
        "stateDiagram", "erDiagram", "gantt", "mindmap", "journey",
    ))


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class PlaybookExecutor:
    """Manages playbook execution state and progress."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.state_file = run_dir / "playbook_state.json"
        self.playbook_file = run_dir / "playbook_parsed.json"
        self.events_file = run_dir / "events.jsonl"  # [P2]

        # per-step evidence/decision (parallel-safe)
        self.evidence_dir = run_dir / "evidence"
        self.decisions_dir = run_dir / "decisions"

        # [P3] Condition evaluation cache (per executor instance)
        self._condition_cache: Dict[str, Tuple[Optional[bool], Optional[str]]] = {}

    # -------------------------
    # [P1] Run directory init
    # -------------------------

    def init_run_dir(self):
        """
        [P1] Create run subdirectories and template stubs.
        Called once during load_or_create_state when creating fresh state.
        """
        for subdir in RUN_SUBDIRS:
            (self.run_dir / subdir).mkdir(parents=True, exist_ok=True)

        for rel_path, content in TEMPLATE_STUBS.items():
            target = self.run_dir / rel_path
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

    # -------------------------
    # State persistence
    # -------------------------

    def load_or_create_state(self, playbook: Playbook, run_id: str,
                             *, extra_vars: Optional[Dict[str, Any]] = None) -> PlaybookState:
        # [P0] Use file lock for all state reads/writes
        with _FileLock(self.state_file):
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                state = PlaybookState.from_dict(data)
                # [P1] Merge extra_vars into persisted variables
                if extra_vars:
                    state.variables.update(extra_vars)
                return state

            now = PlaybookState._now()
            # [P1] Populate new fields
            merged_vars = dict(playbook.variables or {})
            if extra_vars:
                merged_vars.update(extra_vars)

            state = PlaybookState(
                run_id=run_id,
                playbook_name=playbook.name,
                playbook_file=playbook.source_file,
                variables=merged_vars,
                started_at=now,
                updated_at=now,
            )

            for step in playbook.steps:
                state.steps[step.id] = {
                    "status": StepStatus.PENDING.value,
                    "started_at": None,
                    "completed_at": None,
                    "outputs": [],
                    "evidence": [],
                    "notes": [],
                }

            # [P1] Initialize run directory structure
            self.init_run_dir()

            self._save_state_unlocked(state)
            return state

    def save_state(self, state: PlaybookState):
        """Thread/process-safe state save."""
        with _FileLock(self.state_file):
            self._save_state_unlocked(state)

    def _save_state_unlocked(self, state: PlaybookState):
        """Write state without acquiring lock (caller must hold lock)."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        state.updated_at = PlaybookState._now()
        self.state_file.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_playbook(self, playbook: Playbook):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.playbook_file.write_text(
            json.dumps(playbook.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # -------------------------
    # [P0] Reset state
    # -------------------------

    def reset_state(self, playbook: Playbook, run_id: str,
                    *, extra_vars: Optional[Dict[str, Any]] = None) -> PlaybookState:
        """
        [P0] Backup old state and create fresh state.
        """
        with _FileLock(self.state_file):
            if self.state_file.exists():
                backup = self.state_file.with_name(
                    f"playbook_state.backup.{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
                )
                shutil.copy2(self.state_file, backup)
                self.state_file.unlink()
                self.append_event("state_reset", {"backup": str(backup)})

        # Delegate to load_or_create (will create fresh)
        return self.load_or_create_state(playbook, run_id, extra_vars=extra_vars)

    # -------------------------
    # [P2] Event logging
    # -------------------------

    def append_event(self, event_type: str, data: Optional[Dict[str, Any]] = None):
        """
        [P2] Append a structured event to events.jsonl.
        Each line is a JSON object with timestamp, type, and data.
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        event = {
            "ts": PlaybookState._now(),
            "type": event_type,
            "data": data or {},
        }
        with open(self.events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    # -------------------------
    # Readiness / skipping logic
    # -------------------------

    def build_runtime_vars(self, playbook: Playbook, state: Optional[PlaybookState] = None,
                           extra_vars: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        v = dict(playbook.variables or {})
        # [P1] Merge persisted state variables
        if state and state.variables:
            v.update(state.variables)
        v["run_dir"] = str(self.run_dir)
        if extra_vars:
            v.update(extra_vars)
        return v

    def _eval_condition_cached(self, condition: str, ctx: Dict[str, Any]) -> Tuple[Optional[bool], Optional[str]]:
        """[P3] Cache condition evaluations within a single executor call."""
        # Build a stable cache key from condition + relevant ctx values
        # We use the condition text itself; ctx changes invalidate across runs
        cache_key = condition.strip()
        if cache_key in self._condition_cache:
            return self._condition_cache[cache_key]
        result = _safe_eval_expr(cache_key, ctx)
        self._condition_cache[cache_key] = result
        return result

    def apply_auto_skips(self, state: PlaybookState, playbook: Playbook,
                         *, extra_vars: Optional[Dict[str, Any]] = None) -> int:
        ctx = self.build_runtime_vars(playbook, state=state, extra_vars=extra_vars)
        skipped = 0

        for step in playbook.steps:
            st = state.steps.get(step.id, {})
            status = st.get("status", StepStatus.PENDING.value)
            if status != StepStatus.PENDING.value:
                continue
            if not state.deps_satisfied(step):
                continue
            if not step.condition:
                continue

            cond = step.condition.strip()
            value, err = self._eval_condition_cached(cond, ctx)
            if value is False:
                state.update_step(step.id, StepStatus.SKIPPED,
                                  note=f"auto-skipped: condition evaluated False ({cond})")
                self.append_event("step_skipped", {"step_id": step.id, "reason": f"condition false: {cond}"})
                skipped += 1
            elif value is None:
                state.steps[step.id].setdefault("notes", []).append({
                    "at": PlaybookState._now(),
                    "note": f"condition unresolved: {cond} ({err})",
                })

        if skipped:
            self.save_state(state)
        return skipped

    def get_ready_steps(self, state: PlaybookState, playbook: Playbook,
                        *, extra_vars: Optional[Dict[str, Any]] = None,
                        max_parallel: int = DEFAULT_MAX_PARALLEL) -> List[PlaybookStep]:
        self.apply_auto_skips(state, playbook, extra_vars=extra_vars)
        return state.get_ready_steps(playbook, max_parallel=max_parallel)

    # -------------------------
    # Validation
    # -------------------------

    def validate_step(self, step: PlaybookStep, playbook: Playbook,
                      state: Optional[PlaybookState] = None,
                      *, extra_vars: Optional[Dict[str, Any]] = None) -> Tuple[bool, List[str]]:
        ctx = self.build_runtime_vars(playbook, state=state, extra_vars=extra_vars)
        messages: List[str] = []
        ok = True

        for rule in step.validation or []:
            if not isinstance(rule, dict) or not rule:
                continue
            if len(rule) != 1:
                messages.append(f"WARNING: invalid validation rule (expected single key): {rule}")
                ok = False
                continue

            kind, raw = next(iter(rule.items()))
            target = _render_template(str(raw), ctx)
            p = Path(target)

            if kind == "file_exists":
                if not p.exists():
                    ok = False
                    messages.append(f"FAIL file_exists: {target}")
                else:
                    messages.append(f"OK file_exists: {target}")

            elif kind == "file_not_empty":
                if not p.exists() or p.stat().st_size == 0:
                    ok = False
                    messages.append(f"FAIL file_not_empty: {target}")
                else:
                    messages.append(f"OK file_not_empty: {target}")

            elif kind == "directory_not_empty":
                if not p.exists() or not p.is_dir():
                    ok = False
                    messages.append(f"FAIL directory_not_empty (missing dir): {target}")
                else:
                    has_any = any(p.iterdir())
                    if not has_any:
                        ok = False
                        messages.append(f"FAIL directory_not_empty (empty): {target}")
                    else:
                        messages.append(f"OK directory_not_empty: {target}")

            elif kind == "mermaid_valid":
                if not p.exists() or p.stat().st_size == 0:
                    ok = False
                    messages.append(f"FAIL mermaid_valid (missing/empty): {target}")
                else:
                    file_content = p.read_text(encoding="utf-8", errors="ignore")
                    if not _is_mermaid_like(file_content):
                        ok = False
                        messages.append(f"FAIL mermaid_valid (unrecognized header): {target}")
                    else:
                        messages.append(f"OK mermaid_valid: {target}")

            elif kind == "manual":
                # [P2] Unsupported validation — record as 要確認
                messages.append(f"MANUAL (要確認): {target}")

            else:
                ok = False
                messages.append(f"FAIL unknown_validation_rule: {kind} -> {target}")

        return ok, messages

    # -------------------------
    # Prompts
    # -------------------------

    def _constraint_lines(self, playbook: Playbook) -> List[str]:
        c = playbook.constraints or {}
        lines: List[str] = []
        if not c:
            return lines
        lines.append("**Quality Constraints**:")
        for k, v in c.items():
            lines.append(f"- {k}: {v}")
        lines.append("")
        return lines

    def format_step_prompt(self, state: PlaybookState, step: PlaybookStep,
                           playbook: Playbook,
                           *, extra_vars: Optional[Dict[str, Any]] = None) -> str:
        ctx = self.build_runtime_vars(playbook, state=state, extra_vars=extra_vars)

        # [P3] Use cached condition evaluation
        cond_line: List[str] = []
        if step.condition:
            val, err = self._eval_condition_cached(step.condition.strip(), ctx)
            if val is True:
                cond_line.append(f"**Condition**: `{step.condition}` ✅")
            elif val is False:
                cond_line.append(f"**Condition**: `{step.condition}` ❌ (should be skipped)")
            else:
                cond_line.append(f"**Condition**: `{step.condition}` ⚠️ (unresolved: {err}) — confirm manually")

        ev_path = self.evidence_dir / f"{step.id}.md"
        dc_path = self.decisions_dir / f"{step.id}.md"

        lines: List[str] = [
            f"## Executing Step: {step.id}",
            f"**Goal**: {step.goal}",
            "",
        ]

        if step.routing:
            lines.append(f"**Suggested Role (routing)**: `{step.routing}`")
            lines.append("")

        lines.extend(self._constraint_lines(playbook))

        if cond_line:
            lines.extend(cond_line)
            lines.append("")

        if step.inputs:
            lines.append("**Inputs**:")
            for inp in step.inputs:
                lines.append(f"- {_render_template(inp, ctx)}")
            lines.append("")

        if step.actions:
            lines.append("**Actions**:")
            for act in step.actions:
                lines.append(f"- {_render_template(act, ctx)}")
            lines.append("")

        if step.outputs:
            lines.append("**Expected Outputs**:")
            for out in step.outputs:
                lines.append(f"- {_render_template(out, ctx)}")
            lines.append("")

        if step.exit_criteria:
            lines.append("**Exit Criteria** (verify before marking complete):")
            for crit in step.exit_criteria:
                lines.append(f"- [ ] {_render_template(crit, ctx)}")
            lines.append("")

        if step.validation:
            lines.append("**Validation Checks** (recommended before completion):")
            for rule in step.validation:
                if isinstance(rule, dict) and len(rule) == 1:
                    kind, raw = next(iter(rule.items()))
                    lines.append(f"- [ ] {kind}: `{_render_template(str(raw), ctx)}`")
                else:
                    lines.append(f"- [ ] {rule}")
            lines.append("")

        lines.extend([
            "---",
            "After completing this step (parallel-safe logging):",
            f"1. Write facts/evidence to `{ev_path.relative_to(self.run_dir)}` (create if missing).",
            f"2. Write decisions/unknowns to `{dc_path.relative_to(self.run_dir)}` (mark unknown items as '要確認'; do not guess).",
            "3. If you *must* update global logs (EvidenceRegistry.md / DecisionLog.md), do it ONLY in serial mode or in the final review step to avoid conflicts.",
            "4. Confirm validations and exit criteria before marking the step complete.",
        ])

        return "\n".join(lines)

    def format_parallel_batch_prompt(self, state: PlaybookState,
                                     ready_steps: List[PlaybookStep],
                                     playbook: Playbook,
                                     *, extra_vars: Optional[Dict[str, Any]] = None) -> str:
        ctx = self.build_runtime_vars(playbook, state=state, extra_vars=extra_vars)

        if not ready_steps:
            return "No runnable steps at the moment (check dependencies/conditions)."

        by_role: Dict[str, List[PlaybookStep]] = {}
        for s in ready_steps:
            role = s.routing or "generalist"
            by_role.setdefault(role, []).append(s)

        lines: List[str] = [
            "## Agent Teams Dispatch (Parallel Batch)",
            f"**Playbook**: {playbook.name}",
            f"**Run dir**: {self.run_dir}",
            "",
            "Create an agent team and execute the following steps in parallel where possible.",
            "Rules:",
            "- Avoid editing the same files across teammates.",
            "- Each teammate must only write the outputs listed for their assigned step(s).",
            "- Use per-step logs to avoid conflicts:",
            f"  - evidence: `evidence/<step_id>.md` under `{self.run_dir}`",
            f"  - decisions: `decisions/<step_id>.md` under `{self.run_dir}`",
            "- If a condition is unclear, record it as a question in the step's decisions file instead of guessing.",
            "",
        ]

        lines.extend(self._constraint_lines(playbook))

        lines.append("### Ready Steps")
        for s in ready_steps:
            cond = ""
            if s.condition:
                val, err = self._eval_condition_cached(s.condition.strip(), ctx)
                if val is True:
                    cond = " (condition ✅)"
                elif val is None:
                    cond = f" (condition ⚠️ unresolved: {err})"
            lines.append(f"- **{s.id}**{cond}: {s.goal}  | routing: `{s.routing or 'generalist'}`")
        lines.append("")

        lines.append("### Suggested Assignment")
        for role, items in by_role.items():
            ids = ", ".join(i.id for i in items)
            lines.append(f"- `{role}` -> {ids}")
        lines.append("")

        lines.append("### Execution Template (tell each teammate)")
        lines.append("For each assigned step:")
        lines.append("1) Open the step prompt and follow actions/outputs/criteria.")
        lines.append("2) Write evidence to `evidence/<step_id>.md` and decisions to `decisions/<step_id>.md`.")
        lines.append("3) Run validations (if specified) and report PASS/FAIL with file paths.")
        lines.append("4) Report completion back to the lead with links to outputs.")

        return "\n".join(lines)

    # -------------------------
    # Progress / summary
    # -------------------------

    def get_progress_summary(self, state: PlaybookState, playbook: Playbook) -> str:
        total = len(playbook.steps)
        completed = sum(1 for s in state.steps.values() if s.get("status") == StepStatus.COMPLETED.value)
        failed = sum(1 for s in state.steps.values() if s.get("status") == StepStatus.FAILED.value)
        skipped = sum(1 for s in state.steps.values() if s.get("status") == StepStatus.SKIPPED.value)
        running = sum(1 for s in state.steps.values() if s.get("status") == StepStatus.RUNNING.value)
        blocked = sum(1 for s in state.steps.values() if s.get("status") == StepStatus.BLOCKED.value)

        pct = int(100 * (completed + skipped) / total) if total else 0

        lines = [
            f"## Playbook Progress: {playbook.name}",
            f"Run ID: {state.run_id}",
            f"Started: {state.started_at or 'n/a'}",
            f"Status: {state.overall_status}",
            "",
            f"- Overall: {completed}/{total} completed | {skipped} skipped | {failed} failed | {running} running | {blocked} blocked",
            f"- Progress: {completed + skipped}/{total} steps done ({pct}%)",
            f"- Run dir: {self.run_dir}",
            f"- Evidence: per-step files in evidence/",
            f"- Decisions: per-step files in decisions/",
            "",
        ]

        for step in playbook.steps:
            step_state = state.steps.get(step.id, {})
            status = step_state.get("status", StepStatus.PENDING.value)
            icon = {
                StepStatus.COMPLETED.value: "✅",
                StepStatus.RUNNING.value: "🔄",
                StepStatus.FAILED.value: "❌",
                StepStatus.SKIPPED.value: "⏭️",
                StepStatus.BLOCKED.value: "🔒",
                StepStatus.PENDING.value: "⬜",
            }.get(status, "❓")
            suffix = ""
            if status == StepStatus.RUNNING.value:
                suffix = "  ← CURRENT"
            lines.append(f"{icon} **{step.id}** — {step.goal}{suffix}")

        if state.errors:
            lines.append("")
            lines.append("### Errors")
            for err in state.errors[-5:]:  # last 5 errors
                lines.append(f"- [{err.get('at', '?')}] step={err.get('step_id', '?')}: {err.get('message', '?')}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_kv_list(kv_list: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for kv in kv_list:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        k = k.strip()
        v = v.strip()
        try:
            out[k] = json.loads(v)
        except Exception:
            out[k] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Playbook executor v3 (parallel-ready) for Claude Code")
    ap.add_argument("playbook_file", type=str, help="Path to playbook .yml/.yaml or .md")
    ap.add_argument("--run-dir", type=str, default=".claude/runs/test",
                    help="Run directory to store state/artifacts")
    ap.add_argument("--run-id", type=str, default="test-run", help="Run id (stored in state)")
    ap.add_argument("--vars", nargs="*", default=[],
                    help="Extra vars as key=value (value can be JSON)")

    # --- [P0] All modes declared by SKILL ---
    ap.add_argument("--summary", action="store_true", help="Print progress summary")
    ap.add_argument("--status", action="store_true",
                    help="[P0] Alias for --summary (SKILL-compatible)")
    ap.add_argument("--resume", action="store_true",
                    help="[P0] Continue from last checkpoint (default if state exists)")
    ap.add_argument("--reset", action="store_true",
                    help="[P0] Start fresh, clearing previous state (backup old state first)")
    ap.add_argument("--step", type=str, default="",
                    help="[P0] Jump to a specific step id")
    ap.add_argument("--dry-run", action="store_true",
                    help="[P0] Parse and validate playbook syntax only (no execution)")
    ap.add_argument("--next", dest="do_next", action="store_true",
                    help="Print next runnable step prompt (serial)")
    ap.add_argument("--ready", action="store_true",
                    help="Print ready steps and agent-teams dispatch prompt (parallel)")
    ap.add_argument("--max-parallel", type=int, default=DEFAULT_MAX_PARALLEL,
                    help=f"Max parallel steps to output for --ready (default: {DEFAULT_MAX_PARALLEL})")
    ap.add_argument("--validate", type=str, default="",
                    help="Validate a specific step id (runs step.validation checks)")
    args = ap.parse_args()

    playbook_path = Path(args.playbook_file)
    run_dir = Path(args.run_dir)
    extra_vars = _parse_kv_list(args.vars)

    # --- [P0] --dry-run: parse only ---
    if args.dry_run:
        try:
            playbook = PlaybookParser.parse(playbook_path)
        except Exception as e:
            print(f"PARSE ERROR: {e}")
            return 1
        print(f"Playbook '{playbook.name}' parsed successfully.")
        print(f"  Version: {playbook.version or 'unspecified'}")
        print(f"  Steps: {len(playbook.steps)}")
        for s in playbook.steps:
            deps = f" (depends: {', '.join(s.depends_on)})" if s.depends_on else ""
            cond = f" [condition: {s.condition}]" if s.condition else ""
            print(f"    - {s.id}: {s.goal}{deps}{cond}")
        print(f"  Variables: {list(playbook.variables.keys()) if playbook.variables else '(none)'}")
        print("DRY-RUN: OK")
        return 0

    playbook = PlaybookParser.parse(playbook_path)
    executor = PlaybookExecutor(run_dir)

    # --- [P0] --reset: backup and recreate ---
    if args.reset:
        state = executor.reset_state(playbook, args.run_id, extra_vars=extra_vars)
        print(f"State reset. Backup saved in {run_dir}/")
        executor.save_playbook(playbook)
        executor.append_event("playbook_loaded", {"file": str(playbook_path), "mode": "reset"})
        print(executor.get_progress_summary(state, playbook))
        return 0

    # Load or resume state
    state = executor.load_or_create_state(playbook, args.run_id, extra_vars=extra_vars)
    executor.save_playbook(playbook)
    executor.append_event("playbook_loaded", {"file": str(playbook_path), "mode": "resume" if args.resume else "auto"})

    # --- [P0] --status / --summary ---
    if args.status or args.summary or (
        not args.do_next and not args.ready and not args.validate and not args.step and not args.resume
    ):
        print(executor.get_progress_summary(state, playbook))
        if args.status or args.summary:
            return 0

    # --- [P0] --step <id>: jump to specific step ---
    if args.step:
        step_id = args.step.strip()
        step = next((s for s in playbook.steps if s.id == step_id), None)
        if not step:
            print(f"Unknown step id: {step_id}")
            return 2
        # Mark as running regardless of current status
        state.update_step(step_id, StepStatus.RUNNING, note="jumped via --step")
        executor.save_state(state)
        executor.append_event("step_jumped", {"step_id": step_id})
        print(executor.format_step_prompt(state, step, playbook, extra_vars=extra_vars))
        return 0

    # --- [P0] --resume: show current step or next ---
    if args.resume:
        if state.current_step:
            step = next((s for s in playbook.steps if s.id == state.current_step), None)
            if step:
                cur_status = state.steps.get(step.id, {}).get("status", "pending")
                if cur_status == StepStatus.RUNNING.value:
                    print(f"Resuming from current step: {step.id}")
                    print(executor.format_step_prompt(state, step, playbook, extra_vars=extra_vars))
                    return 0
        # Fall through to --next behavior
        args.do_next = True

    # --- --next ---
    if args.do_next:
        ready = executor.get_ready_steps(state, playbook, extra_vars=extra_vars, max_parallel=1)
        if not ready:
            if state.is_complete(playbook):
                print("All steps completed.")
                state.overall_status = "completed"
                state.completed_at = PlaybookState._now()
                executor.save_state(state)
                executor.append_event("playbook_completed", {})
            else:
                print("No runnable step (check dependencies/conditions).")
            return 0
        step = ready[0]
        state.update_step(step.id, StepStatus.RUNNING)
        executor.save_state(state)
        executor.append_event("step_started", {"step_id": step.id})
        print(executor.format_step_prompt(state, step, playbook, extra_vars=extra_vars))

    # --- --ready ---
    if args.ready:
        ready_steps = executor.get_ready_steps(state, playbook, extra_vars=extra_vars,
                                               max_parallel=args.max_parallel)
        executor.append_event("ready_computed", {"steps": [s.id for s in ready_steps]})
        print(executor.format_parallel_batch_prompt(state, ready_steps, playbook, extra_vars=extra_vars))
        print("")
        print("----")
        print("Step prompts (copy/paste per teammate):")
        for s in ready_steps:
            print("")
            print(executor.format_step_prompt(state, s, playbook, extra_vars=extra_vars))

    # --- --validate ---
    if args.validate:
        step_id = args.validate.strip()
        step = next((s for s in playbook.steps if s.id == step_id), None)
        if not step:
            print(f"Unknown step id: {step_id}")
            return 2
        ok, messages = executor.validate_step(step, playbook, state=state, extra_vars=extra_vars)
        print("\n".join(messages))
        if ok:
            print("VALIDATION: PASS")
            executor.append_event("validation_pass", {"step_id": step_id})
            return 0
        print("VALIDATION: FAIL")
        executor.append_event("validation_fail", {"step_id": step_id, "messages": messages})
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
