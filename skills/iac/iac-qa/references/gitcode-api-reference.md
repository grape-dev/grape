# GitCode API Reference (Shared)

## Base URL
https://api.gitcode.com/api/v5

## Authentication
Header: Authorization: token $BOT_GITCODE_TOKEN
Header: PRIVATE-TOKEN: $BOT_GITCODE_TOKEN

## Endpoints

### Repository
- GET /repos/:owner/:repo - Get repository info
- GET /repos/:owner/:repo/contents/:path - Get file contents

### Issues
- GET /repos/:owner/:repo/issues - List issues
- GET /repos/:owner/:repo/issues/:number - Get single issue
- POST /repos/:owner/:repo/issues - Create issue
- PATCH /repos/:owner/:repo/issues/:number - Update issue
- GET /repos/:owner/:repo/issues/:number/comments - List comments
- POST /repos/:owner/:repo/issues/:number/comments - Create comment

### Labels
- GET /repos/:owner/:repo/labels - List labels
- POST /repos/:owner/:repo/labels - Create label
- PATCH /repos/:owner/:repo/labels/:name - Update label
- DELETE /repos/:owner/:repo/labels/:name - Delete label

### Pull Requests
- GET /repos/:owner/:repo/pulls - List PRs
- GET /repos/:owner/:repo/pulls/:number - Get PR detail
- POST /repos/:owner/:repo/pulls - Create PR
- GET /repos/:owner/:repo/pulls/:number/comments - List PR comments
- POST /repos/:owner/:repo/pulls/:number/comments - Create PR comment
- GET /repos/:owner/:repo/pulls/:number/files - List PR changed files

### A2A
- POST https://example.com/api/a2a - Trigger agent-to-agent handoff

## Notes
- All endpoints use v5 API unless specified
- Pagination: ?page=1&per_page=100
- Rate limiting: 5000 requests/hour