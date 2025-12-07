# FileProcessing

## Purpose
The `FileProcessing` class provides utilities for cleaning, processing, and managing file contents during code generation. It is used to apply diffs, clean code blocks, and manage file metadata.

## Core Component
- **FileProcessing**: Static class with file processing utilities.

## Responsibilities
- Clean file contents by removing code block markers.
- Apply unified diffs to file contents.
- Find file purposes from phase or generated files.
- Aggregate all relevant files from templates and generated outputs.

## Key Methods
- `cleanFileContents(fileContents)` — Removes code block markers from file contents.
- `processGeneratedFileContents(generatedFile, originalContents, logger?)` — Applies diffs or returns cleaned content.
- `findFilePurpose(filePath, phase, generatedFilesMap)` — Determines the purpose of a file.
- `getAllRelevantFiles(templateDetails, generatedFilesMap)` — Aggregates all relevant files for the project.

## Usage
Used by agents to process and validate file outputs, especially when handling LLM-generated code and diffs.

---
*Referenced by: [SimpleCodeGeneratorAgent](simpleGeneratorAgent.md)*
