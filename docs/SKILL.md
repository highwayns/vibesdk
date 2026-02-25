---
name: sc:run-playbook
description: Execute a repo playbook (markdown/yaml) with state tracking, checkpoints, evidence tracking, progress reporting, and optional Agent-Teams parallel dispatch.
argument-hint: "<playbook-file> [--resume|--status|--reset|--step <id>|--dry-run|--ready|--max-parallel N|--vars k=v ...|--validate <step_id>] (e.g. genexus_code_analysis_playbook_v2.yml)"
disable-model-invocation: false
allowed-tools: Read, Grep, Glob, Edit, Write, Bash, mcp__desktop-commander
---

# sc:run-playbook — Enhanced Playbook Executor (Agent-Teams Ready)

## Overview

This skill executes deterministic, auditable playbooks step-by-step with:

- **State persistence**: Resume interrupted runs
- **Evidence tracking**: Every fact must have a source
- **Progress reporting**: Real-time visibility
- **Quality gates**: No guessing allowed
- **Parallel dispatch (optional)**: Identify runnable steps and dispatch them to **Agent Teams** safely
- **Event logging**: All actions logged to `events.jsonl` for audit

This skill is compatible with:
- YAML playbooks (recommended — full feature support)
- Markdown playbooks (supports condition, validation, routing since v3; custom step IDs via `## Step: id — goal`)

> **Architecture note**: The executor (`playbook_executor_v3.py`) is a **state manager and prompt generator** — it does NOT execute step actions itself. The Claude Code agent reads the generated prompt and performs the actual work (file reads, code analysis, document creation, etc.). The executor tracks state transitions, evaluates conditions, and ensures auditability.

---

## Command Syntax

```bash
/sc:run-playbook <playbook-file> [options]
```

### Options

- `--resume`  
  Continue from last checkpoint (if current step is still RUNNING, resume it; otherwise advance to next)

- `--status`  
  Show current progress without executing (alias: `--summary`)

- `--reset`  
  Start fresh — backs up old state to `playbook_state.backup.<timestamp>.json`, then creates new state

- `--step <step_id>`  
  Jump to a specific step (marks it RUNNING regardless of current status; use sparingly for auditability)

- `--dry-run`  
  Parse and validate playbook syntax only — prints step list, variables, and version check; no state created

- `--ready`  
  Compute and print ALL runnable steps (dependencies satisfied) and generate an **Agent Teams dispatch prompt** (does not execute tools)

- `--max-parallel <N>`  
  Limit number of runnable steps included in `--ready` output (default: **4**)

- `--vars k=v ...`  
  Provide runtime variables used for:
  - `condition` evaluation
  - template rendering (e.g., `{run_id}`, `{run_dir}`, etc.)
  Values can be:
  - plain strings (`project_name=foo`)
  - JSON (`doc_types=["system_overview","program"]`)
  Variables are **persisted into state** on first run and merged on resume.

- `--validate <step_id>`  
  Run validation gates for a given step (auto-check supported validations; unsupported validations become "要確認" and require manual evidence)

- `--next`  
  Print next runnable step prompt (serial mode); marks the step as RUNNING

---

## Execution Protocol

### Phase 0: Safety & Guardrails (always)

1. **No guessing**: Never infer as fact without a traceable source.
2. **Evidence first**: Each claim must cite a `file:path:line` or config key.
3. **Template integrity**: Never delete template chapters; use "不適用/無" if needed.
4. **Secrets hygiene**: Never print or store secrets in logs/docs.

---

## Phase 1: Initialization

1. **Parse arguments**
   - First token = playbook filename (look in `.claude/playbooks/`)
   - Detect mode (`--status`, `--ready`, etc.) and options

2. **Load playbook**
   - YAML preferred (full feature support)
   - Markdown supported (condition, validation, routing parsed since v3)
   - Executor path (keep consistent in repo):
     ```bash
     python3 .claude/lib/playbook_executor_v3.py .claude/playbooks/<filename> [options...]
     ```
   - **Version check**: Executor warns on stderr if playbook `version` is not in the supported set. Heed warnings and consider upgrading.

3. **Resolve run directory**
   - Use `$CLAUDE_SESSION_ID` if available
   - Otherwise: `.claude/runs/manual-<timestamp>/`

