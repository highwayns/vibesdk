#!/usr/bin/env python3
"""
Playbook Parser and Executor for Claude Code

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
"""

from __future__ import annotations

import argparse
import ast
import datetime
import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


# -----------------------------
# Models
# -----------------------------

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
    condition: Optional[str] = None  # e.g., "system in doc_types or scope == 'all'"
    validation: List[Dict[str, Any]] = field(default_factory=list)  # e.g., [{"file_exists": "{run_dir}/foo.md"}]
    meta: Dict[str, Any] = field(default_factory=dict)  # optional extension point

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Playbook:
    name: str
    goal: str
    steps: List[PlaybookStep]
    source_file: str

    # extra top-level fields (optional)
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
    overall_status: str = "pending"  # pending/running/completed/failed

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PlaybookState":
        return cls(**data)

    # --- state helpers ---

    @staticmethod
    def _now() -> str:
        return datetime.datetime.utcnow().isoformat() + "Z"

    @staticmethod
    def _dep_done(status: str) -> bool:
        # Treat skipped as satisfied so optional branches don't block downstream review/delivery.
        return status in (StepStatus.COMPLETED.value, StepStatus.SKIPPED.value)

    def update_step(self, step_id: str, status: StepStatus, outputs: Optional[List[str]] = None, note: Optional[str] = None):
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
        """All steps are done (completed/failed/skipped)."""
        for step in playbook.steps:
            status = self.steps.get(step.id, {}).get("status", StepStatus.PENDING.value)
            if status in (StepStatus.PENDING.value, StepStatus.RUNNING.value, StepStatus.BLOCKED.value):
                return False
        return True

    def get_next_step(self, playbook: Playbook, *, allow_blocked: bool = False) -> Optional[PlaybookStep]:
        """
        Backwards-compatible: return the first runnable pending step.
        Prefer get_ready_steps() for parallel dispatch.
        """
        ready = self.get_ready_steps(playbook, allow_blocked=allow_blocked)
        return ready[0] if ready else None

    def get_ready_steps(self, playbook: Playbook, *, allow_blocked: bool = False, max_parallel: int = 99) -> List[PlaybookStep]:
        """
        Return all steps that are runnable now:
        - status == pending
        - dependencies satisfied (completed OR skipped)
        - condition is not definitively false (condition failures should have been auto-skipped by executor)
        """
        ready: List[PlaybookStep] = []
        for step in playbook.steps:
            st = self.steps.get(step.id, {})
            status = st.get("status", StepStatus.PENDING.value)
            if status != StepStatus.PENDING.value:
                continue
            if not self.deps_satisfied(step):
                continue

            # Condition handling is done in executor (so we can use variables/run_dir).
            # Here we optionally allow "blocked" placeholders (if user wants to resolve conditions manually).
            if allow_blocked and status == StepStatus.BLOCKED.value:
                ready.append(step)
            else:
                ready.append(step)

            if len(ready) >= max_parallel:
                break

        return ready


# -----------------------------
# Parsing
# -----------------------------

