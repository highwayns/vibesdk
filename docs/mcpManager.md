# MCPManager

## Purpose
The `MCPManager` class manages connections to Model Context Protocol (MCP) servers and provides unified access to external tools and functions. It enables the agent to discover, list, and execute tools available on connected MCP servers.

## Core Component
- **MCPManager**: Class for managing MCP server connections and tool execution.

## Responsibilities
- Initialize and maintain connections to multiple MCP servers.
- List available tools and their definitions.
- Execute tools by name with provided arguments.
- Track tool availability and manage tool-to-server mapping.
- Provide shutdown and cleanup logic for connections.

## Key Methods
- `initialize()`: Establish connections to MCP servers.
- `getToolDefinitions()`: List all available tools.
- `executeTool(toolName, args)`: Execute a tool by name.
- `hasToolAvailable(toolName)`: Check if a tool is available.
- `getAvailableToolNames()`: List all available tool names.
- `shutdown()`: Clean up connections and state.

## Usage
Used by the agent to access external tools and augment its capabilities via the MCP protocol.

---
*Referenced by: [SimpleCodeGeneratorAgent](simpleGeneratorAgent.md)*