4. **Create run subdirectories** (handled by executor on first state creation)
   The executor automatically creates:
   - `inventory/`, `diagrams/`, `docs/`, `review/`, `artifacts/`
   - `evidence/` (per-step evidence files)
   - `decisions/` (per-step decision logs)
   - Template stubs: `RunSpec.md`, `EvidenceRegistry.md`, `DecisionLog.md`, `review/report.md`, `review/issues.md`, `deliverables.md`

   Resulting structure:
   ```
   <run_dir>/
     RunSpec.md
     EvidenceRegistry.md        (aggregate; written in SERIAL or final merge)
     DecisionLog.md             (aggregate; written in SERIAL or final merge)
     evidence/
       <step_id>.md             (parallel-safe)
     decisions/
       <step_id>.md             (parallel-safe)
     inventory/
     diagrams/
     docs/
     review/
       report.md
       issues.md
     artifacts/
     deliverables.md
     playbook_state.json
     playbook_parsed.json
     events.jsonl
   ```

5. **Load or create state**
   - State file: `<run_dir>/playbook_state.json`
   - If exists:
     - `--status`: show progress and exit
     - `--resume`: continue from `current_step`
     - `--reset`: backup old state, create fresh
     - default: show progress summary
   - State schema includes: `variables` (persisted), `errors` (structured), `playbook_file` (source path)

6. **Merge runtime variables**
   - Load playbook variables defaults
   - Merge persisted `state.variables`
   - Apply `--vars` overrides
   - Persist merged result to state for future `--resume`

---

## Phase 2: Compute Runnable Steps (dependency + condition aware)

### Dependency rule (CRITICAL)
A dependency is satisfied if the dependency step status is:
- `completed` OR `skipped`

This prevents conditional doc steps from blocking `review`/`delivery`.

### Condition rule
If a step has `condition`, evaluate it against:
- `state.variables`
- known runtime context (`run_id`, `run_dir`)
If condition is false, mark step `skipped` with a short note in per-step decisions log.

**Safety**: Condition evaluation is sandboxed (AST whitelist, no arithmetic operators, expression length limit, timeout guard). Only boolean comparisons and `in`/`not in` are allowed.

### Runnable (READY) definition
A step is READY if:
- status is `pending`
- all `depends_on` are satisfied (`completed` or `skipped`)
- condition (if any) is true or unresolved (unresolved → agent confirms manually)

A step is also READY if:
- status is `blocked` AND `allow_blocked=True` is set (for manual condition resolution)

---

## Phase 2A: `--ready` mode (Agent Teams Dispatch)

When `--ready` is used:

1. Compute READY steps (auto-skip false conditions first).
2. Select up to `--max-parallel N` steps (default: **4**).
3. Print:
   - list of READY steps
   - a single consolidated **dispatch prompt** suitable for Claude Agent Teams
4. Do NOT execute tools in this mode.

### Dispatch prompt requirements
For each READY step, include:
- Step id, goal, routing role (if any)
- Inputs, actions, expected outputs, exit criteria
- Where to write outputs (run_dir paths)
- Evidence rule:
  - write facts to `evidence/<step_id>.md`
  - write decisions/questions to `decisions/<step_id>.md`
- Validation gates to satisfy (if supported)

---

## Phase 2B: Step Execution Loop (SERIAL mode)

By default (no `--ready`), run the playbook deterministically:

For each step in sequence (respecting dependencies and conditions):

```
┌─────────────────────────────────────────────────────┐
│  STEP: {step_id}                                    │
│  Goal: {goal}                                       │
├─────────────────────────────────────────────────────┤
│  Inputs:                                            │
│    - {input_1}                                      │
│    - {input_2}                                      │
├─────────────────────────────────────────────────────┤
│  Actions:                                           │
│    □ {action_1}                                     │
│    □ {action_2}                                     │
├─────────────────────────────────────────────────────┤
│  Expected Outputs:                                  │
│    - {output_1}                                     │
│    - {output_2}                                     │
├─────────────────────────────────────────────────────┤
│  Exit Criteria:                                     │
│    [ ] {criterion_1}                                │
│    [ ] {criterion_2}                                │
└─────────────────────────────────────────────────────┘
```

### Execution Rules

1. **Before executing any action**
   - Update state: `step.status = "running"` (executor does this automatically with `--next`)
   - Log to `events.jsonl` (executor does this automatically)

