# AgentConfig (Config Types)

## Purpose
Defines the type structure for agent configuration, model selection, and inference context. These types are used throughout the agent system to ensure consistent configuration and model usage.

## Core Component
- **AgentConfig**: Interface describing the configuration for each agent action (template selection, blueprint, phase generation, etc.).

## Responsibilities
- Enumerate supported AI models (`AIModels` enum).
- Define model configuration options (name, reasoning effort, max tokens, temperature, fallback model).
- Specify the structure of the agent's inference context, including user overrides and feature flags.

## Key Types
- `AIModels`: Enum of supported model names.
- `ModelConfig`: Model configuration structure.
- `AgentConfig`: Main agent configuration interface.
- `InferenceContext`: Context for inference calls, including user and agent IDs, model configs, and feature flags.

## Usage
Used by the agent to select models and configure inference parameters for each action in the code generation workflow.

---
*Referenced by: [SimpleCodeGeneratorAgent](simpleGeneratorAgent.md)*
