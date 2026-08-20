# Build & Debug Checklist

## Pre-Diagnosis
- [ ] Full error log collected (no truncation)
- [ ] Error type classified: compile/link/dependency/test/runtime
- [ ] Error frequency established (first occurrence vs recurring)
- [ ] Past Issue/PR search conducted for similar error patterns
- [ ] Environment version info gathered (OS, runtime, toolchain)

## Build Diagnostics
- [ ] Compiler/linter output parsed for error locations
- [ ] Missing dependencies identified
- [ ] Version conflicts checked (dependency tree)
- [ ] Platform-specific issues considered (OS, architecture)
- [ ] Incremental build vs clean build compared

## Dependency Management
- [ ] Lock file vs manifest file version mismatch checked
- [ ] Direct and transitive dependency versions verified
- [ ] Deprecated or EOL dependencies flagged
- [ ] Compatible version ranges confirmed

## Test Failure Analysis
- [ ] Test report parsed (pass/fail/skip counts)
- [ ] Failure stack trace analyzed for root cause
- [ ] Test environment vs CI environment differences noted
- [ ] Flaky test patterns identified (timing, race conditions)
- [ ] Code change diff correlated with failing tests

## Runtime Problem Localization
- [ ] Application logs examined (error level)
- [ ] Stack trace unwound to origin
- [ ] Recent code changes reviewed for introducing commit
- [ ] Resource constraints checked (memory/CPU/disk/network)
- [ ] Configuration drift between environments considered

## Output Requirements
- Root cause clearly stated
- Reproduction steps documented
- Fix recommendation with priority level
- Workaround provided if available
- Declare "past Issue/PR search conducted: <pattern keywords>"