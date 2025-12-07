# MCPServerConfig & MCPResult (Tool Types)

## Purpose
Defines types for MCP server configuration and tool execution results. These types are used by the MCPManager and related tool infrastructure.

## Core Components
- **MCPServerConfig**: Configuration for an MCP server (name, SSE URL).
- **MCPResult**: Result type for tool execution.

## Responsibilities
- Specify the structure for MCP server configuration.
- Define the result format for tool calls and error handling.
- Provide type definitions for tool implementations and arguments.

## Key Types
- `MCPServerConfig`: Server configuration (name, sseUrl).
- `MCPResult`: Tool execution result (content).
- `ErrorResult`, `ToolCallResult`, `ToolImplementation`, `ToolDefinition`, `ExtractToolArgs`, `ExtractToolResult`: Supporting types for tool infrastructure.

## Usage
Used by the MCPManager and agent to configure tool servers and handle tool execution results.

---
*Referenced by: [MCPManager](mcpManager.md)*
