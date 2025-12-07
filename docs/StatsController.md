# StatsController

## Purpose
Provides endpoints for retrieving user statistics and activity timelines.

## Core Components
- StatsController (class)

## Responsibilities
- Fetch user statistics (e.g., usage, activity)
- Fetch user activity timeline (recent actions)

## Key Interactions
- **AnalyticsService**: Retrieves user stats and activity ([Database Services and Types](Database Services and Types.md))

## Data Flow
```mermaid
sequenceDiagram
    participant Client
    participant StatsController
    participant AnalyticsService

    Client->>StatsController: GET /api/stats/user
    StatsController->>AnalyticsService: getUserStats
    AnalyticsService-->>StatsController: Stats
    StatsController-->>Client: Stats response

    Client->>StatsController: GET /api/stats/user-activity
    StatsController->>AnalyticsService: getUserActivityTimeline
    AnalyticsService-->>StatsController: Activity
    StatsController-->>Client: Activity response
```

## Endpoints
- `GET /api/stats/user` — Get user stats
- `GET /api/stats/user-activity` — Get user activity timeline

## Related Modules
- [Database Services and Types](Database Services and Types.md)
