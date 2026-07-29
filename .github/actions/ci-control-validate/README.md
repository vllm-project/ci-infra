# CI control catalog validation action

This action compiles the protected vLLM policy and Buildkite job definitions
with the versioned catalog compiler in `services/ci-control`. It rejects
missing/duplicate keys, invalid selector metadata, aliases or tombstones,
unknown dependencies, dependency cycles, invalid routes/profiles, and
non-deterministic source values.

Pin cross-repository use to a full `ci-infra` commit:

```yaml
- uses: vllm-project/ci-infra/.github/actions/ci-control-validate@<full-sha>
  with:
    repository-root: .
    baseline-repository-root: .ci-control-baseline
```

When a baseline is supplied, removed job and area selectors require a reviewed
alias or tombstone. The first bootstrap is allowed when the baseline predates
`ci_control.toml`. The action is read-only and requires no GitHub or Buildkite
credentials.
