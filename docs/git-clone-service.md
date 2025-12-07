# GitCloneService

## Purpose
The `GitCloneService` builds in-memory git repositories for agent-generated code, supports template rebasing, and implements the git HTTP protocol for code export and integration with external version control systems.

## Core Component
- **GitCloneService**: Static class for repository construction and git protocol handling.

## Responsibilities
- Build a git repository by rebasing agent commit history on top of template files.
- Handle git info/refs and upload-pack requests for git clone operations.
- Optimize repository structure for efficient export and integration.

## Key Methods
- `buildRepository(options)` — Constructs a repository with template base and agent commits.
- `handleInfoRefs(fs)` — Handles git info/refs requests for clone operations.
- `handleUploadPack(fs)` — Handles upload-pack requests for efficient cloning.

## Usage
Used by the agent to export generated code to GitHub or other git-based platforms, ensuring proper versioning and template lineage.

---
*Referenced by: [SimpleCodeGeneratorAgent](simpleGeneratorAgent.md)*
