# InferenceParamsStructured (Inference Utilities)

## Purpose
Provides structured parameters and helper functions for executing AI inference calls within the agent. Handles model selection, error handling, retries, and schema-based output validation.

## Core Component
- **InferenceParamsStructured**: Interface and function for structured inference calls with schema validation.

## Responsibilities
- Define the structure for inference parameters, including environment, messages, model config, and schema.
- Execute inference with retries, error handling, and model fallback logic.
- Support both string and structured (schema-validated) inference responses.
- Provide utility functions for generating file enhancement and generation request messages.

## Key Functions
- `executeInference(params)`: Executes an inference call with the given parameters and schema.
- `createFileEnhancementRequestMessage(filePath, fileContents)`: Generates a message for file enhancement requests.
- `createFileGenerationResponseMessage(filePath, fileContents, explanation, nextFile?)`: Generates a message for file generation responses.

## Usage
Used by the agent to interact with LLMs for code generation, file enhancement, and structured output parsing.

---
*Referenced by: [SimpleCodeGeneratorAgent](simpleGeneratorAgent.md)*