class PlaybookParser:
    """Parse playbooks from YAML or Markdown format."""

    @classmethod
    def parse(cls, filepath: Path) -> Playbook:
        content = filepath.read_text(encoding="utf-8")
        if filepath.suffix in (".yml", ".yaml"):
            return cls._parse_yaml(content, str(filepath))
        if filepath.suffix == ".md":
            return cls._parse_markdown(content, str(filepath))
        raise ValueError(f"Unsupported playbook format: {filepath.suffix}")

    @classmethod
    def _parse_yaml(cls, content: str, source: str) -> Playbook:
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
                    "id","goal","inputs","actions","outputs","exit_criteria","depends_on","routing","condition","validation"
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
        Parse markdown playbook with "## Step N" headers.
        (Kept for compatibility; does not support validation/conditions as richly as YAML.)
        """
        lines = content.split("\n")

        name = "unnamed"
        goal = ""
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

        # Parse steps
        for line in lines:
            step_match = re.match(r"^##\s+Step\s+(\d+)\s*:\s*(.*)", line, re.IGNORECASE)
            if step_match:
                if current_step:
                    steps.append(current_step)
                step_id = f"step_{step_match.group(1)}"
                current_step = PlaybookStep(id=step_id, goal=step_match.group(2).strip())
                current_section = None
                continue

            if current_step:
                section_match = re.match(r"^\*\*(Inputs|Actions|Outputs|Exit Criteria|Depends On)\*\*", line, re.IGNORECASE)
                if section_match:
                    current_section = section_match.group(1).lower()
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

        if current_step:
            steps.append(current_step)

        return Playbook(
            name=name,
            goal=goal,
            steps=steps,
            source_file=source,
        )


# -----------------------------
# Condition evaluation (safe)
# -----------------------------

_ALLOWED_AST_NODES = (
    ast.Expression, ast.BoolOp, ast.UnaryOp, ast.BinOp, ast.Compare,
    ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple, ast.Set,
    ast.And, ast.Or, ast.Not,
    ast.Eq, ast.NotEq, ast.In, ast.NotIn,
)

def _safe_eval_expr(expr: str, env: Dict[str, Any]) -> Tuple[Optional[bool], Optional[str]]:
    """
    Safely evaluate a limited boolean expression used by playbook conditions.

    Returns: (value, error)
      - value: True/False if evaluable, None if not evaluable
      - error: error message if any
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except Exception as e:
        return None, f"parse_error: {e}"

    # Validate allowed nodes
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_AST_NODES):
            return None, f"disallowed_ast_node: {type(node).__name__}"

    # Evaluate with restricted globals/locals
    try:
        value = eval(compile(tree, "<condition>", "eval"), {"__builtins__": {}}, dict(env))
    except Exception as e:
        return None, f"eval_error: {e}"

    if isinstance(value, bool):
        return value, None
    return None, "condition_not_boolean"


# -----------------------------
# Validation helpers
# -----------------------------

