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

This skill is compatible with:
- YAML playbooks (recommended)
- Markdown playbooks (legacy)

---

## Command Syntax

```bash
/sc:run-playbook <playbook-file> [options]
```

### Options

- `--resume`  
  Continue from last checkpoint (default if state exists)

- `--status`  
  Show current progress without executing

- `--reset`  
  Start fresh, clearing previous state (backup old state first)

- `--step <step_id>`  
  Jump to a specific step (used sparingly; keep auditability)

- `--dry-run`  
  Parse and validate playbook syntax only (no execution)

- `--ready`  
  Compute and print ALL runnable steps (dependencies satisfied) and generate an **Agent Teams dispatch prompt** (does not execute tools)

- `--max-parallel <N>`  
  Limit number of runnable steps included in `--ready` output (default: 4)

- `--vars k=v ...`  
  Provide runtime variables used for:
  - `condition` evaluation
  - template rendering (e.g., `{run_id}`, `{run_dir}`, etc.)
  Values can be:
  - plain strings (`project_name=foo`)
  - JSON (`doc_types=["system_overview","program"]`)

- `--validate <step_id>`  
  Run validation gates for a given step (auto-check supported validations; unsupported validations become “要确认” and require manual evidence)

---

## Execution Protocol

### Phase 0: Safety & Guardrails (always)

1. **No guessing**: Never infer as fact without a traceable source.
2. **Evidence first**: Each claim must cite a `file:path:line` or config key.
3. **Template integrity**: Never delete template chapters; use “不适用/无” if needed.
4. **Secrets hygiene**: Never print or store secrets in logs/docs.

---

## Phase 1: Initialization

1. **Parse arguments**
   - First token = playbook filename (look in `.claude/playbooks/`)
   - Detect mode (`--status`, `--ready`, etc.) and options

2. **Load playbook**
   - YAML preferred
   - Executor path (choose one and keep consistent in repo):
     ```bash
     python3 .claude/lib/playbook_executor_v2.py .claude/playbooks/<filename> [options...]
     ```
   - If your repo uses another executor file name, update this line accordingly.

3. **Resolve run directory**
   - Use `$CLAUDE_SESSION_ID` if available
   - Otherwise: `.claude/runs/manual-<timestamp>/`

4. **Create run subdirectories**
   - Always create:
     - `inventory/`, `diagrams/`, `docs/`, `review/`, `artifacts/`
   - For evidence (parallel-safe):
     - `evidence/` (per-step evidence)
     - `decisions/` (per-step decision logs)
   - Keep legacy aggregate docs for final merge (optional):
     - `EvidenceRegistry.md`, `DecisionLog.md`

   Recommended structure:
   ```
   <run_dir>/
     RunSpec.md
     EvidenceRegistry.md        (optional aggregate; written in SERIAL or final merge)
     DecisionLog.md            (optional aggregate; written in SERIAL or final merge)
     evidence/
       <step_id>.md            (parallel-safe)
     decisions/
       <step_id>.md            (parallel-safe)
     inventory/
     diagrams/
     docs/
     review/
     artifacts/
     deliverables.md
     playbook_state.json
     playbook_parsed.json
     events.jsonl
   ```

5. **Copy templates (if not exists)**
   | Template | Destination |
   |----------|-------------|
   | RunSpec.md | `<run_dir>/RunSpec.md` |
   | EvidenceRegistry.md | `<run_dir>/EvidenceRegistry.md` |
   | DecisionLog.md | `<run_dir>/DecisionLog.md` |
   | review_report.md | `<run_dir>/review/report.md` |
   | issues.md | `<run_dir>/review/issues.md` |
   | deliverables.md | `<run_dir>/deliverables.md` |

   Additionally, ensure per-step files exist (create lazily if needed):
   - `<run_dir>/evidence/<step_id>.md`
   - `<run_dir>/decisions/<step_id>.md`

6. **Load or create state**
   - State file: `<run_dir>/playbook_state.json`
   - If exists:
     - `--status`: show progress and exit
     - `--resume`: continue from `current_step`
     - default: ask user whether to resume/reset (if interactive)
   - If `--reset`: backup old state, create fresh

7. **Merge runtime variables**
   - Load playbook variables defaults
   - Apply `--vars` overrides
   - Persist to state: `state.variables`

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

### Runnable (READY) definition
A step is READY if:
- status is `pending` (or `blocked`)
- all `depends_on` are satisfied (`completed` or `skipped`)
- condition (if any) is true

---

## Phase 2A: `--ready` mode (Agent Teams Dispatch)

When `--ready` is used:

