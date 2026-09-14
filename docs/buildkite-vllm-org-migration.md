# Buildkite migration: `tpu-commons` → `vllm`

Moving CI/CD for `vllm-project/tpu-inference` and `vllm-project/vllm-torchtpu` out of the
`tpu-commons` Buildkite org and into the `vllm` org, onto cluster
[`0219f117-7dc6-4a04-aee2-1619736fd800`](https://buildkite.com/organizations/vllm/clusters/0219f117-7dc6-4a04-aee2-1619736fd800/queues) ("TPU").

**Audience:** engineers working on `tpu-inference` / `vllm-torchtpu`, and PMs tracking the rollout.
Sections 1 and 3 are the PM view; section 5 is what engineers actually need.

**Last updated:** 2026-09-03.

**Status legend:** ✅ done · 🔄 in progress · ⬜ not started

---

## 1. Summary

Today TPU CI lives in a Buildkite organization (`tpu-commons`) that is separate from the one the
rest of vLLM uses. That means separate billing and a second org to maintain.

**The headline for planning purposes:**

- **One cutover window: Friday 2026-09-04, 13:00 PT.** Budget **up to ~4 hours** of no CI. We
  expect materially less, but the window is deliberately generous so the work is not rushed.
- Everything before that window is *capacity reduction*, not outage — CI keeps working throughout.
- Pipeline names, queue names, and PR status checks are **deliberately unchanged**, so branch
  protection and developer muscle memory survive the move.
- Rollback is a single lever right up to cleanup.

**Where we are:** phases 0–2 are complete, and the new org has run a full green validation build on
real TPU hardware, with results landing in the new test-analytics suite. The v6e reservation move
landed on 2026-08-28 (see §2.4). Remaining: final pre-cutover checks, then the cutover itself.

---

## 2. What is changing

### 2.1 The approach: prove it on a v6e slice, then cut over in one window

![Migration approach](images/migration-approach.png)

We did **not** try to migrate fleet-by-fleet over weeks. A small v6e slice was carved out of the
existing reservation and pointed at the new org, so we could run genuine builds on real chips before
committing. That slice is green. Everything else now moves in a single announced window.

Why one cutover rather than a gradual one:

- **Capacity cannot be split.** Both orgs draw from the same fixed reservation. Every chip parked in
  the new org is a chip missing from the old one, so a long parallel period means a long period of
  degraded CI for everybody. v7x in particular is far too scarce to run two partial fleets.
- **The risk was already retired.** The v6e slice exercised the whole chain end to end — agent token,
  registration, clone via the CI bot App, cluster secrets, test execution, analytics upload. What
  remains is mechanical repetition on other accelerator types.
- **One window is easier to communicate** than a fortnight of "which org am I in today?"

The cost is a real, announced downtime window instead of a near-invisible flip. That is the
deliberate trade.

### 2.2 What engineers will and won't notice

| Thing | Changes? | Detail |
|---|---|---|
| Pipeline slugs | **No** | `tpu-inference-ci` stays `tpu-inference-ci` |
| PR status check names | **No** | Contexts are `buildkite/<slug>/<step>` — org is not part of the name, so branch protection rules need no edit |
| Queue names in pipeline YAML | **No** | `tpu_v6e_queue` etc. are identical in both orgs. **Zero pipeline-YAML changes for queues** (~444 `queue:` references left alone) |
| Test analytics — `tpu-inference` | **No** | The suite is chosen by the analytics **token**, not by any name in the pipeline YAML, so there is nothing to edit. See step 20 |
| Test analytics — `vllm-torchtpu` | **Yes** | Different mechanism: the `tests` plugin with OIDC and a hardcoded `suite-slug`. Needs an OIDC policy (step 21) and a one-line PR merged inside the window (steps 22 and 35) |
| Machine images / test behaviour | **No** | Same Terraform modules, same startup scripts. The only per-VM difference is which agent token it registers with |
| Build URLs | **Yes** | `buildkite.com/tpu-commons/...` → `buildkite.com/vllm/...` |
| Where you log in | **Yes** | You need membership in the `vllm` Buildkite org, team `google-tpu`. See Appendix A |
| Test-analytics *history* | **Yes** | New suite `vllm/tpu-ci`, starting from empty. Historical trends stay in the old suite `tpu-commons/tests` and are not migrated |

### 2.3 Under the hood

| Component | Before | After |
|---|---|---|
| Buildkite org | `tpu-commons` | `vllm` |
| Cluster | `3bae784e` ("CI") | `0219f117` ("TPU") |
| Agent token | `tpu_commons_buildkite_agent_token` | `vllm_buildkite_agent_token` |
| Analytics token | `tpu_commons_buildkite_analytics_token` (suite `tests`) | `vllm_buildkite_analytics_token` (suite `tpu-ci`) |
| GitHub auth | CI bot App `app_id 4156238`, installation `142868369` | **unchanged** — the App authenticates to GitHub, not Buildkite, so it is Buildkite org-independent |
| Pipelines | 11 | 11, identical slugs, team `google-tpu` |
| Schedules | 11 | 11 |
| Access model | one default `everyone` team, 99 members | team `google-tpu` — see Appendix A |

### 2.4 The reservation move is a separate step, done first

Two different moves were in flight and they were deliberately **split into two steps** rather than
attempted together:

| | What moves | When | Status |
|---|---|---|---|
| **Step A — reservation** | v6e TPUs change *GCP project and zone*, into `cloud-ullm-inference-ci-cd` / `us-east5-a` | Thu **2026-08-28** | ✅ done |
| **Step B — Buildkite org** | those same TPUs change *Buildkite org*, `tpu-commons` → `vllm` | Fri **2026-09-04** | ⬜ this document |

They are independent levers: the project is fixed by the Terraform provider alias, the org by the
agent token. Doing them in one window would have meant debugging a capacity problem and an org
problem at the same time, on a clock. So step A ran a week early, on its own, and the v6e fleet is
now sitting in its final project **still registered to `tpu-commons`** — exactly as intended.

What that leaves for the window: 20 × v6e-1 and 7 × v6e-8 need a token flip and a rolling restart,
the same treatment as v7x and CPU. See step 31a.

> **Consequence for capacity.** The new reservation is **128 ct6e, shared with the benchmark fleet
> at exactly zero headroom** (CI 94, benchmarks 34 — becoming 102 / 26 once step 45 applies). Two
> things follow: any v6e roll in
> `us-east5-a` must destroy before it creates, and growing a CI v6e fleet requires the benchmark
> fleet to give chips back — which is how the 9th v6e-8 is being funded. See step 45.

---

## 3. Timeline

| Phase | What | Target | Status | Capacity impact |
|---|---|---|---|---|
| **0** | Provision the target cluster: queues, secrets, pipelines, schedules, team | 2026-08-25 | ✅ done | none — additive |
| **1** | CPU fleets (`cpu`, `cpu_64_core`) into the vllm org | 2026-08-26 | ✅ done | none — net-new capacity |
| **2** | v6e confidence slice (v6e-1 and v6e-8) | 2026-08-26 | ✅ done | v6e-1 at 67% for ~1 h |
| **2.5** | **Reservation move** — v6e into `cloud-ullm-inference-ci-cd` / `us-east5-a` (§2.4) | 2026-08-28 | ✅ done | v6e down for the apply; no org change |
| **3** | Verification and prerequisites: 3 nightly cycles, notifications, access, visibility | 2026-08-27 → 09-04 | 🔄 in progress | none |
| **4** | **Full cutover** — v7x, remaining v6e, CPU, monitoring, traffic | **Fri 2026-09-04, 13:00 PT** | ⬜ | **planned downtime, budget ~4 h** |
| **5** | Cleanup: delete old pipelines, webhooks, stale state | from 2026-09-11 (after a green week) | ⬜ | none |

**The only planned downtime is phase 4.** Phase 3 is a hard gate: if the nightlies are not green, or
Slack notifications are unverified, the window slips rather than the checks being skipped.

> **Note on the schedule.** A Friday window gives phase 3 eight nightly cycles of runway (nights of
> Aug 27 → Sep 3) against a gate of three consecutive green ones, so a single bad night no longer
> threatens the date. Starting at 13:00 keeps the work inside the working day — everyone who might
> be needed is still around — while the outage lands on the quietest afternoon of the week and the
> weekend that follows is unpressured buffer if something needs unpicking. If the gate is not met
> by Friday morning, the window slips a week rather than the checks being skipped.

---

## 4. The steps

Every step required to complete the migration, in order. Completed ones are marked.

### Phase 0 — target cluster ✅

1. ✅ Create cluster `0219f117` ("TPU") in the `vllm` org.
2. ✅ Create 8 queues with names **identical** to the tpu-commons side.
3. ✅ Create agent token `tpu-ci-fleet`; mirror it into GCP Secret Manager as
   `vllm_buildkite_agent_token` in both the `cloud-tpu-inference-test` and
   `cloud-ullm-inference-ci-cd` projects.
4. ✅ Port the 6 cluster secrets by hand (values never printed; verified by byte count):

   | Secret | Source | Notes |
   |---|---|---|
   | `HF_TOKEN` | `tpu_commons_buildkite_hf_token` | |
   | `GITHUB_CI_BOT_APP_ID` | `tpu_commons_github_ci_bot_app_id` | |
   | `GITHUB_CI_BOT_PEM` | `tpu_commons_github_ci_bot_pem` | **load-bearing** — see below |
   | `ANALYTICS_TOKEN` | new suite `vllm/tpu-ci` | |
   | `DOCKERHUB_TOKEN` | `vllm_buildkite_dockerhub_token` | |
   | `GITHUB_PAT` | `vllm_buildkite_github_pat` | to be retired, step 26 |

   `GITHUB_DEPLOY_KEY` was deliberately not ported — it has zero consumers.

   > **`GITHUB_CI_BOT_PEM` is load-bearing.** Every agent runs
   > `buildkite-agent secret get GITHUB_CI_BOT_PEM` at boot and installs a system-wide git
   > credential helper from it. If it is missing or wrong, **every clone on every agent fails.**

5. ✅ Create all 11 pipelines with identical slugs and byte-identical configuration, assigned to
   team `google-tpu` (pipeline creation fails with `422 teams` without a team).
6. ✅ Set every new pipeline to `trigger_mode: none` and `publish_commit_status: false`.

   > **Why commit status must stay off.** The status context is `buildkite/<pipeline-slug>[/<step>]`
   > — the org is *not* part of it. With identical slugs in both orgs, two pipelines publishing at
   > once overwrite each other's status on the same commit and `tpu-inference` branch protection
   > flaps. This stays `false` until phase 4 switches both orgs in one window.

7. ✅ Create all 11 schedules, **disabled**.

### Phase 1 — CPU fleets ✅

8. ✅ Add an agent watchdog (`keep-agent-connected.sh`) that restarts an agent that stops reporting
   healthy, and stop agents accepting jobs mid-provisioning.
9. ✅ Introduce a `purpose` Terraform variable so new-org fleets get a self-describing name shared by
   the TPU VM, its disk, its label, and the Buildkite agent
   (`<accelerator_type>-ci-<purpose>-<index>-<project_short_name>-<zone>`, e.g.
   `v6e-1-ci-vllm-3-test-us-east5-a`). Existing fleets keep their original names, so they are not
   renamed — and therefore not destroyed and rebuilt — by this change.
10. ✅ Stand up vllm-org CPU fleets in `cloud-ullm-inference-ci-cd`:
    `ci_cpu_vllm_zone_b` (8 → `cpu`), `ci_cpu_64_core_vllm_zone_b` (4) and
    `ci_cpu_64_core_vllm_zone_f` (4) (→ `cpu_64_core`).
11. ✅ Verify agent counts per queue against Terraform `instance_count` after the roll.

    > Do this after **every** roll. On the phase-1 roll, 3 of ~86 rebuilt VMs came up with no agent
    > at all because the startup script raced `unattended-upgrades` for the dpkg lock. It is silent
    > — the only way to catch it is to count.

### Phase 2 — v6e confidence slice ✅

12. ✅ Shrink `ci_v6e_8` 9 → 6 in `cloud-tpu-inference-test`.
13. ✅ Create `ci_v6e_8_vllm` (2, `southamerica-west1-a`).
14. ✅ Pause the 10 departing `v6e-1` agents in Buildkite (server-side, 24 h timeout) so they finish
    their current job and accept nothing new.

    > Pausing is the right lever here. A graceful stop over SSH would be undone by the agent unit's
    > `Restart=always` and by the watchdog; a Buildkite-side pause cannot be. **Zero jobs were
    > interrupted** — the drain took ~90 minutes for the last long test to land.

15. ✅ Shrink `ci_v6e_1` 30 → 20.
16. ✅ Create `ci_v6e_1_vllm` (10, `us-east5-a`).
17. ✅ Confirm all 12 new agents registered, and that a full untargeted `terraform plan` reports
    `No changes`.
18. ✅ Run a real validation build in the new org (`vllm/tpu-inference-ci` #1) and confirm the whole
    chain end to end: token → agent → clone via CI bot App → cluster secrets → test execution.

### Phase 3 — verification and prerequisites 🔄

19. ✅ Enable **one** schedule in the vllm org: `tpu-inference-ci` nightly (`0 0 * * *`
    America/Los_Angeles). Enabled 2026-08-28; first fire 08-29 00:00 PT. `trigger_mode` stays
    `none` — scheduled builds do not depend on it. The two `MODEL_IMPL_TYPE` variants stay disabled.

    > **Expect v7x steps to fail until the window.** There are no v7x agents in the vllm org, so
    > every `tpu_v7x_*_queue` step in the nightly will sit unscheduled or time out. That is
    > expected and does **not** break the step-24 gate — judge the gate on the v6e and CPU steps.

19a. ✅ **Move the nightly image build early.** `vllm-torchtpu-nightly-image` is a good candidate to
    move ahead of the window: it is schedule-only, publishes no commit status, and its two steps run
    on `cpu` and `cpu_64_core`, both of which are at full strength in the vllm org. Enabled in
    `vllm` and **disabled in `tpu-commons`** on 2026-08-28.

    > **These two had to move together, not in stages.** Both push the *same* tags
    > (`us-central1-docker.pkg.dev/cloud-ullm-inference-ci-cd/vllm-torchtpu/torchtpu-vllm-<target>`
    > at `:<date>` and `:latest`). Leaving both enabled would have raced two builds onto identical
    > tags. Both fleets live in the same GCP project, so the pushing service account is unchanged.

    Once it has been green for a few nights, the tpu-commons copy can be deleted outright rather
    than just disabled (step 41).
20. ✅ Confirm `tpu-inference` tests upload to the new analytics suite `vllm/tpu-ci`. **Verified** —
    validation build #1 produced a finished run in that suite on 2026-08-26.

    > **`tpu-inference` needs no suite-name change.** Its suite is selected entirely by
    > `BUILDKITE_ANALYTICS_TOKEN`, which Terraform injects into `/etc/environment` per fleet at VM
    > provision time from the GCP secret `vllm_buildkite_analytics_token`. No suite name appears in
    > its pipeline YAML, so the behaviour is already conditional by fleet — old agents keep writing
    > to `tpu-commons/tests`, new agents write to `vllm/tpu-ci`, with no config change and no
    > cutover coupling. **This does not apply to `vllm-torchtpu` — see step 21.**

21. ✅ Add an OIDC policy to `vllm/tpu-ci` permitting `organization_slug: vllm` /
    `pipeline_slug: vllm-torchtpu-ci`, with the same five scopes the tpu-commons policy grants
    (`read_suites`, `read_test_plan`, `write_test_plan`, `write_uploads`, `write_test_ownerships`).
    Applied and verified 2026-08-26; the suite previously had `oidc_policy: null`.

    > **`vllm-torchtpu` uploads by a completely different route.** It uses the `tests#v1.0.0` plugin
    > with `suite-slug: tests` and `oidc-lifetime: 7200`
    > (`vllm-torchtpu/.buildkite/pipeline_tests.yml`), authenticating by OIDC rather than by a token.
    > Two things therefore break at cutover and both need fixing: the target suite must accept an
    > OIDC token whose `organization_slug` claim is `vllm` (this step), and the slug itself must
    > resolve — `vllm/tests` does not exist (step 22).
    >
    > `tpu-inference-ci` is deliberately **not** in the new policy: it authenticates by token, so
    > granting it OIDC scopes it does not use would be a gratuitous permission in a shared org.

22. ✅ Prepare — but **do not merge** — the one-line `vllm-torchtpu` PR changing `suite-slug: tests`
    → `tpu-ci`. Open as
    [vllm-torchtpu #671](https://github.com/vllm-project/vllm-torchtpu/pull/671), held as a draft
    titled `[DO NOT MERGE UNTIL 2026-09-04]`. It cannot land early: while builds still run in `tpu-commons` it would resolve to
    `tpu-commons/tpu-ci`, which does not exist, and torchtpu test uploads would break before the
    cutover. It merges inside the window at step 35.

23. ⬜ Confirm a deliberate failure delivers **both** the email and the Slack notification.

    > This is the only check on Slack wiring. The pipeline emits
    > `slack: "vllm#tpu-ci-notifications"`, where `vllm` is the **Slack workspace name**, not the
    > Buildkite org — so the string itself needs no edit. But the vllm Buildkite org must have that
    > workspace connected under Organization Settings → Notification Services. There is no API to
    > check this, and if it is missing, **delivery fails silently.** Do not cut over until a real
    > failure has produced a real Slack message.

24. ⬜ Hold for **3 consecutive** nightly cycles green before proceeding. The schedule was enabled on
    2026-08-28, so seven fires are available (00:00 PT on Aug 29, 30, 31 and Sep 1, 2, 3, 4 — the
    last lands 13 h before the window). The gate is three in a row ending no later than the night
    of 09-04, not the first three nights. Judge it on the v6e and CPU steps; v7x steps cannot pass
    yet (step 19).
25. ⬜ An **org admin** flips `tpu-inference-ci` and `tpu-vllm-integration` to **public**.
    Cluster-maintainer permission is not sufficient. They are private in the vllm org today.
26. 🔄 Merged [tpu-inference PR #3054](https://github.com/vllm-project/tpu-inference/pull/3054)
    on 2026-08-25, which removes every `GITHUB_PAT` reference and switches the three push-back steps
    (`commit_support_matrices.sh`, `commit_verified_commit_hashes.sh`, `update_lkg_version.sh`) to a
    bare `git push origin` authenticated by the CI bot App. ⬜ **Still outstanding:** delete the
    `GITHUB_PAT` cluster secret from **both** orgs — it is still present in the vllm cluster.
    Confirm `vllm-torchtpu` has no remaining reference before deleting.

    > The first real exercise of the App-credential push is the next nightly on `main` — pre-merge
    > CI does not cover these steps.

27. ⬜ **Grant access.** Ask a `vllm` org admin to invite the 88 people who are not yet in the org and
    add all 99 to team `google-tpu`. See **Appendix A**. This must land before the window, or people
    lose visibility into their own CI the moment traffic moves.
28. ✅ Confirm the live v7x inventory before starting. **Resolved 2026-08-28: the
    `cloud-tpu-inference-test-v7x` root is phantom.** It declares `ci_v7x_2 = 8` and `ci_v7x_8 = 8`,
    but `cloud-tpu-inference-test` has **zero** CI-named TPUs in any zone. All 36 v7x CI nodes
    (16 + 18 + 2) are in `cloud-ullm-inference-ci-cd`, matching the tpu-commons agent census
    exactly. Nothing in that root has to be rolled — two rows come off step 30. Check its remote
    state for leftover resources before deleting the root in phase 5.
29. ⬜ Announce the window to both repos' contributors, at least 24 h ahead.

> **Prerequisite — the v6e reservation in `cloud-ullm-inference-ci-cd`. ✅ Satisfied 2026-08-28.**
> `cloudtpu-20260828173000-731402396`, **128 ct6e** in `us-east5-a`, `READY` and at **128/128**.
> The whole v6e fleet already sits on it ([ci-infra #487](https://github.com/vllm-project/ci-infra/pull/487),
> merged `65f2d7e`), together with the benchmark fleet
> ([bm-infra #291](https://github.com/QiliangCui/bm-infra/pull/291), merged `80a2521`). The old
> reservation in `cloud-tpu-inference-test` is gone and that project holds no v6e capacity.
>
> This was **step A** of the two-step split described in §2.4 — it is no longer a blocker on the
> window, and no longer competes with it for time.



### Phase 4 — full cutover ⬜ — Fri 2026-09-04, 13:00 PT

Everything below happens inside the one window, so commit statuses never overlap between orgs.

30. ⬜ Flip `buildkite_token_value` to the vllm token and roll the v7x modules, one at a time. Every
    one of them is in `cloud-ullm-inference-ci-cd` — see step 28 on why `cloud-tpu-inference-test-v7x`
    is not in this list.

    | Module | Root | Declared |
    |---|---|---|
    | `ci_v7x_2` | `cloud-ullm-inference-ci-cd` | 16 |
    | `ci_v7x_8` | `cloud-ullm-inference-ci-cd` | 18 |
    | `ci_v7x_16` | `cloud-ullm-inference-ci-cd` | 2 |

    Use `./scripts/rolling_restart.py -m module.<name> -b 4 --delay 120 --dir <root>`.

    > **No headroom in `us-central1-c`.** That zone's `tpu7x` reservation is at **128/128 chips**
    > (`cloudtpu-20251114223000-2002888989`). Any step that creates a v7x node before releasing one
    > will fail on reservation capacity, so roll destroy-then-create and never overlap batches.

    **Recreate, not a process restart.** Both tokens reach a v7x host only through
    `metadata.startup-script`, which writes `token=` into
    `/etc/buildkite-agent/buildkite-agent.cfg` and `BUILDKITE_ANALYTICS_TOKEN` into
    `/etc/buildkite-agent/hooks/environment`. A `systemctl restart buildkite-agent` re-reads the
    *existing* cfg and reconnects to tpu-commons, so it does nothing on its own — the cfg is only
    rewritten when the startup script runs again. Recreating is also the only clean option for
    `ci_v7x_16`, which is multi-host (`is_multi_host = tpu_core_size > 8`) and therefore one
    Terraform resource spanning both workers.

    `rolling_restart.py` is the right tool and needs no separate apply first: it targets the
    resources it replaces, so the new token goes in as each node is created. Two defaults matter —
    `-replace` is destroy-before-create (the module declares no `create_before_destroy`), which is
    what the 128/128 reservation requires; and `--replace-disks` is **off**, so
    `/mnt/disks/persist` survives. That disk is Docker's `data-root`, so the image cache is kept
    and the first builds after the window are not cold.

    > **Budget the time, not the risk.** Every create re-runs the whole startup script — apt, the
    > agent install, the Ops Agent, and a `cargo install minijinja-cli` that compiles from source.
    > Across 36 v7x nodes this, not reservation contention, is what fills the window. Time one
    > batch before settling on a batch size.

    > **Watch `tpu7x-8-ci-4`.** `ci_v7x_8` sets `disk_size = 4096`, and the script exits with the
    > agent left masked if `/mnt/disks/persist` fails to mount. That host is already running Docker
    > off the boot disk, so its mount is suspect; the recreate will either fix it with a fresh
    > format or make the failure loud. Check it specifically once the roll finishes.
    > `tpu7x-16-ci-0/1` having no persist disk is by design — `ci_v7x_16` leaves `disk_size` at 0.

    No draining is needed. The window is announced downtime and the pipelines are paused, so kill
    in-flight jobs rather than waiting them out.

31. ⬜ Move the remaining tpu-commons CPU capacity the same way: `ci_cpu` (8, root
    `cloud-tpu-inference-test` — note this is a *different* root from every other module here, so it
    needs its own `init`/`apply`) and `ci_cpu_64_core_zone_c` (8, root
    `cloud-ullm-inference-ci-cd`), then collapse per step 31b. The vllm-side capacity these retire
    into was already added on 09-03 for `cpu`; see 31c for why `cpu_64_core` was not.
31a. ⬜ **v6e rolls like everything else now.** The relocation already happened as step A (§2.4), so
    there is nothing to move — the fleet is in its final project and only needs the token flip:

    | Module | Root | Declared | Token today |
    |---|---|---|---|
    | `ci_v6e_1` | `cloud-ullm-inference-ci-cd` | 20 | `tpu_commons_buildkite_agent_token` |
    | `ci_v6e_8` | `cloud-ullm-inference-ci-cd` | 7 | `tpu_commons_buildkite_agent_token` |

    > `ci_v6e_8` is declared 7 as of [#494](https://github.com/vllm-project/ci-infra/pull/494)
    > ("take the 9th v6e-8 back, funded by the benchmark fleet") but **only 6 nodes exist** — #494 is
    > merged and unapplied. Either apply it before the window or the collapsed fleet lands at 8, not
    > 9. Do not read the live count as the target.

    `ci_v6e_1_vllm` (10) and `ci_v6e_8_vllm` (2) are already on the vllm token and are untouched.

    > **No headroom in `us-east5-a` either.** That reservation is at **128/128** and shared with the
    > benchmark fleet (§2.4), so the same rule as v7x applies: destroy-then-create, never overlap
    > batches. `rolling_restart.py` with `-b 4` is safe; anything that creates first is not.

31b. ⬜ **Collapse the duplicated fleets — keep only the `*_vllm` module, and give it all the
    capacity.** Today every v6e and CPU queue is served by a tpu-commons module *and* a vllm module
    running side by side. That pairing exists only to keep both orgs alive during the overlap; once
    traffic is on vllm it is pure duplication. Retire the tpu-commons half and raise the vllm half's
    `instance_count` to the full total, so **vllm ends up with 100% of the capacity**:

    | Queue | Keep | Retire | `instance_count` |
    |---|---|---|---|
    | `tpu_v6e_queue` | `ci_v6e_1_vllm` | `ci_v6e_1` | 10 → **30** |
    | `tpu_v6e_8_queue` | `ci_v6e_8_vllm` | `ci_v6e_8` | 2 → **9** (7 + 2, per #494) |
    | `cpu` | `ci_cpu_vllm_zone_b` | `ci_cpu` | **8, unchanged** — already at parity |
    | `cpu_64_core` | `ci_cpu_64_core_vllm_zone_{b,f}` | `ci_cpu_64_core_zone_c` | **4 + 4, unchanged** — already at parity |

    Four v6e modules become two. Per-queue totals are unchanged from §5.2 — this is a consolidation,
    not a capacity change, so it must be **net-zero on reserved chips at every point**. Both
    reservations are at 128/128, so retire the tpu-commons module and let it destroy *before*
    raising the vllm module's count. Never let the two counts overlap.

    > `module.ci_v6e_8` carries a phantom index above 8 — drop `instance_count` to 8 before any
    > `-target` apply against it, or the plan will not resolve. The collapsed target is 8 anyway.

    **Apply the v6e collapse in two passes.** A single untargeted apply contains both the destroys
    and their replacement creates, and Terraform gives no ordering guarantee across separate
    modules. Worst case it asks for 94 existing + 76 new chips against a hard ceiling of 128 and
    fails halfway. Target the destroys first:

    ```
    terraform apply -target=module.ci_v6e_1 -target=module.ci_v6e_8   # frees 68 chips
    terraform apply                                                   # creates 76
    ```

    Run `terraform state list | grep ci_v6e_8` first — the phantom index above will stop `-target`
    resolving. Finish with an untargeted `plan` to confirm convergence.

    > **Destroying the benchmark fleet does not remove the need for this.** `us-east5-a` is exactly
    > 128/128: 94 chips of CI (20 + 48 + 10 + 16) and 34 of benchmark (`vllm-tpu-v6e-1-bm-600..609`,
    > `-v6e-4-bm-600,601`, `-v6e-8-bm-600,601`). Freeing all 34 still leaves the 128 ceiling below
    > the 170-chip worst-case overlap, so it turns "will fail" into "will probably succeed" — the
    > worse failure mode inside a timed window. It also cannot reduce the PR count: that fleet is
    > not in this repo's Terraform (the only `benchmark` module here is `count = 0` in
    > `us-east1-d`). Worth destroying separately as headroom if its owners agree — end state would
    > be 102/128 — but it is not load-bearing.

    **Only v6e grows; CPU is already done.** The v6e pairing really is a split of one fixed
    128-chip reservation, so collapsing it has to move counts. The CPU pairing never was: the vllm
    CPU modules were built in [#486](https://github.com/vllm-project/ci-infra/pull/486) as 1:1
    parity with the tpu-commons fleet's 8 `cpu` + 8 `cpu_64_core`, not as a half-share. vllm is
    already at 8 and 4 + 4. Nothing to raise — the window's CPU work is purely *retiring*
    `ci_cpu` and `ci_cpu_64_core_zone_c`, which has to wait because tpu-commons pipelines run on
    them until traffic cuts over.

    > An earlier revision of this step targeted `cpu` 8 → 16 and `cpu_64_core` 4 + 4 → 8 + 8. That
    > was wrong — it read the parity fleets as a capacity split and would have doubled the CPU
    > fleet for no reason. The 32 VMs built on 09-03 under that reading were destroyed the same
    > day; live state is unchanged.

31c. ⬜ **`cpu_64_core` cannot grow in `us-central1-b`/`f` — zone stockout, not quota.** Raising
    `ci_cpu_vllm_zone_b` 8 → 16 on 2026-09-03 succeeded (16 × `e2-standard-2`, all agents
    registered). Raising the two 64-core modules 4 → 8 **failed on every one of the 8 new
    instances**:

    ```
    Error: The zone 'projects/cloud-ullm-inference-ci-cd/zones/us-central1-b' does not have
    enough resources available to fulfill the request. '(resource type:compute)'.
    A n2d-standard-64 VM instance is currently unavailable in the us-central1-b zone.
    ```

    Same error in `us-central1-f`. Neither CPU module declares a `reservation_affinity`, so these
    are pure on-demand and subject to zonal stockout. Terraform state stayed clean (no tainted
    resources); the 8 `-ip` addresses for indices 4–7 were created and sit `RESERVED`, and a later
    apply will pick them up.

    The retry on 09-03 failed identically on all 8. A direct single-instance capacity probe across
    the region then established where `n2d-standard-64` can actually land:

    | Zone | `n2d-standard-64` capacity |
    |---|---|
    | `us-central1-a` | ❌ `state:STOCKOUT` |
    | `us-central1-b` | ❌ stocked out (2 terraform attempts) |
    | `us-central1-c` | ⚠️ available one at a time, **not** as a burst of 8 |
    | `us-central1-f` | ❌ stocked out (2 terraform attempts) |

    > **A single-instance probe does not predict a bulk create.** `us-central1-c` accepted a
    > one-off probe instance, then rejected all 8 when terraform requested them in parallel. The
    > zone hints GCP puts in the error text ("try `us-central1-a`, `us-central1-f`") flip within
    > minutes and contradicted the probe results — do not plan around them.

    **This invalidates the 8 + 8 target as written**, but not the approach — the growth just has to
    go in `us-central1-c`, the only zone with capacity. Freeing machines in c at cutover does *not*
    create headroom in b or f; zonal capacity is not fungible, so a grow-b/f plan attempted inside
    the window would risk ending with **fewer** `cpu_64_core` agents than we started with.

    ### ✅ Resolved 2026-09-03 — `terraform apply -parallelism=1`

    A new **`ci_cpu_64_core_vllm_zone_c` module at 8** was added, with b and f left at 4 + 4. That
    is 4 + 4 + 8 = **16**, the full fleet, and it keeps the "only `*_vllm` modules survive" shape.

    The fix that made it work was **serializing the creates**. Terraform's default parallelism of 10
    asks the zone for all 8 machines at once, which a trickling stockout will refuse outright; one
    at a time, every one of the 8 succeeded:

    ```
    terraform apply -parallelism=1 <plan>
    → Apply complete! Resources: 8 added, 0 changed, 0 destroyed.
    ```

    **Keep `-parallelism=1` for any on-demand CPU create from here on.** It is slow (~1–2 min per
    instance) but it is the difference between all-or-nothing and all-succeed.

    Verified after apply: `us-central1-c` at 16 `n2d-standard-64` (8 tpu-commons + 8 vllm), b and f
    at 4 each; **32 of 32 CPU agents connected in the vllm org — `cpu` 16, `cpu_64_core` 16**;
    `terraform plan` reports no changes on all four CPU modules. The 8 `-ip` addresses stranded in b
    and f by the failed attempts were cleaned up in the same apply.

    All that is left for the window is *retiring* the tpu-commons modules — `ci_cpu` (8, other root)
    and `ci_cpu_64_core_zone_c` (8) — which must wait, since tpu-commons pipelines still run on
    them. Until then the region carries 24 `n2d-standard-64` instead of 16; that over-provision is
    the price of de-risking the window.

    Fallback, if the zone-c fleet ever has to be rebuilt and capacity is gone: flip
    `ci_cpu_64_core_zone_c`'s `buildkite_token_value` to the vllm token in place and leave b/f at
    4 + 4. Same 16, zero creates, zero destroys, reversible by flipping back — the same mechanic as
    the v7x flip. Costs only tidiness, fixable later with a `moved` block.

32. ⬜ Point CI monitoring at the new org — [ci-infra #488](https://github.com/vllm-project/ci-infra/pull/488),
    already open and rebased. It is a **single variable**: `org_slug = "tpu-commons"` → `"vllm"` on
    `ci_monitoring`, plus the matching agent-metrics token. The module already threads `org_slug`
    into the `buildkite-webhook-handler` Cloud Function's `ORG_SLUG` env and already reads
    `vllm_buildkite_rest_api_token`, so no separate edit is needed for either.
33. ⬜ Re-count agents per queue against `instance_count` (step 11).
34. ⬜ Two machines are **not** in Terraform and will not follow the token swap:
    `t1v-n-ae378e31-w-0` (queue `tpu_v7x_32_queue`, referenced only by
    `vllm-torchtpu/.buildkite/pipeline_dev_k3_gcs.yml:39`) and `vllm-cpu-64-core-vm` (queue `cpu`).
    Re-point them by hand or accept their loss.
35. ⬜ Merge the `vllm-torchtpu` PR prepared in step 22 (`suite-slug: tests` → `tpu-ci`). The OIDC
    policy it relies on is already in place from step 21.
36. ⬜ In **tpu-commons**: disable all 11 schedules, set `trigger_mode: none`, set
    `publish_commit_status: false`.
37. ⬜ In **vllm**: set `trigger_mode: code`; restore each pipeline's original
    `publish_commit_status`, `publish_commit_status_per_step` and `build_pull_requests` values;
    enable 10 schedules (the weekly kernel-tuning one is disabled today and stays disabled).
38. ⬜ Verify `buildkite/tpu-inference-ci/<step>` contexts appear on a test PR **before** re-enabling
    branch-protection enforcement.
39. ⬜ Smoke-test: trigger one build per pipeline and confirm each finds agents on its queue —
    including a `vllm-torchtpu-ci` build, to prove the OIDC upload path end to end.

### Phase 5 — cleanup ⬜ (only after a green week)

40. ⬜ Update hardcoded `tpu-commons` URLs:
    - `tpu-inference/.buildkite/benchmark/scripts/mlcompass_export.py:80`
    - `tpu-inference/.github/ISSUE_TEMPLATE/450-ci-failure.yml:60`
    - `vllm-torchtpu/.github/ISSUE_TEMPLATE/450-ci-failure.yml:46`
    - `torchtpu-vllm/.github/ISSUE_TEMPLATE/450-ci-failure.yml:46`
41. ⬜ Delete the 11 tpu-commons pipelines **and their GitHub webhooks** — Buildkite does not remove
    webhooks on pipeline deletion, so they become 404-orphans. `vllm-torchtpu-nightly-image` is
    already schedule-disabled there (step 19a) and can go first, ahead of the rest, once its vllm
    counterpart has been green for a few nights.
42. ⬜ Rename GCP secrets `tpu_commons_*` → `vllm_*`, or leave them and document why.
43. ⬜ Delete `cloud-ullm-inference-ci-cd/errored.tfstate` — 706 KB of plaintext secrets, stale
    against remote state.
44. ⬜ Retire dead knobs: `GITHUB_DEPLOY_KEY` (0 consumers), `h100_8_queue` (0 YAML references, never
    ran a job), and unreferenced modules `{benchmark, ci_v5, ci_v6}`. Also drop the now-unused
    `us-east5-a` and `southamerica-west1-a` provider aliases from `cloud-tpu-inference-test` —
    **unblocked**: the v6e destroy applied on 2026-08-28, so this is now safe to do at any point.
    Consider deleting the phantom `cloud-tpu-inference-test-v7x` root at the same time (step 28).
45. 🔄 Restore the v6e-8 fleet to 9 nodes. **Not satisfied by the relocation.** The old
    `southamerica-west1-a` config declared a 9th node its hyperdisk quota could never create, which
    left index 8 permanently phantom and broke targeted applies; the new `us-east5-a` fleet declared
    a clean 6 (+2 vllm), so the phantom was gone — but the count was 8, not 9.

    A 9th v6e-8 costs 8 chips and the reservation has none spare, so it was a **trade with the
    benchmark fleet**, which held 34 of the 128 (10 × v6e-1, 2 × v6e-4, 2 × v6e-8). Two candidates
    freed exactly 8: one bm v6e-8, or both bm v6e-4. Took the v6e-8 — those two v6e-4 are the only
    v6e-4 in the whole benchmark fleet (`infer_test_southamerica_west1` declares 0) and 7 hourly
    cases need them, including a customer autotune sweep, whereas v6e-8 goes 14 → 13 fleet-wide with
    12 still in `southamerica-west1-a`.

    Applied as a paired change, bm-first so the chips were free before CI asked for them:
    bm-infra `ci_cd`'s `v6e_8_count` 2 → 1, then ci-infra's `ci_v6e_8` `instance_count` 6 → 7.
    Split becomes CI 102 / benchmarks 26, still 128/128. Both PRs are open — bm-infra#292 and
    ci-infra#494 — and neither has applied yet; the `tpu_v6e_8_queue` row in §5.2 goes to 7 / 2 / 9
    when they do.

### Out of scope

| Item | Reason |
|---|---|
| Cluster `7d36687d` ("CI-DEV") | PoC only. Pipelines `kube-dev` and `vllm-torchtpu-integration` stay in tpu-commons. |
| `ci-infra/claude-skills/*` | Already targets vLLM's own CI on cluster `9cecc6b1` — unaffected. |

---

## 5. What engineers should expect to see

### 5.1 Right now (before Friday 09-04)

**Almost nothing.** Your PRs still build in `tpu-commons`, at the same URLs, with the same check
names. The vllm org runs the `tpu-inference-ci` nightly in parallel but takes no developer traffic
and publishes no commit statuses.

Two things you may notice:

- **Slightly longer queue times on `tpu_v6e_queue`**, because a third of that fleet now serves the
  new org. Total chips are unchanged; they are just split across two orgs until Friday 09-04.
- **The nightly torchtpu images are now built by the vllm org** (step 19a). Same registry, same
  tags, same contents — only the build link changed. If a nightly image is missing, look at
  `buildkite.com/vllm/vllm-torchtpu-nightly-image`, not the tpu-commons one.

The v6e fleet also changed GCP project on 08-28 (§2.4). Nothing about that is visible from
Buildkite — same queues, same org, same agent names.

### 5.2 Current capacity split

| Queue | tpu-commons | vllm | Total |
|---|---|---|---|
| `tpu_v6e_queue` | 20 | 10 | 30 |
| `tpu_v6e_8_queue` | 6 | 2 | 8 |
| `cpu` | 8 | 8 | 16 |
| `cpu_64_core` | 8 | 8 | 16 |
| `tpu_v7x_2_queue` | 16 | — | 16 |
| `tpu_v7x_8_queue` | 18 | — | 18 |
| `tpu_v7x_16_queue` | 2 | — | 2 |
| `tpu_v7x_32_queue` | 1 | — | 1 |

Verified against the live agent census on 2026-08-28. CPU queues are net-new capacity on the vllm
side, not a split — both orgs are at full strength.

All 30 v6e-1 and 8 v6e-8 are now in `cloud-ullm-inference-ci-cd` / `us-east5-a`, and all 36 v7x are
in `cloud-ullm-inference-ci-cd` / `us-central1-c`. `tpu_v7x_32_queue`'s single agent is
`t1v-n-ae378e31-w-0`, which is not managed by Terraform — see step 34.

### 5.3 During the cutover window — Fri 2026-09-04, 13:00 PT

- **Assume no CI for the duration of the window.** Budget up to ~4 hours. Pushes during the window
  will not start a build; re-push or retry afterwards.
- Merges that depend on a green TPU check will be blocked until the window closes. Land anything
  time-sensitive before 13:00 PT Friday.
- Agents are rolled in batches, so queue depth will look erratic while it happens. That is expected.
- If a build is queued when its agent is rolled, it is retried automatically on another agent.

### 5.4 After the cutover

- Your build links point at `buildkite.com/vllm/...`. Check names on the PR are unchanged, so branch
  protection continues to pass/fail exactly as before.
- `vllm-torchtpu` has no branch protection rule today, so only `tpu-inference` is exposed to the
  status-check path at all.
- Log in to Buildkite under the **`vllm`** org. If you cannot see the TPU pipelines, you need to be
  added to team `google-tpu` — see Appendix A.
- Test-analytics trends reset: the new suite `vllm/tpu-ci` starts from 2026-08-26. Historical data
  stays in `tpu-commons/tests` and is not migrated.
- Everything else — pipeline names, step names, queue names, machine images, test behaviour — is
  identical.

### 5.5 Known transient failures

These are real but not caused by the migration. Retry before investigating:

- **v7x steps failing in the vllm nightly.** Expected until the window — that org has no v7x agents
  (step 19). Only the v6e and CPU results are meaningful there for now.

- **Ray port collision.** `tpu6e/tpu7x E2E test for Multihost DCN-based P/D disaggregation` co-locates
  a prefill and a decode server on one host; their two Ray raylets race to bind port `10002` and the
  loser dies with `Address already in use`, surfacing as
  `vllm serve on 8400 (PID …) died inside container while waiting for health check`. Observed once
  in validation build #1 and **passed on retry on the same agent**.
- **Agentless VM after a roll.** If a queue is short of agents after a fleet roll, the startup script
  probably lost a dpkg-lock race with `unattended-upgrades`. The fix is a Terraform `-replace` of the
  affected node — a package reinstall does **not** repair it.

### 5.6 Where to look

| Question | Where |
|---|---|
| Is the new org healthy? | [vllm TPU cluster queues](https://buildkite.com/organizations/vllm/clusters/0219f117-7dc6-4a04-aee2-1619736fd800/queues) |
| Did my build run? | `buildkite.com/vllm/tpu-inference-ci` (after the cutover) |
| Is an agent missing? | Compare agent count per queue against Terraform `instance_count` |
| Who owns the fleet config? | `terraform/gcp_old/tpu-inference/` in `vllm-project/ci-infra` |

---

## 6. Validation evidence

First real build in the new org — `vllm/tpu-inference-ci` #1, `main`, 2026-08-26:

| Result | Count |
|---|---|
| v6e jobs passed | 15 |
| v6e jobs failed | 1, **green on retry** (§5.5 Ray port race) |
| v7x jobs | never scheduled — no v7x agents in the vllm org until phase 4 |
| Steps skipped on an unmet `if:` condition | 25 (all `Nightly` variants — normal) |
| Analytics runs landed in `vllm/tpu-ci` | 1, finished |

This exercised the full chain on real hardware: agent token → registration → clone via the CI bot
App → cluster secrets → test execution on both `tpu_v6e_queue` and `tpu_v6e_8_queue` → analytics
upload to the new suite.

### 6.1 Nightly evidence, 08-30 → 09-03

Scheduled `vllm/tpu-inference-ci` builds, scoped to v6e (queues `tpu_v6e_queue`, `tpu_v6e_8_queue`,
plus `cpu` steps named `tpu6e*`). 512 v6e jobs every night; `497 passed / 15 broken` is the healthy
signature — the 15 `broken` are unmet `if:` conditions, not failures.

| Night | vllm (v6e only) | tpu-commons (v6e only) |
|---|---|---|
| 08-30 #3 | 497 passed / 15 broken | — |
| 08-31 #4 | 497 passed / 15 broken | — |
| 09-01 #5 | 494 passed / **3 failed** / 15 broken | — |
| 09-02 #6 | 497 passed / 15 broken | #25047 — 497 passed / 15 broken |
| 09-03 #7 | 497 passed / 15 broken | #25114 — 497 passed / 15 broken |

Where both orgs ran the same night, the non-passing `(state, name)` sets are **identical**. The one
exception, 09-01, was a single lost agent — `tpu6e JAX unit tests part2` exited `-1` on
`v6e-1-ci-vllm-6-cicd-us-east5-a`, taking the two CPU reporting steps with it. Not retried, not
recurring.

**Every vllm nightly still reports `failed` at the build level, and that is expected pre-cutover:**
the vllm org has zero v7x agents, so ~110 v7x jobs expire, ~106 dependents go `waiting_failed`, and
~30 CPU report steps exit 1. The 609–789 min runtimes are the expiry timeout, not slow tests. Step 30
is what clears this.

One thing to watch: `tpu_v6e_8_queue` max wait is **119–125 min in vllm against 44–77 min in
tpu-commons** — 2 agents versus 6. No failures from it, but it is the in-window bottleneck until
step 31b consolidates the fleet at 8.

---

## 7. Rollback

Up to and including phase 4, rollback is **one lever**: point `buildkite_token_value` back at
`tpu_commons_buildkite_agent_token` and re-roll. The tpu-commons cluster, its queues, its secrets and
its pipelines stay fully intact until phase 5, so agents re-register into the old org on next boot.
Cost: one rolling restart, ~30–50 min per module.

Rolling back the traffic flip additionally means re-enabling the tpu-commons schedules and triggers
and disabling them in vllm — a few minutes of API calls. The same applies to the nightly image build
moved early in step 19a: flip its two schedules back the other way.

**The reservation move (§2.4) is not part of this lever and does not need to be.** The v6e fleet
stays in `cloud-ullm-inference-ci-cd` under either outcome; only the token it registers with
changes. Reverting the project move is not possible — `cloud-tpu-inference-test` no longer has a
v6e reservation — and is not required for a Buildkite rollback.

**Phase 5 is the only irreversible step.** Do not start it until the new org has been green for a
full week, including all nightlies and the weekly.

---

## 8. Day-of checklist — Fri 2026-09-04, 13:00 PT

The single ordered list to work from in the window. Each item points at the phase-4 step that
explains it; this section is the running order, not a second source of truth.

### Before the window (morning of 09-04)

- [ ] **`git fetch && git rebase origin/main` before the first `terraform plan`, and re-check
      between phases.** On 09-03 a CPU apply ran from a branch 11 commits behind main and so used
      the pre-[#506](https://github.com/vllm-project/ci-infra/pull/506) startup script, which echoes
      `HF_TOKEN` to the serial console. Confirm with `git log --oneline HEAD..origin/main` — it must
      be empty — and `git diff origin/main...HEAD`, which should show only the changes you intend.
- [ ] Decide what to do about the two **merged-but-unapplied** changes sitting in main, since the
      window's rolls will pick them up whether or not that is intended:
      **#506** (startup-script token fix — a `metadata`-only diff on all 8 CPU + 8 64-core-vllm +
      8 64-core-tpu-commons + 30 v6e + 36 v7x instances; note metadata alone does not re-run the
      script, so it only takes effect on the next boot) and **#494** (the 7th `ci_v6e_8` node and
      its disk, never created).
- [ ] Re-run the access sweep and add any overnight acceptances to team `google-tpu` (Appendix A).
      Org membership alone grants nothing — both pipelines are PRIVATE and attached only to that team.
- [ ] Chase the admin to **re-send invites to the people who report never receiving one**
      (Amy Lin, Bavina Inuganti as of 09-03).
- [ ] Confirm [#488](https://github.com/vllm-project/ci-infra/pull/488) is green and rebased;
      plan must read `0 to add, 2 to change, 2 to destroy`.
- [ ] Reconcile the announcement window against this doc — the draft mail says 13:00–21:00, the
      agreed window is 13:00 PT for ~4 h.
- [ ] Decide the two still-open questions **before** the window so neither is discovered mid-flight:
      `kube-dev` migrate-or-retire, and `chengjiyao@meta.com`'s place in `google-tpu`.

### Freeze first

- [ ] Step 36 — in **tpu-commons**: disable all 11 schedules, `trigger_mode: none`,
      `publish_commit_status: false`. Do this *before* touching any fleet, so nothing new lands
      mid-move and commit statuses never overlap between orgs.
- [ ] Drain in-flight builds; write down anything that will need a re-run.

### Cut over

- [ ] Step 30 — flip `buildkite_token_value` on `ci_v7x_2` (16), `ci_v7x_8` (18), `ci_v7x_16` (2)
      and roll one module at a time. **Highest-risk step:** v7x has no `*_vllm` counterpart, so this
      is a modify-in-place hard cutover with no overlap window. It is also the step that makes the
      vllm nightlies stop failing (§6).
- [ ] Step 31 / 31a — same flip for the remaining tpu-commons CPU and v6e modules.
- [ ] Step 31b — collapse the duplicated fleets: retire each tpu-commons module and raise its
      `*_vllm` counterpart to the full count. Destroy before create; both reservations are 128/128.
      Only the **v6e** rows are left to do — `cpu` was already raised to 16 on 09-03, and
      `cpu_64_core` follows 31c, not the 8 + 8 in the table.
- [ ] Step 31c — `cpu_64_core`: destroy `ci_cpu_64_core_zone_c` (8). Its replacement,
      `ci_cpu_64_core_vllm_zone_c` (8), is already built and connected. Nothing needs creating —
      and do **not** try to, `n2d-standard-64` was stocked out region-wide on 09-03. If any CPU
      instance does have to be created in the window, use `terraform apply -parallelism=1`.
- [ ] Step 32 — merge and apply #488 so monitoring points at vllm.
- [ ] Step 35 — merge the `vllm-torchtpu` PR (`suite-slug: tests` → `tpu-ci`).
- [ ] Step 37 — in **vllm**: `trigger_mode: code`, restore each pipeline's `publish_commit_status`,
      `publish_commit_status_per_step` and `build_pull_requests`, enable the 10 schedules.

### Verify before declaring done

- [ ] Step 33 — agent census per queue matches `instance_count`, and all four v7x queues are
      populated in vllm (16 / 18 / 2 / 1) with v6e at 30 / 9 (9 only if #494 has been applied; 8 if not).
- [ ] Step 39 — trigger one build per pipeline, including `vllm-torchtpu-ci`, and confirm jobs
      **dispatch** rather than expire. Expiry with zero wait and zero run time is the signature of a
      queue with no agents.
- [ ] Step 38 — `buildkite/tpu-inference-ci/<step>` contexts appear on a test PR before
      re-enabling branch-protection enforcement.
- [ ] Step 34 — re-point or knowingly abandon the two non-Terraform machines,
      `t1v-n-ae378e31-w-0` (`tpu_v7x_32_queue`) and `vllm-cpu-64-core-vm` (`cpu`).
- [ ] Repoint the Ops View dashboard's three `tpu_commons` metric paths to `vllm`.

### After the window

- [ ] Delete the `GITHUB_PAT` cluster secret from both orgs
      (`1f4d6cb4-d9dc-466b-89aa-0d018feb45f4` in vllm).
- [ ] Narrow the new REST token from 25 scopes to the 5 read scopes it actually needs.
- [ ] Add a log-based alert on non-200s from `buildkite-webhook-handler`.
- [ ] Rebuild the four `tpu-vllm-integration` jobs killed during the reservation move; start or
      taint `vllm-tpu-v6e-8-bm-201/202/203` (currently STOPPED).
- [ ] Phase 5 stays untouched until the new org has been green for a full week — it is the only
      irreversible part (§7).

### Rollback, in one line

Point `buildkite_token_value` back at `tpu_commons_buildkite_agent_token` and re-roll, then
re-enable the tpu-commons schedules and disable the vllm ones. Two levers, ~30–50 min per module.
This stops working once step 31b has destroyed the tpu-commons modules, so **treat 31b as the
point of no easy return** and hold it until the smoke tests above pass.

---

## Appendix A — access migration list

`tpu-commons` has a single default team, `everyone`, so **all 99 org members have access to every TPU
pipeline today.** The `vllm` org uses team `google-tpu` (`9f49084a-8012-42cd-8833-ae85d3b5db30`),
which currently has **2** members. Without action, 97 people lose visibility into their own CI at
cutover.

| Group | Count | Action needed |
|---|---|---|
| **A** — already in the `vllm` org | 11 | Add to team `google-tpu` |
| **B** — not in the `vllm` org | 88 | Invite to the org, then add to team `google-tpu` |
| Already in `google-tpu` | 2 | none (Ming Huang, QiliangCui) |

Inviting requires a **`vllm` org admin**. Adding to the team does **not** — a team MAINTAINER can do
it, which is how the adds below were made without admin rights.

#### Status as of 2026-09-03 — the table above is the 2026-08-28 starting point

| | |
|---|---|
| `tpu-commons` accounts | 103 |
| `vllm` org accounts | 310 |
| In team `google-tpu` | **74** |
| In the `vllm` org but not in the team | **0** |
| `tpu-commons` accounts with no `vllm` account | 31 — **29** excluding two whose owner has access under another address |
| Share of builds since 07-01 by people already in the team | **85%** (89% excluding those two) |

`cuiq@google.com` and `p@temotter.com` are covered via `derrhein@gmail.com` and
`patemotter@google.com`; both owners confirmed. Two people report never receiving an invitation
(Amy Lin, Bavina Inuganti) and need the admin to re-send — no sweep will surface them.

> **Identity is the email address on the Buildkite account.** Two accounts with different addresses
> are two identities even when the display names match. Anyone who accepted on a different address
> shows as outstanding here but is in fact covered, so the outstanding count is an upper bound.
>
> **Invitation state is not readable without org admin.** REST `/invitations` returns
> *"You're not allowed to invite members to this organization"*, and the GraphQL `invitations`
> connection silently reports `count: 0` for every state including `ACCEPTED`. Use the roster diff
> (`tpu-commons` members minus `vllm` members), not invitation records.
>
> Live tracker with per-person status and comment threads:
> [Buildkite vllm org migration — access status](https://docs.google.com/document/d/1V-HSqX1FrIXVit5VR7t-pZFs1ZXi7PYmjHFjkZk2fWE/edit).

> **Two caveats before bulk-inviting.**
> 1. Several people hold both a corporate and a personal account (`cuiq@google.com` and
>    `derrhein@gmail.com` are the same person). 45 of the 99 are `@google.com`; 54 are not. The list
>    below is accounts, not humans — prune it before sending 88 invites.
> 2. Group A membership is matched on email address. Anyone using a different address in each org
>    will show up in group B by mistake.

### A. Already in the `vllm` org — add to team `google-tpu` (11)

| Name | Email | tpu-commons role |
|---|---|---|
| QiliangCui | `derrhein@gmail.com` | admin |
| gpolovets1 | `gpolovets@gmail.com` | admin |
| Pysith Vanuptikul | `piv@google.com` | admin |
| Ming Huang | `theminghuang@gmail.com` | admin |
| Chengji Yao | `chengjiyao@meta.com` | member |
| lkchen | `github@lkchen.net` | member |
| Kevin H. Luu | `khluu000@gmail.com` | member |
| JiriesKaileh | `mmjiries12@gmail.com` | member |
| Richard Liu | `ricliu@google.com` | member |
| Woosuk Kwon | `woosuk.kwon@berkeley.edu` | member |
| Yifan Qiao | `yifanqiao@inferact.ai` | member |

### B. Not in the `vllm` org — invite, then add to team `google-tpu` (88)

| Name | Email | tpu-commons role |
|---|---|---|
| Bavina Inuganti | `bavina.inuganti@gmail.com` | admin |
| Teresa Chen | `chenteresa@google.com` | admin |
| QiliangCui2023 | `cuiq@google.com` | admin |
| dennisyeh | `dennisyeh@google.com` | admin |
| XiongfeiWei | `isaacwxf23@gmail.com` | admin |
| weiyu | `jcab1688@gmail.com` | admin |
| jyj0w0 | `jinyijia24@gmail.com` | admin |
| Kewei Wang | `keweiwang@google.com` | admin |
| Jun Wan | `mrjunwan@google.com` | admin |
| Patrick Ji | `patrickji2014@gmail.com` | admin |
| Lumosis | `ranlihao@google.com` | admin |
| StingLin | `yenpei@google.com` | admin |
| Yiwei Wang | `yiwei.dddd@gmail.com` | admin |
| chen qian | `1370871543@qq.com` | member |
| acczz | `814429359@qq.com` | member |
| Alexis Macaskill | `amacaskill@google.com` | member |
| Aman Gupta | `aman2930@gmail.com` | member |
| Amanda Liang | `amandafilan@gmail.com` | member |
| cychiuak | `anderson19991122@gmail.com` | member |
| Benliang Wang | `benliangw@gmail.com` | member |
| Mehdy Bohlool | `bohlool@gmail.com` | member |
| brianseung | `brianseung@google.com` | member |
| Huy Cao | `caogiahuy615@gmail.com` | member |
| caojx-google | `caojx@google.com` | member |
| Caslyn Tonelli | `caslyn.tonelli@gmail.com` | member |
| ernie-chang | `changernie@google.com` | member |
| Danna Wang | `dannawang@google.com` | member |
| Denali Molitor | `dmolitor@ucla.edu` | member |
| zhubin | `doran_zhu@163.com` | member |
| dongruizi | `drq12345@outlook.com` | member |
| evangelenes08 | `evangelenes@google.com` | member |
| George Novack | `george@inferact.ai` | member |
| Yuhao Ge | `geyuhao33@gmail.com` | member |
| guowei-dev | `guoweij@google.com` | member |
| gxd3 | `gxd@google.com` | member |
| HandsomeHow | `handsomehowyxh@gmail.com` | member |
| Harsh Shah | `harsh.shah.hks@gmail.com` | member |
| Harut Movsisyan | `harut@google.com` | member |
| Haibo Huang | `hhb@google.com` | member |
| Hung-Ming Hsu | `hmhsu@google.com` | member |
| Igor Tsvetkov | `igorts@google.com` | member |
| Jacob Platin | `jacobplatin@google.com` | member |
| Jahangir Hasan | `jahangir@google.com` | member |
| Juncheng Gu | `jcgu@google.com` | member |
| Jeff Ma | `jeffjma@inferact.ai` | member |
| Jimmy Tsai | `jimmytsai@google.com` | member |
| jparkerh | `jparkerh@google.com` | member |
| Junyan Xu | `junyanxu5513@gmail.com` | member |
| Kyuyeun Kim | `kyuyeunk01@gmail.com` | member |
| Howard Liberty | `liberty@anymemo.org` | member |
| Charles Li | `licharles@google.com` | member |
| Xuting | `liuxt129@gmail.com` | member |
| Lyle Lai | `lyle.lai@cienet.com` | member |
| Mike Heddes | `mikeheddes@gmail.com` | member |
| Mudit Gokhale | `muditgokhale2@gmail.com` | member |
| Orti Bazar | `orti@google.com` | member |
| Pate Motter | `p@temotter.com` | member |
| Pritha D N | `pritha.narayanappa@gmail.com` | member |
| Qi Zhou | `qizzzh@google.com` | member |
| Ken Cheng | `qw22323+github@hotmail.com` | member |
| John | `qzhang03022@gmail.com` | member |
| rsingh-g | `rsinghc@google.com` | member |
| Rushabh Lalwani | `rushilal2411@gmail.com` | member |
| Saikat Roychowdhury | `saikat.royc85@gmail.com` | member |
| Sangam Jindal | `sangamjindal49@gmail.com` | member |
| Santosh Dixit | `santosh.hogwarts@gmail.com` | member |
| Sierra Qian | `shengjieqian1225@gmail.com` | member |
| ShobhitBehl | `shobhitbehl@google.com` | member |
| Xiang Si | `sixiang@google.com` | member |
| haotianxue-google | `ultraman.giantoflight@gmail.com` | member |
| Xiaohua (Victor) Liang | `victorlxh@google.com` | member |
| Venkata Ayyagari | `vsayyagari@google.com` | member |
| wzy-782 | `wangziyi@google.com` | member |
| Weida Hong | `wdhongtw@gmail.com` | member |
| WenXin Dong | `wenxindong@google.com` | member |
| wjessG | `wjess@google.com` | member |
| Wonpyo Park | `wppark.pio@gmail.com` | member |
| wyzhang | `wyzhang@google.com` | member |
| xuefgu | `xfgu@google.com` | member |
| xiangll | `xiangll@google.com` | member |
| Xiyu Xie | `xiyuxie@google.com` | member |
| anthonsu | `xsuanthony@gmail.com` | member |
| Yin Lin | `yinlin09@gmail.com` | member |
| Ylang | `ylangt@google.com` | member |
| Yun Yao | `yunyao@google.com` | member |
| yuyanpeng-google | `yuyanpeng@google.com` | member |
| zhangamy-crypto | `zhangamy@google.com` | member |
| Wenzhe Zhou | `zwzmzd@gmail.com` | member |

---
