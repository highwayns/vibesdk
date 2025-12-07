# DependencyManagement

## Purpose
The `DependencyManagement` class provides pure functions for handling dependencies in code generation workflows. It is used to merge, extract, and format dependencies from templates and package manifests without side effects.

## Core Component
- **DependencyManagement**: Static class with utility methods for dependency management.

## Responsibilities
- Merge dependencies from template and existing `package.json` files.
- Extract dependencies from a `package.json` string.
- Format dependency lists for display or logging.

## Key Methods
- `mergeDependencies(templateDeps, lastPackageJson, logger?)` — Merges dependencies from template and previous package.json.
- `extractDependenciesFromPackageJson(packageJson)` — Extracts dependencies as a key-value map.
- `formatDependencyList(deps)` — Formats dependencies as a string list.

## Usage
Used by agents to ensure all required dependencies are present and correctly merged during project setup and code generation.

---
*Referenced by: [SimpleCodeGeneratorAgent](simpleGeneratorAgent.md)*