1. Compute READY steps.
2. Select up to `--max-parallel N` steps.
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
   - Update state: `step.status = "running"`
   - Log to `events.jsonl`

2. **During execution**
   - Execute ONLY actions listed in playbook
   - Evidence & decisions logging:
     - **SERIAL** (single-thread): write to aggregate docs  
       - `EvidenceRegistry.md`
       - `DecisionLog.md`
     - **PARALLEL-SAFE** (recommended even in serial): write per-step docs  
       - `evidence/<step_id>.md`
       - `decisions/<step_id>.md`

   Evidence row format (recommended):
   ```markdown
   | F-{n} | {statement} | {source:path:line} | 确/要确认 | {impact} | {notes} |
   ```

   Decision row format (recommended):
   ```markdown
   | {time} | {decision/question} | {options} | {chosen} | {rationale} | {evidence_refs} | {follow-up} |
   ```

3. **Quality gates (MUST follow)**
   - ❌ NEVER guess or infer without evidence
   - ❌ NEVER delete template chapters (use "不适用/无" instead)
   - ❌ NEVER proceed if exit criteria not verifiable
   - ✅ Mark uncertain items as "要确认" with specific questions
   - ✅ Cite evidence for every conclusion (file:line or config:key)

4. **After completing step**
   - Verify ALL exit criteria are met
   - Run validations (if present) or mark unsupported ones as “要确认”
   - Update state: `step.status = "completed"` or `"failed"`
   - Record outputs in state
   - Log completion event

5. **On skip (condition false)**
   - Update state: `step.status = "skipped"`
   - Add a short note to `decisions/<step_id>.md` explaining why it was skipped
   - Log skip event

6. **On failure or uncertainty**
   - Update state: `step.status = "blocked"` or `"failed"`
   - Add entry to `decisions/<step_id>.md` with blockers
   - Ask user for guidance before proceeding

---

## Phase 2C: Validation Gates (`--validate` and post-step)

### Supported validations (auto-check)
- `file_exists: "<path>"`
- `file_not_empty: "<path>"`
- `directory_not_empty: "<path>"`
- `mermaid_valid: "<path>.mmd"` (basic: file exists + not empty; optional syntax lint if available)

### Unsupported validations (manual evidence required)
Examples you may see in playbooks:
- `csv_has_rows`
- `template_complete`
- compound/conditional validation entries

When unsupported:
- Record as “要确认” in `decisions/<step_id>.md`
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
   - Categorize: OK / 要修正 / 要确认
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

⬜ intake — Confirm scope and initialize run artifacts
✅ inventory — Enumerate objects and configs
🔄 feature_map — Build feature decomposition and dependency graph  ← CURRENT
⬜ docs_system — System overview spec
⬜ review — Self-review
⬜ delivery — Handoff

Progress: X/Y steps completed (Z%)
Evidence: per-step files in evidence/
Decisions: per-step files in decisions/
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
  "steps": {
    "intake": {
      "status": "completed",
      "started_at": "...",
      "completed_at": "...",
      "outputs": ["RunSpec.md"],
      "evidence": []
    },
    "docs_system": {
      "status": "skipped",
      "started_at": "...",
      "completed_at": "...",
      "outputs": [],
      "evidence": []
    }
  },
  "variables": {},
  "errors": []
}
```

---

## Error Recovery

If execution is interrupted:

1. State is automatically persisted after each action
2. Next run with same session_id will:
   - Load existing state
   - Show progress summary
   - Ask: “Resume from step {current_step}? [Y/n]” (if interactive)
3. User can choose to:
   - Resume (default)
   - Reset and start over
   - Jump to specific step

---

## Integration with Hooks

This skill can rely on `.claude/hooks/` for event logging:

- `user_prompt_submit.sh`: Initialize run directory
- `pre_tool_use.sh`: Log tool invocation
- `post_tool_use.sh`: Log tool result
- `post_tool_use_failure.sh`: Log failures

All events are appended to `<run_dir>/events.jsonl`.

---

## Example Usage

```bash
# Start new playbook run (serial)
 /sc:run-playbook genexus_code_analysis_playbook_v2.yml

# Check progress
 /sc:run-playbook genexus_code_analysis_playbook_v2.yml --status

# Resume interrupted run
 /sc:run-playbook genexus_code_analysis_playbook_v2.yml --resume

# Start fresh
 /sc:run-playbook genexus_code_analysis_playbook_v2.yml --reset

# Validate playbook syntax only
 /sc:run-playbook genexus_code_analysis_playbook_v2.yml --dry-run

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