2. **During execution**
   - Execute ONLY actions listed in playbook
   - Evidence & decisions logging:
     - **PARALLEL-SAFE** (recommended): write per-step docs  
       - `evidence/<step_id>.md`
       - `decisions/<step_id>.md`
     - **SERIAL aggregate** (optional): also append to  
       - `EvidenceRegistry.md`
       - `DecisionLog.md`

   Evidence row format (recommended):
   ```markdown
   | F-{n} | {statement} | {source:path:line} | 確/要確認 | {impact} | {notes} |
   ```

   Decision row format (recommended):
   ```markdown
   | {time} | {decision/question} | {options} | {chosen} | {rationale} | {evidence_refs} | {follow-up} |
   ```

3. **Quality gates (MUST follow)**
   - ❌ NEVER guess or infer without evidence
   - ❌ NEVER delete template chapters (use "不適用/無" instead)
   - ❌ NEVER proceed if exit criteria not verifiable
   - ✅ Mark uncertain items as "要確認" with specific questions
   - ✅ Cite evidence for every conclusion (file:line or config:key)

4. **After completing step**
   - Verify ALL exit criteria are met
   - Run validations (if present) or mark unsupported ones as "要確認"
   - Update state: `step.status = "completed"` or `"failed"`
   - Record outputs in state
   - Log completion event to `events.jsonl`

5. **On skip (condition false)**
   - Update state: `step.status = "skipped"` (executor auto-skips definitively false conditions)
   - Add a short note to `decisions/<step_id>.md` explaining why it was skipped
   - Log skip event

6. **On failure or uncertainty**
   - Update state: `step.status = "blocked"` or `"failed"`
   - Record structured error: `state.errors[]`
   - Add entry to `decisions/<step_id>.md` with blockers
   - Ask user for guidance before proceeding

---

## Phase 2C: Validation Gates (`--validate` and post-step)

### Supported validations (auto-check)
- `file_exists: "<path>"`
- `file_not_empty: "<path>"`
- `directory_not_empty: "<path>"`
- `mermaid_valid: "<path>.mmd"` (basic: file exists + not empty + recognized header)

### Unsupported validations (manual evidence required)
Examples you may see in playbooks:
- `csv_has_rows`
- `template_complete`
- compound/conditional validation entries
- `manual: "<description>"` (explicit manual check)

When unsupported:
- Record as "要確認" in `decisions/<step_id>.md`
- Provide a concrete verification command/checklist
- Require manual evidence before marking the step fully completed

---

## Phase 3: Completion

1. **(Optional) Merge per-step evidence into aggregate files**
   - In parallel-safe mode, per-step files are the source of truth.
   - At the end, you MAY compile summaries into:
     - `<run_dir>/EvidenceRegistry.md`
     - `<run_dir>/DecisionLog.md`

2. **Generate deliverables summary**
   Update `<run_dir>/deliverables.md`:

   ```markdown
   # Deliverables

   ## Created/Updated Files
   - [x] <file_path> - <description>

   ## Verification Steps
   - [ ] Run: <command>
   - [ ] Check: <condition>

   ## Rollback Notes
   - <instructions>

   ## Recommended Next Actions
   - [ ] <action>
   ```

3. **Generate review report**
   - Compile findings from all steps
   - Categorize: OK / 要修正 / 要確認
   - Link to evidence (prefer per-step evidence paths)

4. **Update final state**
   Set `overall_status = "completed"` with a summary:
   ```json
   {
     "overall_status": "completed",
     "completed_at": "<timestamp>",
     "summary": {
       "total_steps": N,
       "completed": N,
       "failed": 0,
       "skipped": M,
       "issues_found": K
     }
   }
   ```

5. **Suggest next actions**
   - If issues found → list remediation steps
   - If PR needed → prepare plan (but DO NOT create PR without user approval)

---

## Progress Display (`--status`)

When `--status` is used or during execution, show:

```markdown
## Playbook Progress: {playbook_name}
Run ID: {run_id}
Started: {timestamp}
Status: {overall_status}

- Overall: X/Y completed | Z skipped | 0 failed | 1 running | 0 blocked
- Progress: X/Y steps done (N%)
- Run dir: .claude/runs/session-xxx
- Evidence: per-step files in evidence/
- Decisions: per-step files in decisions/

⬜ intake — Confirm scope and initialize run artifacts
✅ inventory — Enumerate objects and configs
🔄 feature_map — Build feature decomposition and dependency graph  ← CURRENT
⬜ docs_system — System overview spec
⬜ review — Self-review
⬜ delivery — Handoff
```

---

## State File Schema

`playbook_state.json`:

