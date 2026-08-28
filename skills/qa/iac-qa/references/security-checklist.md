# Security Checklist

## Sensitive Information
- [ ] No hardcoded AK/SK/access keys
- [ ] No hardcoded passwords or tokens
- [ ] No database connection strings with credentials
- [ ] No private keys or certificates committed
- [ ] No API keys in code or config files
- [ ] No personal identifiable information (PII)

## Input Validation
- [ ] All user inputs are validated (type, length, range)
- [ ] SQL injection prevention (parameterized queries)
- [ ] Command injection prevention (no shell=True with user input)
- [ ] Path traversal prevention
- [ ] Cross-site scripting (XSS) prevention

## Dependency Security
- [ ] All dependencies are from trusted sources
- [ ] No known vulnerable versions
- [ ] Minimal dependency footprint
- [ ] License compatibility checked

## Infrastructure Security
- [ ] Security group rules are minimal (least privilege)
- [ ] Network ACLs are properly configured
- [ ] Encryption at rest is enabled
- [ ] Encryption in transit (TLS) is enforced
- [ ] IAM roles have minimal required permissions