def _render_template(s: str, ctx: Dict[str, Any]) -> str:
    """
    Render placeholders like {run_dir} / {project_name}.
    Missing keys are left as-is.
    """
    if not isinstance(s, str):
        return str(s)

    class _SafeDict(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    try:
        return s.format_map(_SafeDict(ctx))
    except Exception:
        return s


def _is_mermaid_like(text: str) -> bool:
    # Minimal heuristic: mermaid diagrams commonly start with these.
    head = (text or "").lstrip()
    return head.startswith(("flowchart", "sequenceDiagram", "classDiagram", "stateDiagram", "erDiagram", "gantt", "mindmap", "journey"))


# -----------------------------
# Executor
# -----------------------------

class PlaybookExecutor:
    """Manages playbook execution state and progress."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.state_file = run_dir / "playbook_state.json"
        self.playbook_file = run_dir / "playbook_parsed.json"

        # per-step evidence/decision (parallel-safe)
        self.evidence_dir = run_dir / "evidence"
        self.decisions_dir = run_dir / "decisions"

    def load_or_create_state(self, playbook: Playbook, run_id: str) -> PlaybookState:
        if self.state_file.exists():
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return PlaybookState.from_dict(data)

        now = PlaybookState._now()
        state = PlaybookState(
            run_id=run_id,
            playbook_name=playbook.name,
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

        self.save_state(state)
        return state

    def save_state(self, state: PlaybookState):
        self.run_dir.mkdir(parents=True, exist_ok=True)
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

    # -----------------------------
    # Readiness / skipping logic
    # -----------------------------

    def build_runtime_vars(self, playbook: Playbook, extra_vars: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Variables available to conditions/templates/validation.
        """
        v = dict(playbook.variables or {})
        v["run_dir"] = str(self.run_dir)
        if extra_vars:
            v.update(extra_vars)
        return v

    def apply_auto_skips(self, state: PlaybookState, playbook: Playbook, *, extra_vars: Optional[Dict[str, Any]] = None) -> int:
        """
        Auto-skip steps whose condition is definitively False once dependencies are satisfied.
        Returns number of steps skipped in this pass.
        """
        ctx = self.build_runtime_vars(playbook, extra_vars=extra_vars)
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
            value, err = _safe_eval_expr(cond, ctx)
            if value is False:
                state.update_step(step.id, StepStatus.SKIPPED, note=f"auto-skipped: condition evaluated False ({cond})")
                skipped += 1
            elif value is None:
                # Not evaluable; keep pending but attach a note so user can resolve in prompt.
                state.steps[step.id].setdefault("notes", []).append({"at": PlaybookState._now(), "note": f"condition unresolved: {cond} ({err})"})

        if skipped:
            self.save_state(state)
        return skipped

    def get_ready_steps(self, state: PlaybookState, playbook: Playbook, *, extra_vars: Optional[Dict[str, Any]] = None, max_parallel: int = 99) -> List[PlaybookStep]:
        """
        Apply auto-skips first, then return all runnable steps.
        """
        self.apply_auto_skips(state, playbook, extra_vars=extra_vars)
        return state.get_ready_steps(playbook, max_parallel=max_parallel)

    # -----------------------------
    # Validation
    # -----------------------------

    def validate_step(self, step: PlaybookStep, playbook: Playbook, *, extra_vars: Optional[Dict[str, Any]] = None) -> Tuple[bool, List[str]]:
        """
        Validate outputs according to step.validation checks.
        Returns (ok, messages).
        """
        ctx = self.build_runtime_vars(playbook, extra_vars=extra_vars)
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
                # Minimal check: file exists, non-empty, and looks like mermaid syntax.
                if not p.exists() or p.stat().st_size == 0:
                    ok = False
                    messages.append(f"FAIL mermaid_valid (missing/empty): {target}")
                else:
                    content = p.read_text(encoding="utf-8", errors="ignore")
                    if not _is_mermaid_like(content):
                        ok = False
                        messages.append(f"FAIL mermaid_valid (unrecognized header): {target}")
                    else:
                        messages.append(f"OK mermaid_valid: {target}")

            else:
                ok = False
                messages.append(f"FAIL unknown_validation_rule: {kind} -> {target}")

        return ok, messages

    # -----------------------------
    # Prompts
    # -----------------------------

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

    def format_step_prompt(self, state: PlaybookState, step: PlaybookStep, playbook: Playbook, *, extra_vars: Optional[Dict[str, Any]] = None) -> str:
        """
        Generate execution prompt for a single step (serial).
        """
        ctx = self.build_runtime_vars(playbook, extra_vars=extra_vars)

        # Evaluate condition (for transparency)
        cond_line: List[str] = []
        if step.condition:
            val, err = _safe_eval_expr(step.condition.strip(), ctx)
            if val is True:
                cond_line.append(f"**Condition**: `{step.condition}` ✅")
            elif val is False:
                cond_line.append(f"**Condition**: `{step.condition}` ❌ (should be skipped)")
            else:
                cond_line.append(f"**Condition**: `{step.condition}` ⚠️ (unresolved: {err}) — confirm manually")

        # Per-step evidence/decision files (parallel-safe)
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
                lines.append(f"- { _render_template(inp, ctx) }")
            lines.append("")

        if step.actions:
            lines.append("**Actions**:")
            for act in step.actions:
                lines.append(f"- { _render_template(act, ctx) }")
            lines.append("")

        if step.outputs:
            lines.append("**Expected Outputs**:")
            for out in step.outputs:
                lines.append(f"- { _render_template(out, ctx) }")
            lines.append("")

        if step.exit_criteria:
            lines.append("**Exit Criteria** (verify before marking complete):")
            for crit in step.exit_criteria:
                lines.append(f"- [ ] { _render_template(crit, ctx) }")
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
            f"2. Write decisions/unknowns to `{dc_path.relative_to(self.run_dir)}` (mark unknown items as '要确认'; do not guess).",
            "3. If you *must* update global logs (EvidenceRegistry.md / DecisionLog.md), do it ONLY in serial mode or in the final review step to avoid conflicts.",
            "4. Confirm validations and exit criteria before marking the step complete.",
        ])

        return "\n".join(lines)

    def format_parallel_batch_prompt(self, state: PlaybookState, ready_steps: List[PlaybookStep], playbook: Playbook, *, extra_vars: Optional[Dict[str, Any]] = None) -> str:
        """
        Generate a Team Lead prompt to dispatch ready steps to Claude Agent Teams.
        """
        ctx = self.build_runtime_vars(playbook, extra_vars=extra_vars)

        if not ready_steps:
            return "No runnable steps at the moment (check dependencies/conditions)."

        # Group by routing for easier assignment
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
                val, err = _safe_eval_expr(s.condition.strip(), ctx)
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

    # -----------------------------
    # Progress / summary
    # -----------------------------

    def get_progress_summary(self, state: PlaybookState, playbook: Playbook) -> str:
        total = len(playbook.steps)
        completed = sum(1 for s in state.steps.values() if s.get("status") == StepStatus.COMPLETED.value)
        failed = sum(1 for s in state.steps.values() if s.get("status") == StepStatus.FAILED.value)
        skipped = sum(1 for s in state.steps.values() if s.get("status") == StepStatus.SKIPPED.value)
        running = sum(1 for s in state.steps.values() if s.get("status") == StepStatus.RUNNING.value)

        lines = [
            f"## Playbook Progress: {playbook.name}",
            f"- Overall: {completed}/{total} completed | {skipped} skipped | {failed} failed | {running} running",
            f"- Status: {state.overall_status}",
            f"- Run dir: {self.run_dir}",
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
            lines.append(f"{icon} **{step.id}**: {step.goal}")
            if status == StepStatus.RUNNING.value:
                lines.append("   └─ Current step")

        return "\n".join(lines)


# -----------------------------
# CLI
# -----------------------------

def _parse_kv_list(kv_list: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for kv in kv_list:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        k = k.strip()
        v = v.strip()
        # try JSON for lists/bools/numbers
        try:
            out[k] = json.loads(v)
        except Exception:
            out[k] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Playbook executor (parallel-ready) for Claude Code")
    ap.add_argument("playbook_file", type=str, help="Path to playbook .yml/.yaml or .md")
    ap.add_argument("--run-dir", type=str, default=".claude/runs/test", help="Run directory to store state/artifacts")
    ap.add_argument("--run-id", type=str, default="test-run", help="Run id (stored in state)")
    ap.add_argument("--vars", nargs="*", default=[], help="Extra vars as key=value (value can be JSON)")
    ap.add_argument("--summary", action="store_true", help="Print progress summary")
    ap.add_argument("--next", dest="do_next", action="store_true", help="Print next runnable step prompt (serial)")
    ap.add_argument("--ready", action="store_true", help="Print ready steps and agent-teams dispatch prompt (parallel)")
    ap.add_argument("--max-parallel", type=int, default=8, help="Max parallel steps to output for --ready")
    ap.add_argument("--validate", type=str, default="", help="Validate a specific step id (runs step.validation checks)")
    args = ap.parse_args()

    playbook_path = Path(args.playbook_file)
    run_dir = Path(args.run_dir)

    playbook = PlaybookParser.parse(playbook_path)
    executor = PlaybookExecutor(run_dir)
    state = executor.load_or_create_state(playbook, args.run_id)
    executor.save_playbook(playbook)

    extra_vars = _parse_kv_list(args.vars)

    if args.summary or (not args.do_next and not args.ready and not args.validate):
        print(executor.get_progress_summary(state, playbook))

    if args.do_next:
        ready = executor.get_ready_steps(state, playbook, extra_vars=extra_vars, max_parallel=1)
        if not ready:
            print("No runnable step (check dependencies/conditions).")
            return 0
        step = ready[0]
        print(executor.format_step_prompt(state, step, playbook, extra_vars=extra_vars))

    if args.ready:
        ready_steps = executor.get_ready_steps(state, playbook, extra_vars=extra_vars, max_parallel=args.max_parallel)
        print(executor.format_parallel_batch_prompt(state, ready_steps, playbook, extra_vars=extra_vars))
        print("")
        print("----")
        print("Step prompts (copy/paste per teammate):")
        for s in ready_steps:
            print("")
            print(executor.format_step_prompt(state, s, playbook, extra_vars=extra_vars))

    if args.validate:
        step_id = args.validate.strip()
        step = next((s for s in playbook.steps if s.id == step_id), None)
        if not step:
            print(f"Unknown step id: {step_id}")
            return 2
        ok, messages = executor.validate_step(step, playbook, extra_vars=extra_vars)
        print("\n".join(messages))
        if ok:
            print("VALIDATION: PASS")
            return 0
        print("VALIDATION: FAIL")
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
