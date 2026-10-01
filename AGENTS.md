# Agent notes for ci-infra

## Files a job writes into the checkout from its container

Job containers run as root, and the Buildkite checkout they write into is a bind mount owned by the agent user. A directory created there from inside the container comes out `root:root 0755`. Deleting a file needs write permission on its directory, so the agent can't clean that checkout afterwards. The next job on that agent slot fails at checkout with `Failed to remove ... permission denied`, then `fatal: destination path '.' already exists`, and git exit 128 on every retry. Every later job on the slot fails the same way until someone runs `sudo rm -rf` on the slot's `builds/<slot>/vllm/ci`.

- **Writing such code:** make every directory the job creates in the checkout `0777`, including parents it creates, and open up what the job wrote when it ends, even on failure. Add a test that the directories come out deletable. `buildkite/ci_selector/recorders/fnrec/ci_setup.sh` and `recorders/kernrec/ci_setup.sh` do this, and `recorders/kernrec/test_checkout_perms.sh` tests it.
- **Staging it:** the build that leaves the files behind passes. The failure lands in the next job on the same slot, usually someone else's build, where it looks like an infra flake. After a staging build of new job code, list the agents its jobs ran on and check those slots for files the agent can't delete before calling it validated.
- **Debugging a clone failure:** `permission denied` while removing the old checkout points at root-owned files left by an earlier job on that slot. List the path in the error to find which job wrote them (ci-infra#701 was one such case).
