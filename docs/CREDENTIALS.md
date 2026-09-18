# Credentials

Three separate identities, and keeping them separate is the point.

| what | identity | secret? |
|---|---|---|
| You, running a script locally | your own account, via `az login` | none |
| CI → Foundry (staging) | app registration, federated | **none** |
| CI → Foundry (prod) | app registration, federated | **none** |
| Anything → Dataverse | app registration + application user | client secret |

Local runs need no app registration at all — every script uses
`DefaultAzureCredential`, which picks up your `az login` session. This document
covers the other two.

---

## 1. CI → Foundry

`.github/workflows/evals.yml` is already wired for this. It sets
`id-token: write` and uses `azure/login@v2` with
`vars.AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_SUBSCRIPTION_ID`. Those
variables are simply unset. No secret is involved: the workflow exchanges a
GitHub OIDC token for an Azure one.

### The one thing that is easy to get wrong

The Azure-touching jobs run **under a GitHub environment**:

```yaml
    environment:
      name: ${{ matrix.environment }}     # staging | prod
```

When a job declares an environment, the OIDC token's subject claim is

```
repo:verve-it/automation-solutions-evaluations:environment:staging
```

**not** the branch form `…:ref:refs/heads/staging`. Almost every tutorial shows
the branch form, because most jobs do not use environments. Configure the
branch subject here and `azure/login` fails with `AADSTS70021: No matching
federated identity record found` — which reads like a broken client id rather
than a wrong subject.

The nightly and weekly schedules go through the same environments, so the
branch form is not needed at all. The `frozen-sets` job touches no Azure and
needs no identity.

### Two registrations, not one

`staging` targets the `automation-solutions-test` project and `prod` targets
`automation-solutions`. GitHub variables are environment-scoped, so each
environment can carry a different `AZURE_CLIENT_ID`. Use two apps: the staging
identity then cannot reach production, which is the whole reason the split
exists. One app with two federated credentials also works and gives that up.

### Doing it

Per environment — run once with `staging`, once with `prod`:

```bash
ENV=staging                       # then repeat with: ENV=prod
REPO=verve-it/automation-solutions-evaluations

APP_ID=$(az ad app create --display-name "evals-ci-$ENV" --query appId -o tsv)
az ad sp create --id "$APP_ID"

az ad app federated-credential create --id "$APP_ID" --parameters "{
  \"name\": \"github-$ENV\",
  \"issuer\": \"https://token.actions.githubusercontent.com\",
  \"subject\": \"repo:$REPO:environment:$ENV\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"

echo "AZURE_CLIENT_ID for $ENV: $APP_ID"
```

Then give each app the **Foundry User** role on its own project — renamed from
`Azure AI User`, same role id and permissions. It is the least-privilege
developer role and covers data-plane evaluation runs, which is all CI does.
Scope it to the project resource, not the subscription.

Finally, in GitHub → Settings → Environments → `staging` (and `prod`) →
Environment variables:

| variable | value |
|---|---|
| `AZURE_CLIENT_ID` | that environment's app id |
| `AZURE_TENANT_ID` | your tenant id |
| `AZURE_SUBSCRIPTION_ID` | the subscription holding the project |
| `AZURE_AI_PROJECT_ENDPOINT` | `https://<resource>.services.ai.azure.com/api/projects/<project>` |
| `AZURE_JUDGE_DEPLOYMENT` | the judge model deployment name (weekly job only) |

Variables, not secrets — none of these is sensitive, and a client id with no
secret and a subject-scoped trust is not a credential on its own.

### Checking it

Push a branch, then run the workflow with `workflow_dispatch` against
`staging`. The `az login` step either succeeds or names the subject it
expected, which is the fastest way to confirm the claim format.

---

## 2. Dataverse

Different model, and the difference is where people lose an afternoon.

### An app registration alone grants nothing

Entra authenticates the app; **Dataverse authorises it separately**, through
its own security roles. Creating the registration and stopping there produces
401s that look like network failures.

```bash
APP_ID=$(az ad app create --display-name "evals-dataverse-reader" --query appId -o tsv)
az ad sp create --id "$APP_ID"
az ad app credential reset --id "$APP_ID" --years 1 --query password -o tsv
```

Keep that secret in a password manager, and as a GitHub **secret** (not a
variable) if CI ever needs it. It is a real credential, unlike the CI client
ids above.

### You do not need `user_impersonation`

Most guides tell you to add the Dynamics CRM `user_impersonation` **delegated**
permission and grant admin consent. For app-only (client credentials) access
that is wrong and unnecessary: there is no user to impersonate, and
authorisation comes from the application user's security roles inside
Dataverse. Skip the API permissions blade entirely.

### The step that actually grants access

Power Platform Admin Center → your environment → **Settings** →
**Users + permissions** → **Application users** → **+ New app user**:

1. **Add an app** — search by the client id above.
2. **Business unit** — the root one is fine.
3. **Security roles** — assign a role.

On roles: do not use System Administrator. This repo only ever reads. Create a
custom role granting **Read** (organisation scope) on the four tables in
handoff §8 — AI Orchestration, AI Run, AI Decision, AI Review — and nothing
else. If the loader later needs to call a bound function or action rather than
plain OData reads, `Service Writer` is the role to add, but read access does
not require it.

### Values the loader needs

| variable | value |
|---|---|
| `DATAVERSE_URL` | `https://<org>.crm<N>.dynamics.com` |
| `DATAVERSE_CLIENT_ID` | the app id above |
| `DATAVERSE_CLIENT_SECRET` | the secret above — **secret, never a variable** |
| `DATAVERSE_TENANT_ID` | your tenant id |

The token scope is `{DATAVERSE_URL}/.default`, not a Graph scope — a Graph
token against Dataverse returns 401 with no useful detail.

### Checking it

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "$DATAVERSE_URL/api/data/v9.2/WhoAmI" | jq
```

`WhoAmI` returning a `UserId` means the application user exists and the role
stuck. A 401 here means step 2 was skipped; a 403 on a table query means the
role is too narrow.

---

## Why three identities

The Dataverse app holds a long-lived secret and can read customer decisions.
The CI apps hold no secret and can only score evaluations. Folding them
together would give whatever can read customer data a standing path into
Foundry, and give a CI compromise a path into customer records. Neither is
worth the one registration it saves.
