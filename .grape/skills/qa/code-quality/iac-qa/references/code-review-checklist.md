# Code Review Checklist

## Code Quality
- [ ] Follows project coding style and conventions
- [ ] No dead code or commented-out code
- [ ] Proper error handling and logging
- [ ] No hardcoded sensitive values (AK/SK/passwords)
- [ ] Variable and function names are descriptive
- [ ] Code complexity is manageable
- [ ] No code duplication

## Functional Correctness
- [ ] Implements all acceptance criteria from the spec
- [ ] Edge cases are handled (empty input, null, boundary values)
- [ ] Error paths are properly managed
- [ ] No regression on existing features

## Testing
- [ ] Unit tests cover new code
- [ ] Tests are meaningful (test behavior, not implementation)
- [ ] Edge cases have test coverage
- [ ] Tests pass locally

## Security
- [ ] No secrets/tokens in code or config files
- [ ] Input validation is in place
- [ ] No command injection vulnerabilities
- [ ] Dependency versions are up to date

## Documentation
- [ ] Inline comments for non-obvious logic
- [ ] Public APIs have docstrings
- [ ] CHANGELOG or release notes updated if needed