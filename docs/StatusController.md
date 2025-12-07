# StatusController

## Purpose
Returns platform-wide status, global user messages, and change logs for the frontend.

## Core Components
- StatusController (class)

## Responsibilities
- Provide global user messages and change logs
- Indicate if there are active platform-wide messages

## Key Interactions
- **Platform Config**: Reads global messaging and change logs from configuration ([Database Services and Types](Database Services and Types.md))

## Data Flow
```mermaid
sequenceDiagram
    participant Client
    participant StatusController
    participant PlatformConfig

    Client->>StatusController: GET /api/status/platform
    StatusController->>PlatformConfig: get globalUserMessage, changeLogs
    PlatformConfig-->>StatusController: Status data
    StatusController-->>Client: Status response
```

## Endpoints
- `GET /api/status/platform` — Get platform status

## Related Modules
- [Database Services and Types](Database Services and Types.md)
