// commitlint: conventional types only (baseline enum, no repo-local additions)
module.exports = {
  extends: ['@commitlint/config-conventional'],
  rules: {
    'type-enum': [
      2,
      'always',
      ['feat', 'fix', 'docs', 'style', 'refactor', 'test', 'chore', 'perf', 'ci', 'build', 'revert'],
    ],
    // NOTE: repo gate requires English subjects; no scope-language rule enforced here.
  },
}
