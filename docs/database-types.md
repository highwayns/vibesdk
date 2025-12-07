# Database Types

## Overview

This file defines all shared TypeScript interfaces and types used for database operations throughout the system. These types ensure consistency, type safety, and clear contracts for data exchanged between services, controllers, and clients.

## Core Components

### 1. Query and Pagination Types
- **PaginationInfo**: Standard structure for paginated results.
- **PaginatedResult<T>**: Generic wrapper for paginated data.
- **PaginationParams**: Input parameters for pagination.
- **BaseAppQueryOptions**: Common filters and pagination for app queries.
- **AppQueryOptions / PublicAppQueryOptions**: User-specific and public query options.

### 2. App, User, Team, and Board Types
- **AppForForkResult**: App data with fork permission check.
- **SimpleAppCreation**: Minimal data required to create an app.
- **EnhancedAppData / AppWithFavoriteStatus**: App data with user and social stats.
- **TeamStats / BoardStats**: Aggregated statistics for teams and boards.
- **UserStats / UserActivity**: User analytics and activity timeline.

### 3. Analytics and Health Types
- **AppStats / BatchAppStats**: App-level and batch statistics.
- **HealthStatusResult**: Health check result for services.

### 4. Error Handling and Operation Results
- **DatabaseError**: Structured error for database operations.
- **OperationResult<T>**: Wrapper for operation results with error handling.
- **ErrorWithMessage / isErrorWithMessage**: Utility for error type guards.

### 5. Secrets and Model Config Types
- **SecretData / EncryptedSecret**: Types for secret storage and retrieval.
- **UserModelConfigWithMetadata**: Model config with user override metadata.
- **ModelTestRequest / ModelTestResult**: Types for model testing and validation.

## Example: Paginated App Query

```typescript
const queryOptions: BaseAppQueryOptions = {
    framework: 'react',
    sort: 'popular',
    limit: 20,
    offset: 0,
};

// Used by a service to fetch paginated apps
const result: PaginatedResult<EnhancedAppData> = await appService.getApps(queryOptions);
```

## Usage
- Used by API Controllers for request/response validation ([API Controllers.md])
- Consumed by Agent Core for analytics and state management ([Agent Core.md])
- Shared with frontend and other backend modules for consistent data contracts

## Related Modules
- [Database Services and Types.md] (module overview)
- [API Controllers.md]
- [Agent Core.md]
