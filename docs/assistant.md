# Assistant

## Purpose
The `Assistant` class provides conversational context and message history management for agents. It is a lightweight utility that maintains a history of messages and supports basic operations such as saving, retrieving, and clearing conversation history.

## Core Component
- **Assistant<Env>**: Generic class for managing message history and environment context for an agent.

## Responsibilities
- Store and manage the sequence of messages exchanged in an agent's session.
- Provide methods to save new messages, retrieve the current history, and clear the history.
- Optionally initialize with a system prompt.

## Key Methods
- `save(messages: Message[]): Message[]` — Appends messages to the history.
- `getHistory(): Message[]` — Retrieves the current message history.
- `clearHistory()` — Clears the message history.

## Usage
The `Assistant` is typically used as a helper within agent implementations to maintain conversational state, especially for multi-turn interactions.

---
*Referenced by: [SimpleCodeGeneratorAgent](simpleGeneratorAgent.md)*