```json
{
  "run_id": "session-xxx",
  "playbook_name": "genexus_code_analysis",
  "playbook_file": ".claude/playbooks/genexus_code_analysis_playbook_v2.yml",
  "current_step": "feature_map",
  "overall_status": "running",
  "started_at": "2026-01-23T10:00:00Z",
  "updated_at": "2026-01-23T10:30:00Z",
  "completed_at": null,
  "variables": {
    "scope": "all",
    "doc_types": ["system_overview", "program"]
  },
  "errors": [],
  "steps": {
    "intake": {
      "status": "completed",
      "started_at": "...",
      "completed_at": "...",
      "outputs": ["RunSpec.md"],
      "evidence": [],
      "notes": []
    },
    "docs_system": {
      "status": "skipped",
      "started_at": "...",
      "completed_at": "...",
      "outputs": [],
      "evidence": [],
      "notes": [{"at": "...", "note": "auto-skipped: condition evaluated False"}]
    }
  }
}
```

---

## Parallel Safety

### State file locking
The executor uses file-level locking (`fcntl.flock` on Unix) for all state reads and writes. This prevents data loss when multiple Agent Team members attempt to update state concurrently.

### Per-step isolation
- Evidence: `evidence/<step_id>.md` (one file per step — no conflicts)
- Decisions: `decisions/<step_id>.md` (one file per step — no conflicts)
- Global logs (`EvidenceRegistry.md`, `DecisionLog.md`): write ONLY in serial mode or during the final merge step

### Rules for parallel execution
- Each teammate writes ONLY to their assigned step's output files
- No two teammates should edit the same file
- Condition resolution questions go to `decisions/<step_id>.md`, not global logs

---

## Error Recovery

If execution is interrupted:

1. State is automatically persisted after each state transition (with file locking)
2. Next run with same session_id / run-dir will:
   - Load existing state
   - With `--resume`: continue from current step (if RUNNING) or advance to next
   - With `--status`: show progress summary only
   - Default: show progress summary
3. User can choose to:
   - Resume: `--resume` (default behavior)
   - Reset and start over: `--reset` (backs up old state)
   - Jump to specific step: `--step <id>`

### Non-interactive error handling
When running non-interactively (e.g., in CI or automated agent loops):
- If executor script crashes: re-run with `--resume`; state is persisted at each transition
- If state file is corrupted: delete `playbook_state.json` and re-run (or use `--reset`)
- If a dependency step failed: downstream steps remain PENDING; agent should address the failure first, then use `--step <id>` to retry
- All errors are recorded in `state.errors[]` for post-mortem analysis

---

## Integration with Hooks

This skill can rely on `.claude/hooks/` for event logging:

- `user_prompt_submit.sh`: Initialize run directory
- `pre_tool_use.sh`: Log tool invocation
- `post_tool_use.sh`: Log tool result
- `post_tool_use_failure.sh`: Log failures

All events are appended to `<run_dir>/events.jsonl` (executor also appends its own events).

---

## Example Usage

```bash
# Parse and validate playbook syntax only (no execution)
 /sc:run-playbook genexus_code_analysis_playbook_v2.yml --dry-run

# Start new playbook run (serial, advance step by step)
 /sc:run-playbook genexus_code_analysis_playbook_v2.yml --next

# Check progress
 /sc:run-playbook genexus_code_analysis_playbook_v2.yml --status

# Resume interrupted run
 /sc:run-playbook genexus_code_analysis_playbook_v2.yml --resume

# Start fresh (backs up old state)
 /sc:run-playbook genexus_code_analysis_playbook_v2.yml --reset

# Jump to specific step
 /sc:run-playbook genexus_code_analysis_playbook_v2.yml --step docs_system

# Compute runnable steps and generate Agent Teams dispatch prompt
 /sc:run-playbook genexus_code_analysis_playbook_v2.yml --ready --max-parallel 4 --vars scope=all --vars 'doc_types=["system_overview","program"]'

# Run validation gates for a step
 /sc:run-playbook genexus_code_analysis_playbook_v2.yml --validate docs_db
```

---

## Forbidden Actions

- ❌ Guessing without evidence
- ❌ Deleting template chapters
- ❌ Creating PR without explicit user approval
- ❌ Skipping quality gates
- ❌ Proceeding with unverified exit criteria
- ❌ Exposing secrets/credentials in logs or documents
- ❌ Writing to the same evidence/decision file concurrently in parallel runs
  - Use `evidence/<step_id>.md` + `decisions/<step_id>.md` to stay parallel-safe
- ❌ Assuming playbook actions are executed by the executor (the executor generates prompts; the agent executes)
- ❌ Using tools outside `allowed-tools` without explicit user permission
