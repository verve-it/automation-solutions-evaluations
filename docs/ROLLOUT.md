# Rollout: what has to happen outside this repo

The code changes land as patches. These are the steps only someone with
access to the Azure subscription, the Foundry projects and the GitHub
settings can take. Ordered so each one unblocks the next; kept current with
every patch.

Status: `[ ]` to do, `[x]` done, `[~]` depends on what an earlier step shows.

---

## 1. Apply the patches

- [x] Patches 77 and 78.
- [ ] Patch 79 (CI login without a subscription, the replay agent made per
      run, the caller workflow).

## 2. Azure permissions

- [x] **CI can log in.** Both `azure/login` steps use
      `allow-no-subscriptions: true` and no `subscription-id` (patch 79):
      the CI apps see no subscription and CI makes no ARM call. The
      `AZURE_SUBSCRIPTION_ID` variable is no longer read; leave or delete it.
- [ ] **Trace export works.** On the Log Analytics workspace, give **both** CI
      apps **Log Analytics Reader** and **Privileged Monitoring Data Reader**.
      Without the second the export returns spans with no content and every
      check scores an empty run, silently.
- [x] Foundry User on both projects, for both CI apps.
- [~] The replay now **creates** `<agent>-replay` on its first version and
      deletes it after the run. If the first gate run fails with 403 at
      "could not create a version of ...-replay", Foundry User does not allow
      creating agents; the next step up is Azure AI Project Manager.
- [ ] **Role Based Access Control Administrator** on the replay Function's
      resource group, for you (not CI), alongside the Contributor you deploy
      with (Owner alone covers both). With both, step 3's deploy assigns the
      identity its storage role itself.

## 3. Replay server (the Function)

- [x] `REPLAY_SERVER_URL` and `REPLAY_TOKEN` on the `staging` and `prod`
      GitHub environments.
- [ ] Redeploy (patch 78 changed the package; the deployed one cannot
      start). Set the **same** token locally first -- a new one locks the
      gate out:

      ```powershell
      $env:REPLAY_TOKEN = '<the value in the GitHub environments>'
      .\functions\replay-mcp\deploy.ps1 -ResourceGroup <rg> -Location <region>
      ```
      ```bash
      export REPLAY_TOKEN='<the value in the GitHub environments>'
      ./functions/replay-mcp/deploy.sh <rg> <region>
      ```

      Default `storageAuth=identity`: the template assigns the identity its
      storage role (step 2's role), and the key and SAS drop out of the app's
      settings. The region can be left off when the resources sit in the
      resource group's own region. The script runs verify at the end.
- [ ] Verify passes: exit 0 and `state after replaying: IdentityBlobStore
      (managed identity)`. Rerun it on its own with
      `python functions/replay-mcp/verify.py https://<app>.azurewebsites.net -g <rg>`.

      A role assigned directly to an identity takes **up to ~10 minutes**.
      The same identity carries the host's storage and the package download,
      so in that window the publish can fail or the app may not start --
      run the same deploy again after ten minutes. Verify may report replay
      state `unavailable` with a 403 for the rest of the window; that clears
      by itself. Still failing after 15 minutes: send verify's full output.

## 4. GitHub

- [ ] **The agents repository's staging environment.** The gate is a
      reusable workflow, so `environment:`, `vars.*` and the OIDC subject all
      belong to the **caller**. In the agents repository, environment
      `staging`:
      - vars: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
        `AZURE_AI_PROJECT_ENDPOINT`, `REPLAY_SERVER_URL`
        (`AZURE_JUDGE_DEPLOYMENT` optional)
      - secrets: `REPLAY_TOKEN`, `LOG_ANALYTICS_WORKSPACE_ID`

      The same values as this repository's `staging` environment. The gate's
      first step lists anything missing, before it logs in.
- [ ] **A federated credential for the agents repository** on the staging CI
      app: subject `repo:verve-it/<agents-repo>:environment:staging`. The
      existing one trusts this repository only.
- [ ] Copy `docs/agent-gate-caller.yml` into the agents repository as
      `.github/workflows/deploy-agents.yml`, replacing its two placeholder
      steps with the existing staging and prod deploy commands. It runs
      deploy-staging, then the gate against staging, then deploy-prod with
      `needs: gate`. The gate tests the **latest** version in the project it
      points at, so it must run after the staging deploy and never be pointed
      at prod.
- [ ] Push `agent-gate.yml` to `main` (the caller pins `@main`), then a first
      run: the gate should pass on an unchanged agent.
- [ ] Optional: install the Claude GitHub App on this repository if you want
      PRs watched and CI failures picked up automatically.

## 5. Data and security

- [x] Leaked blobs purged from history.
- [x] Repository visibility decided.

## 6. Test data: a known-good recording

Today the gate's only single-agent recordings are the two worst ops runs, so
it guards against making them worse. A clean one lets it guard a good one.

- [ ] Run `docs/queries/clean-agent-runs.kql` in the Log Analytics workspace
      (14 days, `connectwise-operations-agent`; both are `let`s at the top).
      It returns operation ids and counts only, no content -- paste me the
      top few rows.
- [ ] Export and scrub the one we pick, locally. The raw export carries
      customer data and never leaves your machine unscrubbed:

      ```powershell
      $env:SCRUB_SALT = '<the repo salt>'
      python export_traces.py --workspace <workspace-id> --operation-ids <id> -o out\clean-ops.json
      python scrub_trace.py out\clean-ops.json --learn out\redact.json
      # review out\redact.json: delete anything that is vocabulary, not data
      python scrub_trace.py out\clean-ops.json --redact-file out\redact.json -o traces\2026-09-23-ops-clean.json --verify
      ```

      Send me `traces\2026-09-23-ops-clean.json`; I build the cassette and
      baseline from it. `--verify` fails if the scrub changed any verdict.
