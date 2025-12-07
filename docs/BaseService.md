# BaseService (Database Services)

## Overview

`BaseService` is an abstract class that provides foundational database access patterns, error handling, and connection management for all domain-specific database services. It is designed to be extended by concrete service classes that implement business logic for specific data domains.

## Core Responsibilities
- **Database Connection Management**: Initializes and manages connections to the database using the provided environment configuration.
- **Type-Safe Query Construction**: Offers utility methods for building type-safe SQL where conditions using Drizzle ORM.
- **Error Handling**: Centralizes error logging and propagation for all database operations, ensuring consistent diagnostics and error reporting.
- **Read Optimization**: Supports read-optimized database connections (e.g., using D1 read replicas) for scalable, low-latency queries.

## Key Methods and Properties

| Method/Property         | Description                                                                                 |
|------------------------|---------------------------------------------------------------------------------------------|
| `constructor(env)`     | Initializes the service with the given environment and database connection.                  |
| `buildWhereConditions` | Helper to combine multiple SQL conditions in a type-safe way.                                |
| `handleDatabaseError`  | Logs and throws errors encountered during database operations.                               |
| `database`             | Provides direct access to the underlying database connection for advanced queries.           |
| `getReadDb(strategy)`  | Returns a read-optimized database connection ("fast" or "fresh" strategies).               |
| `logger`               | Logger instance for the service, namespaced by class.                                       |

## Example Usage

```typescript
class AppService extends BaseService {
    async getAppById(appId: string) {
        try {
            return await this.database.query('SELECT * FROM apps WHERE id = ?', [appId]);
        } catch (error) {
            this.handleDatabaseError(error, 'getAppById', { appId });
        }
    }
}
```

## Dependencies
- **DatabaseService**: Handles the actual database connection and query execution (see implementation in the database layer).
- **Logger**: Used for error reporting (see [Logger.md]).
- **Drizzle ORM**: Used for type-safe SQL query construction.

## Related Modules
- [Database Services and Types.md] (module overview)
- [Logger.md] (logging utilities)
