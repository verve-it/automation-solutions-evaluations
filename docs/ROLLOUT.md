# Rollout: what has to happen outside this repo

The code changes land as patches. These are the steps only someone with
access to the Azure subscription, the Foundry projects and the GitHub
settings can take. Ordered so each one unblocks the next; kept current with
every patch.

Status: `[ ]` to do, `[x]` done, `[~]` depends on what an earlier step shows.

---

## 1. Apply the patches

- [ ] Apply every delivered patch in order (`git am <file>`), newest last.
      Patch 77 and later change the replay server, so step 3 comes after.
- Note: **the deployed replay server may be down now.** Any build
      from `staging` or `develop` since 2026-09-22 (commit dec0fb1) packaged
      a server that cannot import (`evalconfig.py` was left out), so every
      request is a 502 and the gate fails at its first step. Patch 78 fixes
      the package; step 3 redeploys it.

## 2. Azure permissions

- [ ] **CI can log in.** Both drift jobs fail at `azure/login` with
      `No subscriptions found`, and the agent gate logs in the same way. The
      federated credential matches; what is missing is a subscription the app
      can see. Find out which case it is (run as yourself; `--all` matters):

      ```
      az role assignment list --assignee <staging app id> --all -o table
      az role assignment list --assignee <prod app id> --all -o table
      ```

      - Rows scoped to the Foundry project → project-scoped roles do not make
        a subscription visible. Either ask for the one-line change to add
        `allow-no-subscriptions: true` to both `azure/login` steps, or have a
        subscription Owner grant **Reader** at subscription scope.
      - No rows → the role went to the wrong object; re-assign it to the app
        id and the login may simply start working.
- [ ] **Trace export works.** On the Log Analytics workspace, give **both** CI
      apps **Log Analytics Reader** and **Privileged Monitoring Data Reader**.
      Without the second the export returns spans with no content and every
      check scores an empty run, silently.
- [x] Foundry User on both projects, for both CI apps.
- [~] If the first gate run fails with 403 on creating an agent version or a
      toolbox, Foundry User is not enough for the replay's writes; the next
      step up is Azure AI Project Manager.
- [ ] **Role Based Access Control Administrator** on the replay Function's
      resource group, for you (not CI), alongside the Contributor you deploy
      with (Owner alone covers both). With both, step 3's deploy assigns the
      identity its storage role itself. If the roles are split between two
      people, `functions/replay-mcp/infra/rbac.bicep` says how that works.

## 3. Replay server (the Function)

- [ ] Set `REPLAY_TOKEN` to the token the GitHub environments **already
      hold**. Both deploy scripts refuse to run without it, and a new one
      (`-NewToken`) locks the gate out until both environments are updated.
      If it was not kept, read it back:

      ```
      az functionapp config appsettings list -g <rg> -n <app> --query "[?name=='REPLAY_TOKEN'].value" -o tsv
      ```

      ```powershell
      $env:REPLAY_TOKEN = '<token>'
      ```
      ```bash
      export REPLAY_TOKEN='<token>'
      ```

- [ ] Redeploy on the default `storageAuth=identity`, with the same region
      (and `REPLAY_NAME`, if the first deployment set one) as before. Needed
      anyway: patch 78 changes the package.

      ```powershell
      .\functions\replay-mcp\deploy.ps1 -ResourceGroup <rg> -Location <region>
      ```
      ```bash
      ./functions/replay-mcp/deploy.sh <rg> <region>
      ```

      The region can be left off when the resources sit in the resource
      group's own region.

      The template assigns the identity Storage Blob Data Owner itself (step
      2's role), and the storage key and the SAS drop out of the app's
      configuration. The script prints the storage account and identity it
      used, then runs verify.
- [ ] Verify (the deploy script already ran it; run again after the wait):

      ```powershell
      python functions\replay-mcp\verify.py https://<app>.azurewebsites.net -g <rg>
      ```
      ```bash
      python3 functions/replay-mcp/verify.py https://<app>.azurewebsites.net -g <rg>
      ```

      It reads `REPLAY_TOKEN` from the environment. It must exit 0 (`OK`)
      and report `state after replaying: IdentityBlobStore (managed identity)`.

      A role assigned directly to an identity takes **up to ~10 minutes** to
      take effect. The same identity carries the host's own storage and the
      package download, so during that window the publish can fail or the
      app may not start (the script then runs `diagnose.py` itself) -- run
      the same deploy again after ten minutes. Once the app is up, verify may
      still report replay state `unavailable` with a 403 for the rest of the
      window; that clears by itself -- the server retries the store, nothing
      needs restarting. If it still fails
      after 15 minutes, send verify's full output; it includes the handler's
      log, whose `state env :` line names the settings the platform provided.
- [x] `REPLAY_SERVER_URL` and `REPLAY_TOKEN` set on the `staging` and `prod`
      GitHub environments.

## 4. Foundry

- [~] The replay creates its clones under **`connectwise-operations-agent-replay`**,
      never under the production agent. If the service will not create an
      agent by adding its first version, the first gate run stops with an
      error saying so; then create that agent once, from the same code as
      `connectwise-operations-agent`. Nothing may ever call it by name --
      no A2A caller, no configuration.

## 5. GitHub

- [ ] Get `agent-gate.yml` onto `main` (callers pin `@main`, and
      `workflow_dispatch` only lists workflows on the default branch).
- [ ] Dispatch the gate against **staging**, then once more with
      `min-score` = `1` to see it go red. Send the run links.
- [ ] Wire the agents repository's deploy workflow: a job that `uses:` the
      gate with `environment: staging`, and `needs:` it on the deploy job.
      The gate has **no `agent:` input** any more; a caller passing one fails.
      Add `prod` only after staging has gated one real agent change.
- [ ] Optional: install the Claude GitHub App on this repository if you want
      PRs watched and CI failures picked up automatically.

## 6. Data and security

- [ ] **Purge the leaked customer data from git history.** Five blobs from
      unscrubbed exports are still reachable from `main`, `staging` and
      `develop` on a public repository. `docs/HISTORY-PURGE.md` is the
      rehearsed runbook (by blob id, not path). Afterwards: ask GitHub Support
      to purge cached views, and everyone re-clones.
- [ ] Decide whether this repository should be public at all. Actions logs,
      job summaries and artifacts of a public repository are world-readable;
      the gate keeps raw exports off them, but "private" removes the category.

## 7. Test data

- [ ] Point me at one **clean** `connectwise-operations-agent` run (an
      operation id, or a trace export). Today the gate's only single-agent
      recordings are the two worst runs, so it guards against making them
      worse; a known-good recording lets it guard a good one. The trace goes
      through `scrub_trace.py`'s propose → review → apply before it is
      committed.
