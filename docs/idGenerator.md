# IdGenerator

## Purpose
The `IdGenerator` class provides utility functions for generating unique IDs for conversations, entities, and other objects within the agent system.

## Core Component
- **IdGenerator**: Static class for ID generation.

## Responsibilities
- Generate unique conversation IDs with timestamps and random suffixes.
- Generate generic unique IDs with custom prefixes.

## Key Methods
- `generateConversationId()`: Generate a unique conversation ID.
- `generateId(prefix?)`: Generate a generic unique ID with a prefix.

## Usage
Used throughout the agent system to ensure unique identification of conversations, sessions, and other entities.

---
*Referenced by: [SimpleCodeGeneratorAgent](simpleGeneratorAgent.md)*
