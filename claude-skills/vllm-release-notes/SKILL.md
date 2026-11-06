---
name: vllm-release-notes
description: Write the GitHub release notes and the Slack announcement for a new vLLM release. Use when the user asks to "make release notes for vX.Y.Z", "draft the vX.Y.Z release", or wants the Slack promotion message and install instructions for a release.
---

# vLLM Release Notes

Turn the commit range between two vLLM tags into the GitHub release body
(`3-highlights-note-edit.md`) and the Slack announcement
(`4-slack-announcement.md`). Finished examples for every release since v0.22.0
live in [examples/](examples/); read the newest one first for structure and
tone, and use [ref-past-release-notes-highlight.md](ref-past-release-notes-highlight.md)
for the older style and category reference.

## Prerequisites

- GitHub CLI (`gh`) authenticated as a person with read access to
  `vllm-project/vllm`. Run `unset GITHUB_TOKEN` first if a bot token is exported
  in the shell so `gh` uses the personal login.
- `uv` (the fetch script declares its own dependencies).
- Both tags pushed to `vllm-project/vllm`. Use the previous final release as the
  base tag, not a release candidate.

## Workspace layout

Work in a scratch directory (the maintainer uses a private `release-notes`
folder). Every release produces these files; archive them under `v<ver>/` when
the release is published so the next release starts clean:

| File | Purpose |
| --- | --- |
| `0-current-raw-commits.md` | Raw commit titles with PR numbers, one per line |
| `0-contributor-stats.md` | Commit and contributor counts, top and new contributors |
| `1-commit-analysis-draft.csv` | Per-commit classification: `title`, `pr number`, `user facing impact/summary`, `category`, `decision`, `reason` |
| `2-highlights-note-draft.md` | First full draft |
| `3-highlights-note-edit.md` | Final release body pasted into the GitHub release |
| `4-slack-announcement.md` | Slack main message and thread replies |
| `tmp/` | Helper scripts and fetched PR descriptions |

## Steps

1. **Fetch the commits and contributor stats.**

   ```bash
   uv run fetch_commits.py --base-tag v0.28.0 --head-tag v0.29.0 --output 0-current-raw-commits.md --stats
   ```

   The script reads `GITHUB_TOKEN` or `GH_TOKEN`; pass
   `GITHUB_TOKEN=$(gh auth token)` when neither is set. It writes both `0-*`
   files and prints the summary line.

2. **Read every commit title**, then fetch PR descriptions for anything that
   might reach the notes (new models, defaults, perf claims, breaking changes,
   security fixes). `gh api repos/vllm-project/vllm/pulls/<N> --jq .body` is
   enough; a loop over 300 to 400 PRs takes a few minutes. Take performance
   numbers only from PR descriptions, never from titles alone.

3. **Classify every commit** into `1-commit-analysis-draft.csv`. A rule-based
   first pass (title regexes for CI, docs, reverts, refactors, hardware
   vendors, spec decode, quantization, and so on) followed by hand-curated
   overrides works well; keep the overrides in `tmp/` so the CSV can be
   regenerated. Drop merged-then-reverted pairs entirely. Ignore CI, docs,
   tests, mypy, refactors, and benchmark-script-only changes unless they change
   user-visible behavior.

4. **Draft `2-highlights-note-draft.md`** with these sections, in this order:
   `## Highlights`, `## Release Artifacts`, `## Model Support`,
   `## Engine Core`, `## Hardware & Performance`, `## Large Scale Serving`,
   `## Quantization`, `## API & Frontend`, `## Security`, `## Dependencies`,
   `## Breaking Changes & Deprecations`, `## New Contributors`,
   `## Contributors`. Each section is a bulleted list; each bullet opens with a
   bold topic and cites PR numbers as `(#NNNNN)`. Highlights get 6 to 9 bullets:
   the biggest themes, new models, new defaults, and breaking changes.

5. **Edit into `3-highlights-note-edit.md`.** Re-check every claim against the
   PR descriptions and the CSV, then verify that every cited PR number appears
   in `0-current-raw-commits.md`. Do not leave open questions or uncertainties
   in the file; resolve them or drop the item.

