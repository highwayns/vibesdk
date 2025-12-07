# StateMigration

## Purpose
The `StateMigration` class is responsible for migrating and cleaning up agent state across different schema versions. It ensures backward compatibility and data integrity as the agent's state structure evolves.

## Core Component
- **StateMigration**: Static class with migration logic for agent state objects.

## Responsibilities
- Detect outdated or deprecated state fields and migrate them to the latest schema.
- Clean up conversation history, remove duplicates, and filter out internal memos.
- Migrate file formats and template details as needed.
- Remove deprecated or obsolete properties from the state.

## Key Method
- `migrateIfNeeded(state: CodeGenState, logger: StructuredLogger): CodeGenState | null` — Returns a migrated state if changes are needed, or null if no migration is required.

## Usage
`StateMigration` is invoked by the agent during initialization or state restoration to ensure the state is up-to-date and free of legacy artifacts.

---
*Referenced by: [SimpleCodeGeneratorAgent](simpleGeneratorAgent.md)*
