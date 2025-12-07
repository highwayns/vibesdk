# AgentOperation (Operations)

## Purpose
Defines the abstract base class for agent operations, such as phase generation, file regeneration, and user message processing. Provides a standard interface for modular, pluggable operations within the agent.

## Core Component
- **AgentOperation**: Abstract class for defining agent operations.

## Responsibilities
- Standardize the interface for agent operations (input, output, execution method).
- Provide operation options including environment, context, logger, and agent reference.
- Support extension for specific operations (e.g., phase generation, file regeneration).

## Key Types
- `OperationOptions`: Options passed to each operation, including context and logger.
- `AgentOperation<InputType, OutputType>`: Abstract class with `execute` method.

## Usage
Used by the agent to modularize and encapsulate complex operations, enabling extensibility and testability.

---
*Referenced by: [SimpleCodeGeneratorAgent](simpleGeneratorAgent.md)*
