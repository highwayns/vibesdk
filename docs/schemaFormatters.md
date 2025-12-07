# TemplateRegistryEntry (Schema Formatters)

## Purpose
Provides utilities for formatting and parsing structured data (such as code generation schemas) as Markdown or other formats. Enables schema-driven prompt generation and robust parsing of LLM outputs.

## Core Component
- **TemplateRegistryEntry**: Registry entry for schema formatting and parsing logic.

## Responsibilities
- Format Zod schemas as Markdown templates for LLM prompts.
- Parse Markdown content into structured data according to a schema.
- Provide prompt instructions for schema-based output.
- Support extensible registry for additional formats.

## Key Functions
- `formatSchemaAsMarkdown(schema, options?)`: Formats a Zod schema as a Markdown template.
- `parseMarkdownContent(markdownInput, schema, options?)`: Parses Markdown into structured data.
- `generateTemplateForSchema(schema, schemaFormat, options?)`: Generates prompt instructions for a schema.

## Usage
Used by the agent to generate and parse structured LLM outputs, ensuring reliable and machine-readable results.

---
*Referenced by: [SimpleCodeGeneratorAgent](simpleGeneratorAgent.md)*