6. **Summary line.** The first line under Highlights is
   `This release features X commits from Y contributors (Z new)!`, using the
   numbers from `0-contributor-stats.md`.

7. **Contributors.** Append `## New Contributors` (one line per person:
   `* @user made their first contribution in https://github.com/vllm-project/vllm/pull/N`)
   when there are any, then `## Contributors` as a single line of `@handle`
   mentions separated by `, ` covering everyone in the "All Contributors" table.

8. **Release Artifacts section**, placed right after Highlights. Copy it from
   the previous release's example and bump every version string. Three
   subsections:
   - `### Python Wheels` table (Platform | Install): PyPI `pip install vllm`,
     uv `uv pip install vllm --torch-backend=auto`, ROCm
     `pip install vllm --extra-index-url https://wheels.vllm.ai/rocm/<ver>/<rocmNNN>`,
     XPU `uv pip install vllm --extra-index-url https://wheels.vllm.ai/<ver>/xpu --extra-index-url https://download.pytorch.org/whl/xpu --index-strategy unsafe-best-match`.
   - `### Docker Images` table (Platform | Docker Image): `vllm/vllm-openai:v<ver>`
     (default CUDA 13.0, `-cu130` alias), `-cu129`, `-ubuntu2404`,
     `-cu129-ubuntu2404`, `vllm/vllm-openai-rocm`, `vllm/vllm-openai-cpu`,
     `vllm/vllm-openai-xpu`.
   - `### Other Artifacts`: pointer to the release Assets (sdist, CUDA 12.9 and
     13.0 wheels for x86_64 and arm64, CPU wheels for x86_64, arm64 and macOS,
     XPU wheel).

   Verify every URL and tag; they change per release:
   - ROCm subdir: list `https://wheels.vllm.ai/rocm/<ver>/` (v0.28.0 was
     `rocm722`, v0.29.0 was `rocm723`).
   - XPU index: `https://wheels.vllm.ai/<ver>/` should list `xpu/`.
   - Docker tags: `https://hub.docker.com/v2/repositories/vllm/<repo>/tags?name=v<ver>`.
   - Assets: `gh api repos/vllm-project/vllm/releases/tags/v<ver> --jq '.assets[].name'`.

9. **No tildes.** GitHub renders paired `~` as strikethrough. Write "about 5%"
   and "6.6-7.6x", never "~5%" or "6.6~7.6x". Run `grep -n '~' 3-highlights-note-edit.md`
   and fix every hit before finishing.

10. **Slack announcement** in `4-slack-announcement.md`, and paste both messages
    into the final reply to the user.
    - Main message, exact template (3 to 5 highlights):

      ```
      Hi all,
      We just finished vLLM v<ver> release today. :rocket: This release features <X> commits from <Y> contributors. We also welcomed <Z> new contributors to vLLM!
      A few highlights of this release: <highlight 1>, <highlight 2>, <highlight 3>, and more!
      More details and install instructions in :thread:

      Thank you everyone for your contributions to the new version of :vllm: :pray:
      ```

    - Thread reply with install instructions: Python wheels (PyPI `pip`,
      "(Preferred)" uv `--torch-backend=auto`, ROCm index, XPU index), Docker
      images (say that `latest` also works in place of the version tag; list
      default, cu129, ubuntu2404, cu129-ubuntu2404, rocm, cpu, xpu), and the
      GitHub release URL followed by the asset types. Use exactly the URLs and
      tags verified in step 8. Use literal bullet glyphs so Slack shows dots:
      `•` at level 1, `◦` at level 2 with a 4-space indent, `▪` at level 3 with
      an 8-space indent. Slack counts indentation in units of 4 spaces when
      parsing a paste, so 3-space indents flatten the list.
    - Optionally a short thread reply listing notable breaking changes.

See [examples/v0.29.0/](examples/v0.29.0/) for a complete release body and
Slack announcement produced with these steps.

## Judgment

The goal is to miss no important change without listing every commit. Perf
claims need a number and a source PR. New defaults and anything that changes
behavior for existing deployments go in both Highlights and Breaking Changes.
When a feature landed and was reverted inside the range, it did not ship; leave
it out.
