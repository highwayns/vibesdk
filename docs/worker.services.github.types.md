# Types (worker.services.github.types)

## Overview

This sub-module defines all type interfaces, result objects, and error classes used by the GitHubService. These types standardize the data structures for repository management, token handling, and error reporting, ensuring consistency across the GitHub Integration module.

## Core Components

### GitHubServiceConfig
Configuration options for the GitHubService, such as OAuth client ID and secret.

```
interface GitHubServiceConfig {
    clientId?: string;
    clientSecret?: string;
}
```

### GitHubUserAccessToken
Represents an OAuth access token for a GitHub user, including optional refresh and expiry information.

```
interface GitHubUserAccessToken {
    access_token: string;
    token_type: string;
    scope: string;
    refresh_token?: string;
    expires_in?: number;
}
```

### GitHubTokenResult
Standard result object for token operations, including success status, token, expiry, and error message.

```
interface GitHubTokenResult {
    success: boolean;
    token?: string;
    expires_at?: string;
    error?: string;
}
```

### CreateRepositoryOptions & CreateRepositoryResult
Options and result types for repository creation operations.

```
interface CreateRepositoryOptions {
    name: string;
    description?: string;
    private: boolean;
    auto_init?: boolean;
    token: string;
}

interface CreateRepositoryResult {
    success: boolean;
    repository?: GitHubRepository;
    error?: string;
    alreadyExists?: boolean;
    repositoryName?: string;
}
```

### GitHubServiceError
Custom error class for GitHubService-specific errors, including error code and status.

```
class GitHubServiceError extends Error {
    constructor(
        message: string,
        code: string,
        statusCode?: number,
        originalError?: unknown
    )
}
```

## Additional Types
- **GitHubRepository**: Alias for Octokit's repository type.
- **GitHubUser**: Alias for Octokit's user type.
- **GitHubInstallation**: Alias for Octokit's installation type.
- **GitHubAppToken**: Alias for Octokit's app token type.
- **GitHubExportOptions / GitHubExportResult**: Options and result types for export operations.
- **GitHubTokenType**: Union type for token types ('installation', 'user_access', 'oauth').

## Usage
These types are used throughout the [GitHubService](worker.services.github.GitHubService.GitHubService.md) and are essential for all interactions with the GitHub API and repository management workflows.
