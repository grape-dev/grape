# Spec Generation Guide

## When to Generate
- Feature requests (type:feature)
- Enhancement requests (type:enhancement)
- When issue skill determines the issue requires implementation planning

## Spec Template

### Title
[Spec] <Issue Title>

### Sections

1. Requirement Description
   - Original issue summary
   - User story format: "As a <role>, I want <goal> so that <benefit>"
   - Acceptance criteria checklist

2. Technical Approach
   - Recommended solution
   - Alternative approaches considered
   - Architecture/design decisions

3. Scope
   - In-scope: What will be implemented
   - Out-of-scope: What will not be addressed

4. Dependencies
   - Libraries/modules required
   - External services/APIs needed
   - Pre-existing issues/PRs to reference

5. Testing Strategy
   - Unit tests needed
   - Integration tests needed
   - Manual verification steps

6. Risks
   - Technical risks
   - Timeline risks
   - Mitigation plans

## Output
Create the spec as a comment on the original issue with [Spec] prefix.