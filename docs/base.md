# CodeGenerationFormat (Streaming Formats)

## Purpose
Defines the base class and interfaces for streaming code generation formats. Handles chunked parsing, file boundary detection, and real-time file assembly from LLM output streams.

## Core Component
- **CodeGenerationFormat**: Abstract class for streaming code generation formats.

## Responsibilities
- Parse streaming output chunks and reconstruct files in real time.
- Support multiple file formats (full content, unified diff, etc.).
- Provide serialization and deserialization of file objects.
- Define format instructions for LLM prompts.

## Key Interfaces
- `ParsingState`: Tracks the state of the streaming parser.
- `CodeGenerationStreamingState`: Aggregates parsed files and state.

## Usage
Used by the agent to stream code generation results to clients, enabling real-time feedback and efficient file assembly.

---
*Referenced by: [SimpleCodeGeneratorAgent](simpleGeneratorAgent.md)*
