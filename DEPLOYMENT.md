# Deploy Hermes Foundry to a new Microsoft organization

This guide describes the deployment sequence used for the working Hermes Foundry proof of concept.
It is written for an administrator who is new to Azure, Microsoft Foundry, Microsoft Entra, and
Microsoft 365.

> [!IMPORTANT]
> This is the lower-level update and troubleshooting reference. A Microsoft 365 administrator
> installing a prepared blueprint should start with the no-CLI
> [TENANT_ADMIN_INSTALLATION.md](TENANT_ADMIN_INSTALLATION.md). An Azure operator creating the
> runtime, a fresh hosted-agent blueprint, and its Agent 365 package should start with
> [ADVANCED_TENANT_DEPLOYMENT.md](ADVANCED_TENANT_DEPLOYMENT.md).

The procedure records what worked, including the quota, role, permission-inheritance, identity,
and hosted-container problems encountered during the original deployment. Microsoft changes these
preview and recently released services frequently. The tenant-admin guide's prerequisites were
checked against Microsoft documentation on 2026-09-04; verify them again before purchasing licenses.

### Reinstalling an existing application

To reinstall the current application into the selected azd environment, run:

```sh
./deploy.sh --force
```

`./setup.sh --force` is equivalent. The installer archives the previous checkpoint
under the ignored `deployment-output/setup-history/` directory, prompts for settings,
and reprovisions the infrastructure. If the agent already exists with the matching
blueprint, it rebuilds the current source, creates a replacement version with the
configured inference/egress settings, verifies it, and **switches live traffic to it**.
The previous version is retained for rollback. If the agent is absent, setup creates it.
It then continues publication, permission reconciliation, and guided acceptance.

This is an in-place reinstall: identities, existing instances, and stored data are
preserved. It does not delete the resource group, buy licenses, or recreate tenant
accounts. A mismatched identity stops the reinstall; it cannot silently replace an
unrelated blueprint. Select the azd environment belonging to the app you intend to
reinstall. This does not automatically upgrade every Hermes app in the tenant.

The archive is private; do not commit it. The installer lock prevents replacing an
active installer's checkpoint. Normal provisioning/approval checks still apply.
After interruption use `./deploy.sh --continue`; the checkpoint retains reinstall
intent. A retry during provisioning may build another version; old versions remain.
Use `build.sh` and `new-version.sh` if you want a separately reviewed image update
without automatic activation. Do not combine `--force` with other setup modes.

## 1. What gets created

```text
One Microsoft Entra / Microsoft 365 tenant
  ├─ Human owner and administrators
  ├─ Agent 365 registration and governance
  ├─ Agent identity blueprint
  └─ Agent User instance with its own identity and M365 workloads

One Azure subscription in the same tenant
  └─ Resource group
      ├─ Microsoft Foundry account and project
      ├─ Primary and fallback model deployments
      ├─ Foundry hosted agent and version
      ├─ Azure Container Registry
      ├─ Azure Bot used by the M365/Teams channel
      ├─ Application Insights and Log Analytics
      └─ Optional Key Vault for the blueprint's non-turn OBO credential
```

Foundry hosts the combined edge, gateway, Hermes runtime, and Microsoft 365 sidecar container. The
Agent User is a distinct Entra identity; it is not the human owner's identity and it is not an
ordinary on-behalf-of application.

## 2. Understand the three control planes

These are separate products with separate permissions and billing:

| Area | What it controls | Primary portal |
| --- | --- | --- |
| Microsoft 365 / Agent 365 | Agent registration, approval, owner, licenses, Teams, mailbox, OneDrive, governance | [Microsoft 365 admin center](https://admin.cloud.microsoft/) |
| Microsoft Entra | Tenant identities, blueprint, Agent User, consent, inherited permissions | [Microsoft Entra admin center](https://entra.microsoft.com/) |
| Azure / Foundry | Subscription, models, registry, hosted container, logs, usage charges | [Azure portal](https://portal.azure.com/) and [Microsoft Foundry](https://ai.azure.com/) |

An Azure `Owner` role does not grant Foundry data-plane access. A Microsoft 365 `Global
Administrator` role does not grant access to Azure resources. For a first POC, one person can hold
both sets of roles; production deployments should separate them.

## 3. People and roles needed

Have these people available before starting:

- A Microsoft 365 Global Administrator or AI Administrator to enable Agent 365, approve the agent,
  and manage tenant policy. Some identity and consent operations still require Global Administrator.
- An Azure subscription Owner, or a combination of Contributor and Role Based Access Control
  Administrator, to create resources and role assignments.
- A deployment operator with `Foundry Project Manager` on the Foundry project. Azure Owner or
  Contributor alone is insufficient to create and update hosted-agent versions through the
  Foundry data plane.
- A licensed human owner for the agent. The owner must have the Microsoft 365 workloads the agent
  is expected to use and a Teams entitlement if Teams testing is required.

For the first isolated POC, using a dedicated administrator/operator account is simpler. Require
MFA, do not share it, and reduce its roles after deployment.

## 4. Licenses and billing

There is no single "Foundry license." Two independent purchases are involved.

### 4.1 Microsoft 365 and Agent 365

Microsoft currently offers Agent 365 as a standalone subscription for eligible Microsoft 365
plans and includes it in Microsoft 365 E7. Agent 365 is generally licensed per human owner/user at
general availability; agents owned or managed by that licensed user do not each require an E7 or
Agent 365 license.

Hermes Foundry currently relies on the Agent User / own-identity experience. Microsoft still lists
"agents with their own identity" under the Agent 365 **Frontier preview**. A tenant using that
preview currently needs at least one of:

- Microsoft Agent 365, including the entitlement in Microsoft 365 E7; or
- Microsoft 365 Copilot on an eligible base plan.

The Global Administrator must enroll the tenant in Agent 365 Frontier and accept its terms. The
admin center should then show 25 Frontier licenses for AI teammates. In the preview, a license is
assigned per agent instance. Microsoft states that Frontier has no production SLA and can change or
be withdrawn, so this POC should not be represented as a supported production service.

Recommended purchase choices for a new POC are:

1. A Microsoft 365 plan that includes Exchange Online, SharePoint/OneDrive, and the collaboration
   workloads you intend to test. Microsoft recommends E5 for the fullest Agent 365 experience; an
   eligible Business or Enterprise plan can be sufficient for a smaller POC.
2. Agent 365 standalone for the human owner, or Microsoft 365 E7. If the required own-identity UI is
   available only through Frontier in the tenant, enroll in Frontier as described above.
3. A Teams license where Teams is not included in the selected plan or region. This is especially
   important for EEA tenants, where Teams packaging can differ.

Do not buy 25 normal employee seats merely because Frontier displays 25 AI-teammate seats. Do not
assume a similarly named Copilot SKU satisfies the gate; check the current prerequisites in the
tenant's purchase screen or with the Microsoft/reseller account team.

Useful current references:

- [Microsoft Agent 365 service description](https://learn.microsoft.com/en-us/office365/servicedescriptions/microsoft-agent-365/microsoft-agent-365)
- [Agent 365 Frontier enrollment and licensing](https://learn.microsoft.com/en-us/microsoft-agent-365/frontier)
- [Microsoft 365 plan comparison](https://www.microsoft.com/en-us/microsoft-365/enterprise/microsoft365-plans-and-pricing)

### 4.2 Azure

Create a pay-as-you-go or enterprise Azure subscription in the **same Entra tenant** as the
Microsoft 365 organization. Azure charges separately for model tokens, hosted-agent compute,
registry storage/builds, and logging. Application Insights and Log Analytics can produce ongoing
ingestion charges.

Set an Azure budget and alerts before deployment. Model quota is a regional shared rate limit, not
a reserved monthly purchase; requesting excessive model capacity is unnecessary and can prevent a
second deployment in the same region.

## 5. Create the Microsoft 365 organization

If the organization already has a Microsoft 365 tenant, skip to step 5.4.

1. Purchase the selected Microsoft 365 plan and create the tenant. Choose the initial
   `<organization>.onmicrosoft.com` name carefully; it becomes part of identity names.
2. Optional: add the organization's custom domain in **Microsoft 365 admin center → Settings →
   Domains** and create the DNS records Microsoft provides.
3. Create a named administrator account on the organization's domain, enable MFA, and assign the
   temporary roles needed for this deployment.
4. Purchase/assign the Agent 365 or E7 entitlement to the human who will own the agent.
5. If the own-identity capability still requires Frontier in this tenant, go to **Agents →
   Overview**, select the Frontier offer, accept the Agent 365 terms, and wait for the AI-teammate
   licenses to appear under **Billing → Licenses**.
6. Under **Agents → Settings**, verify that the tenant allows the relevant custom/partner agent
   type. Start with a small pilot group instead of all users.

Tenant changes can take from minutes to several hours to propagate. Record the tenant ID and the
primary `onmicrosoft.com` domain in a secure deployment worksheet, not in this repository.

## 6. Create and prepare the Azure subscription

1. In [Azure portal](https://portal.azure.com/), create or select a subscription associated with
   the same Entra tenant.
2. Open **Subscriptions → the subscription → Access control (IAM)** and give the deployment
   operator the required temporary Azure role.
3. Register the resource providers used by the proven stack:

   ```bash
   for provider in \
     Microsoft.CognitiveServices \
     Microsoft.ContainerRegistry \
     Microsoft.Insights \
     Microsoft.OperationalInsights \
     Microsoft.BotService
   do
     az provider register --namespace "$provider"
   done
   ```

   Register `Microsoft.KeyVault` if the optional non-turn Agent OBO credential will be configured.
   The current single-container Foundry architecture does not require Azure Container Apps.

4. Choose a Foundry region that supports hosted agents and the desired model deployments. Confirm
   quota before provisioning:

   ```bash
   az cognitiveservices usage list \
     --location "<azure-region>" \
     --subscription "<subscription-id>" \
     --output table
   ```

Model availability and quota vary by subscription and region. Do not copy the capacity used by a
different subscription without checking the shared regional pool.

## 7. Install deployment tools

Use a local macOS, Linux, or Windows workstation. Install:

- Git
- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli), version 2.80 or later
- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/install-azd)
- Python 3
- OpenSSL

`setup.sh` installs the Azure CLIs, Python 3, and OpenSSL when they are missing. Docker, PowerShell,
and .NET are not required locally because the current Dockerfile compiles the edge during the ACR
cloud build.

Verify them:

```bash
az version
azd version
python3 --version
openssl version
git --version
```

Then sign in explicitly to the correct tenant and subscription:

```bash
az login --tenant "<tenant-id>"
az account set --subscription "<subscription-id>"
azd auth login --tenant-id "<tenant-id>"
az account show --query '{tenant:tenantId,subscription:id,name:name}' --output table
```

Never proceed until the last command shows the intended tenant and subscription.

## 8. Bootstrap the first Foundry and Agent 365 stack

### Current repository limitation

The original working stack was bootstrapped with a modified version of Microsoft's Foundry
autopilot sample. The modifications were not cosmetic. They added or corrected:

- tenant-neutral unique resource names;
- a configurable model-capacity parameter;
- current Foundry data-plane role assignments and propagation handling;
- an Azure Bot and Agent 365 blueprint;
- both `activity` and `responses` container protocols;
- `BotServiceTenant` authorization for Microsoft 365 delivery;
- Graph permission grants, plus optional first-party Microsoft 365 MCP grants for the explicit
  non-production diagnostic mode;
- matching inheritable Graph permissions on the blueprint;
- the complete Hermes identity, owner, model, tool, and routing environment; and
- safe post-provision creation of the initial hosted-agent version.

That sanitized infrastructure package is not yet in this repository. The official sample is useful
background, but using it unmodified does **not** reproduce the working Hermes deployment. The
package must be added before an external administrator can execute this section independently.

Official baseline:
[Microsoft Foundry autopilot sample](https://github.com/microsoft-foundry/foundry-samples/tree/main/samples/csharp/foundry-autopilot-agent).

### Sequence performed by the working bootstrap

The installer must perform these operations in this order:

1. Create a resource group, Foundry account/project, ACR, model deployment(s), Application
   Insights, Log Analytics, Azure Bot, hosted agent, and required project connections.
2. Give the Foundry project's managed identity `Container Registry Repository Reader` (or
   `AcrPull`) on the ACR and `Foundry User` on the Foundry account for model access.
3. Give the deployment operator `Foundry Project Manager` on the project. In the original
   deployment, the post-provision hook also needed `Foundry Owner` and `Foundry Account Owner` at
   the resource-group scope; wait for role propagation before retrying a failed data-plane step.
4. Create the hosted agent and its first version. The version must expose both `activity` and
   `responses`, use `BotServiceTenant`, and include the complete environment listed in the
   repository README.
5. Create and connect the Agent 365 blueprint/Azure Bot, grant the required Microsoft 365
   permissions, and mark the matching Graph scopes inheritable.
6. Emit only a redacted deployment summary. Store tenant IDs, resource IDs, blueprint IDs, owner
   addresses, and generated signing values in the deployment environment or secret store, never in
   source control.

The original deployment also proved two timing rules:

- Model capacity is drawn from a shared regional pool. A provisioning request can fail even when
  no resource is otherwise misconfigured; select a capacity the subscription can actually supply.
- Foundry role assignments can take several minutes to affect data-plane calls. A 403 immediately
  after a role assignment is not proof that the role is wrong.

Microsoft's current hosted-agent permissions reference is the authority for role selection:
[Hosted agent permissions](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-permissions).

## 9. Configure blueprint permissions correctly

There are two independent operations for every Microsoft Graph capability:

1. Grant/admin-consent the delegated scope to the blueprint service principal.
2. Declare the same scope inheritable so Agent User instances can receive it.

Doing only the first can make a permission look correct in Entra while the Agent User still gets a
403. This was the root cause of the original OneDrive failure after `Files.ReadWrite.All` had already
been granted.

The exact permission set must be reviewed per tenant. The normal Hermes path uses Graph capabilities
for mail, calendar, files, sites, user lookup, sharing, and document operations. First-party
Microsoft 365 MCP scopes are needed only if the explicit non-production `ResponsesApi` diagnostic
mode is enabled. Treat generic Graph writes as a privileged capability and remove anything the
deployment does not need.

After consent, verify effective inheritance per resource. A summary such as "4 of 5 effective" is
not success if Microsoft Graph is the missing resource.

Microsoft reference:
[Configure inheritable permissions for agent identity blueprints](https://learn.microsoft.com/en-us/entra/agent-id/configure-inheritable-permissions-blueprints).

## 10. Build and publish the Hermes container

This section is runnable from a clean clone once section 8 has produced an ACR.

```bash
git clone https://github.com/NousResearch/hermes-foundry.git
cd hermes-foundry

export REG="<acr-name-without-azurecr-io>"
./build.sh v1
```

`build.sh` submits an ACR cloud build. The Dockerfile compiles the .NET edge in a build stage and
copies the committed gateway and sidecar directly from `src/`. The image tag is later resolved to
an immutable digest.

Verify the image exists:

```bash
az acr repository show \
  --name "$REG" \
  --image "<image-repository-shown-by-build>:v1" \
  --query '{digest:digest,created:createdTime}' \
  --output table
```

## 11. Create a Hermes hosted-agent version

`new-version.sh` deliberately clones the currently serving definition. It is an **upgrade tool**,
not an initial installer. Run it only after the bootstrap has produced one complete serving version.

Record the coordinates from the bootstrap output, then run:

```bash
export ACCOUNT="<foundry-account-name>"
export PROJECT="<foundry-project-name>"
export AGENT="<hosted-agent-name>"
export REG="<acr-name>"
export PRIMARY_MODEL="<primary-model-deployment-name>"
export FALLBACK_MODEL="<fallback-model-deployment-name>"

./new-version.sh v1
```

Do not assume the script's development model names exist in another tenant. Create compatible model
deployments first and pass their actual deployment names. For a minimal smoke test, the same
compatible deployment can be supplied as both primary and fallback.

The script creates and validates a new version but does not route traffic. It prints the explicit
routing command. Keep the old serving version as a rollback target until Microsoft 365 operations
have been tested.

## 12. Publish to Store and instantiate the Agent User

1. Open [Agents > Requests](https://admin.cloud.microsoft/#/agents/all/requested), find the requested
   **Hermes Agent on Foundry** blueprint, review its permissions, restrict hiring scope to the pilot users/group, and select **Publish to Store**.
   Under **Activate (optional)** > **Select users who can create agent instances**, choose
   **None** (no users), **All** (all users), or **Specific** (selected users/groups).
   Use **Specific** for the pilot. This setting does not create an instance automatically.
2. Return to the terminal running `./setup.sh` and press **Enter** when prompted. If setup
   stopped, run `./setup.sh --continue`. Follow the separate Microsoft browser sign-in prompt
   so setup can connect Teams messages to the deployed Hermes agent. Setup configures the
   Bot Based binding and verifies it automatically; no manual Bot ID entry is needed.
3. Wait for setup to verify the connection and hosted endpoint, then follow its instructions
   to create an agent instance and assign its human owner.

If you ran `azd` directly, use the manual connection procedure in
[Tenant administrator installation, Step 6](TENANT_ADMIN_INSTALLATION.md#step-6-connect-the-foundry-hosted-agent-to-its-azure-bot).

Do not substitute a raw Graph-created Entra identity for this step. An identity created directly
through the agent identity API can exist in Entra without the fully provisioned Microsoft 365
workloads expected by this project.

Mailbox and OneDrive provisioning is asynchronous. It often completes in minutes but can take much
longer. If the agent can identify itself but OneDrive reports that the personal site is absent,
wait and check provisioning state before changing permissions.

Record these values in the hosted-agent environment through the installer, not in Git:

- Agent blueprint application ID
- Agent instance/object ID and UPN
- Entra tenant ID
- Human owner UPN and object ID
- Tenant domain

Create a new hosted-agent version after those values are available. Never print signing keys or
certificate material in deployment output.

## 13. Test in the correct order

Use a new chat/session so it cannot remain pinned to an older hosted-agent version.

1. Ask the agent to state its own identity. Confirm it uses the Agent User, not the human owner.
2. Ask it to list the last three messages in **its own** mailbox.
3. Ask it to send a harmless email to the owner and confirm receipt.
4. Ask it to create a uniquely named text file in **its own** OneDrive, read it back, and share it
   with the owner.
5. Trigger one native Microsoft 365 MCP read if that capability is enabled.
6. Inspect tool results and logs. Do not accept a model's narrated success without an explicit tool
   success response.

For Graph diagnostics, first query the Agent User and then address the known drive/item directly.
Do not use `/me/drive` to diagnose a drive that may not yet be provisioned.

Hosted-agent sessions can stay pinned to their original version. Routing a new version does not
rewrite an already active session. Disable/re-enable or start a genuinely new session before
concluding that a deployment change had no effect.

## 14. Routines and non-turn identity

Interactive turns and scheduled invocations enter the same Hermes image through different
protocols. An Activity turn contains trusted Agent 365 instance metadata. A Foundry Routine has no
human turn, so the edge verifies the signed instance/task binding stored when the owner created the
Routine and uses Microsoft Identity Web's agent-specific OBO flow to select the child Agent Identity
and its one-to-one Agent User. Entra issues a fresh short-lived Graph token for that execution. The
Routine does not store a human credential, Graph token, or refresh token.

The blueprint must authenticate as a confidential client for this flow. In the tested setup, an
administrator registers one X.509 public certificate on the blueprint, stores its private key in
Azure Key Vault, and grants the hosted runtime permission to read it. Configure
`RoutineCredentialVaultUri` and `RoutineCredentialCertificateName` on the hosted-agent version. The
administrator performs this once per deployment and rotates/revokes the credential according to
tenant policy, not once per Routine.

Routine execution and Routine management are separate authorization boundaries. Calling the
Foundry Routine data plane requires **Foundry User** or higher at project scope. Child Agent
Identities do not inherit Azure project RBAC from their blueprint. Install the included reconciler
once per Hermes blueprint/project after the base stack exists:

```bash
./scripts/install-agent-rbac-automation.sh \
  --resource-group <resource-group> \
  --account <foundry-account> \
  --project <foundry-project> \
  --blueprint-client-id <blueprint-client-id>
```

The installer deploys a Consumption Logic App with a system-assigned managed identity. Every five
minutes it discovers new Agent Identities belonging to the configured blueprint and grants only
**Foundry User** at that project. It does not provision Agent Users, Microsoft 365 workloads,
mailboxes, OneDrive, or Teams identities. Do not assign roles manually to make an instance test pass;
that hides a broken tenant-wide provisioning path.

The deployment must also provide one stable `RoutineBindingSignKey` through tenant-controlled secret
automation. It signs identity/task bindings; it is not a Graph credential. For prerequisites,
least-privilege boundaries, verification, cost, and removal, follow
[ROUTINE_RBAC_AUTOMATION.md](ROUTINE_RBAC_AUTOMATION.md).

Hermes/OS cron is disabled. Routine requests require an explicit IANA time zone; Hermes asks the
owner when none has been stored and remembers the answer in that Agent User's durable OneDrive
memory. Foundry Routines remain a preview service, so validate downstream tool completion rather
than treating dispatch status alone as proof that the requested business action finished.

## 15. What the working deployment taught us

These implementation fixes are already in the repository and should not be removed during
"simplification":

- Foundry wraps the image, so an init system cannot assume it is PID 1.
- The edge must bind its readiness port before Hermes finishes its slower startup.
- The gateway must tolerate the first request arriving while Hermes is still starting.
- Every internal port is pinned because Foundry injects a public `PORT` value.
- The .NET runtime comes from the official runtime image rather than a runtime install script.
- Loopback service keys are generated together so the edge, gateway, sidecar, and Hermes cannot
  silently disagree.
- Hosted agents use the project-scoped OpenAI endpoint for implicit project inference. Do not switch
  to the account-level Cognitive Services endpoint unless every runtime identity is deliberately
  granted the additional model-access RBAC it requires.
- Both `activity` and `responses` protocols are required: interactive M365 delivery uses the former
  and Foundry routines use the latter.
- `AgentLogic=ResponsesApi` is diagnostic compatibility mode, not the Hermes production path.

## 16. Release blockers for a true beginner installer

Before this guide can be called a complete external deployment process, the repository still needs:

- the sanitized, tenant-neutral Bicep/`azd` bootstrap used by the working deployment;
- an idempotent blueprint permission and inheritance step with least-privilege profiles;
- an initial-version command that collects and validates all identity/owner environment values;
- preflight checks for tenant licensing, Frontier/own-identity availability, providers, roles,
  regional model availability, and quota;
- safe approval/instance handoff instructions that match the current Agent 365 UI;
- a redacted deployment summary and validation command;
- an uninstall/rollback procedure; and
- production hardening for Routine binding revocation, signing-key rotation, deduplication, and
  downstream completion telemetry.

Until those are committed and tested in a second clean tenant, deployment is maintainer-assisted.

## 17. Troubleshooting quick reference

| Symptom | First thing to check |
| --- | --- |
| `403` creating/updating hosted agent | `Foundry Project Manager` data-plane role and propagation time; Azure Owner alone is not enough |
| ACR image cannot be pulled | Project managed identity has Repository Reader/`AcrPull` on that ACR |
| Model call returns 401 | Confirm `AZURE_FOUNDRY_BASE_URL` is the project-scoped `/openai/v1` endpoint; account-level endpoints require separate model RBAC |
| Model rate limit on first/second turn | Deployment TPM capacity is below a Hermes prompt/tool loop or subscription regional quota is exhausted |
| Graph scope appears granted but tool gets 403 | The same scope was not declared inheritable on the blueprint |
| OneDrive personal site/drive not found | Agent User workload provisioning is incomplete; wait before changing scopes |
| New code appears unchanged | The conversation is pinned to an older hosted-agent version |
| Container never becomes ready | Edge did not bind early, port collision, missing runtime, or startup process assumed PID 1 |
| Chat works but M365 tools fail | Agent identity environment, token exchange, Graph inheritance, or internal gateway/sidecar key mismatch |
| Routine creation returns 403 | Confirm the blueprint/project RBAC reconciler is installed and the Agent Identity has **Foundry User** at project scope |
| Routine dispatches but M365 action fails | Verify the signed instance binding, blueprint credential, Agent User relationship, and inheritable Graph permissions |

Microsoft references:

- [Deploy a hosted agent](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent)
- [Hosted agent permissions](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-permissions)
- [Publish a Foundry agent to Microsoft 365](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/agent-365)
- [Agent 365 overview](https://learn.microsoft.com/en-us/microsoft-agent-365/overview)
- [Agent 365 Frontier](https://learn.microsoft.com/en-us/microsoft-agent-365/frontier)

### Outbound web and build security

Builds stage only the files explicitly listed in `config/build-context.json`. Both
`build.sh` and the azd post-provision hook use this staging path. Keep the manifest
and `.dockerignore` allowlist in sync when adding runtime files. Local credentials,
private notes and other unlisted files are excluded even if Git ignores them.

Public page fetching connects directly to validated public IP addresses while
retaining TLS hostname verification. It does not use environment-configured HTTP
proxies. Every redirect is checked again. Graph download redirects are handled
without forwarding the Graph bearer token; unexpected download hosts fail closed.

Hosted browser tools are disabled by default. They require network-level egress
controls on the **remote browser environment** that deny private, loopback,
link-local, metadata and reserved destinations, for all protocols and after DNS
resolution. The sidecar cannot establish these controls through CDP. After an
administrator has configured and verified them, set
`BROWSER_PUBLIC_EGRESS_ENFORCED=true` in the hosted agent environment. This flag is
an administrator assertion, **not a switch that installs a firewall**. Do not set
it solely to make a failed browser call succeed. The installer does not set it.
Without that enforcement, continue using `fetch_url`; browser rendering and
screenshots remain unavailable.

When enabled, each browser call uses a new isolated context, blocks service
workers and WebSockets, and validates navigation and subresource URLs. These
application checks supplement remote egress controls; local DNS validation cannot
prove what address the remote browser will resolve or reach.

### Configurable Nous portal and Foundry inference

New installations default to Nous inference, with Foundry available as an explicit
alternative. Existing deployments keep Foundry until explicitly reconfigured.
Hosting, Microsoft 365 identity, and tools remain on Foundry.

`./deploy.sh` / `./setup.sh` prompt for the default provider, enabled providers,
Nous model IDs, and (when Foundry is enabled) the Azure model name to deploy; its version is resolved automatically from the Azure catalog. The Nous prompt defaults to `openai/gpt-6.1-sol`. Foundry defaults to Azure catalog
name `gpt-6.1-sol`, with its version resolved for the selected region.
Azure uses the model name without the `openai/` prefix. Saved choices
are preserved; explicit environment settings take precedence. Foundry model/version
choices are passed into Bicep, not merely written into the agent's prompt.
`new-version.sh` preserves the serving primary/fallback models unless overridden.

**Each user signs into their own Nous account through a browser.** Setup never asks
for a portal API key, secret-file path, or Key Vault secret. There is no shared
portal billing identity and no fallback to another user's credentials.

The gateway generates onboarding and authentication-error replies procedurally,
before any inference request. If Foundry is enabled, these replies also offer
`/provider azure-foundry` and `/model`. An unsigned-in agent can therefore guide
the user without relying on an LLM. Provider/model commands are handled by the edge.

After deployment, in a **one-to-one Teams chat** with the agent:

1. Send `/portal login` (the first Nous-backed message also offers sign-in).
2. Open the portal verification link and approve using your own portal account.
3. Send `/portal status` to finish the connection, then send your message.
4. Use `/portal logout` to disconnect your account from this agent.

Polling happens when you send a message or `/portal status`; no browser callback
or public callback server is needed. If a code expires, run `/portal login` again.
Group chats and scheduled jobs never display device codes. A portal account's
own subscription, billing organization, and model entitlements still apply.

| Setting | Meaning |
| --- | --- |
| `INFERENCE_PROVIDER` | Startup provider: `nous` or `azure-foundry`. |
| `INFERENCE_ALLOWED_PROVIDERS` | Comma-separated providers users may select. |
| `NOUS_MODEL` | Default Nous model ID. |
| `NOUS_MODELS` | Additional administrator-approved Nous model IDs. |
| `NOUS_INFERENCE_BASE_URL` | Production endpoint `https://inference-api.nousresearch.com/v1`; browser auth currently supports production only. |
| `FOUNDRY_MODEL_NAME` | Azure OpenAI catalog model to provision; setup offers an editable default. |
| `FOUNDRY_MODEL_VERSION` | Optional advanced version override. Setup automatically resolves the Azure catalog default version when unset. |
| `PRIMARY_MODEL`, `FALLBACK_MODEL` | Optional existing Foundry deployment-name overrides for `new-version.sh`. Absent overrides preserve the serving settings. |

Noninteractive setup can use these environment variables. Users still authorize
their own portal session after deployment. Legacy `NOUS_API_KEY_FILE` and
`NOUS_API_KEY_SECRET_URI` are removed from proposed agent versions and never used.

Chat controls:

- `/provider` displays the selected and enabled providers.
- `/provider nous` or `/provider azure-foundry` changes this conversation's provider.
- `/provider default` restores the administrator's default.
- `/model` lists permitted models; `/model <id>` selects one and `/model auto` resets it.

Foundry uses the agent's existing Entra inference access and requires no Nous login.
Nous model options are an administrator allowlist, not proof of portal entitlement.
Preferences are caller/session-scoped; authentication is caller/agent-scoped, so
conversations with the same verified caller ID and agent reuse that user's login.
Signing into a different agent requires a separate portal authorization. Portal
management/dashboard registration is not part of this inference integration.

OAuth access/refresh tokens and pending device codes are encrypted with AES-GCM
in separate records in the Agent User's OneDrive app folder. The encryption context
binds tenant, agent, caller object ID, and inference endpoint. `IdentitySignKey`
derives the encryption key and must remain stable across versions; rotating or
losing it makes saved portal sessions unreadable. These records are outside Hermes
memory/state archives. Plaintext tokens are never stored in agent configuration,
chat history, or logs. Runtime clients receive only the current turn's access token.

Refresh and device-token exchanges use OneDrive ETag compare-and-swap. Each rotating
credential is consumed in durable state before an exchange, preventing concurrent
containers from replaying it. An ambiguous crash can require the affected user to
sign in again after a 90-second in-progress window. Logout removes the stored
session and attempts portal revocation; already-running requests may finish with
their short-lived token. The Agent User needs a working OneDrive and its existing
Graph file permissions for durable sign-in; storage failures fail closed.

Scheduled routines use the administrator's provider/model default. Nous-backed
routines resolve the owner object ID from the verified routine binding and use that
owner's authorization for this agent. Missing identity/login fails closed; routines
never borrow the most recent chatter's account. With both providers enabled,
automatic cross-provider fallback is disabled to keep billing/data routing explicit.

Validation: `python scripts/run-python-tests.py`, .NET edge tests, and
`bash scripts/test-pinned-inference.sh`. The pinned-runtime test uses synthetic
caller tokens, verifies Alice → Foundry → Bob isolation, and rejects an anonymous turn.
A real user's browser approval and successful hosted inference are still required
for live end-to-end acceptance.

### Portal inference egress

`create-hosted-blueprint.sh` and `new-version.sh` run
`scripts/configure_inference_egress.py` before creating a Nous-enabled version.
**No manual policy creation or policy ID is required.** Both scripts create and
attach the policy as part of deployment, including in a fresh tenant with no
pre-existing custom policy. The script allows the **configured** inference hostname and `portal.nousresearch.com` for browser
authorization, token refresh, and revocation.

- For an existing Foundry managed network in `AllowOnlyApprovedOutbound` mode,
  it creates individual FQDN outbound rules and reads them back. It does not change
  the network isolation mode. Adding the first FQDN rule can provision a managed
  Azure Firewall and incur its charges.
- For hosted-agent egress, it clones the version's existing RAI policy, prepends
  exact-host Allow rules, and attaches the new policy to the proposed version.
  Existing content-safety settings, egress mode, default action, and other rules
  are retained; the shared source policy is not edited. `RAI_POLICY_ID` can select
  the source policy explicitly, including for new deployments.
- Without an existing policy, it creates an enforced policy with explicit inference
  allows and `defaultAction: Allow`. This preserves previously unrestricted tool
  access; it is **not** a new deny-by-default firewall. The new policy inherits
  `Microsoft.DefaultV2` content-safety defaults.
- Policy readback and agent-version attachment are checked before routing. A
  failure aborts deployment. New-version creation still does not switch traffic.

Hosted-agent egress uses Microsoft's preview RAI API `2026-05-15-preview`.
Managed-network rules use `2026-05-15-preview`, verified against the live Labs
account; the `2026-05-01` version in the managed-network guide was rejected there. The operator needs permission to read and
write those resources. Customer-managed VNet firewalls/NSGs and private Key Vault
access policies are separate controls; this script does not reconfigure those.
TLS clients must retain the sandbox's injected `SSL_CERT_FILE`,
`REQUESTS_CA_BUNDLE`, and other CA settings; never disable certificate validation.

Readback confirms configuration, not live connectivity. Test a fresh hosted
session after deploying: running sandboxes do not reload egress policy changes.
Complete `/portal login` in a fresh one-to-one Teams session and verify a Nous
response from that runtime. A workstation request does not verify hosted-agent egress.

Microsoft references: [hosted-agent egress and certificate handling](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/add-hosted-agent-guardrails#network-egress-controls-preview),
[managed-network outbound rules](https://learn.microsoft.com/en-us/azure/foundry/how-to/managed-virtual-network#manage-outbound-rules).


Model input formats in setup:

| Input | Format examples | Automatic normalization |
| --- | --- | --- |
| Default Nous portal model | `openai/gpt-6.1-sol`, `openai/gpt-5-mini`, `openai/gpt-4.1` | Bare GPT and o-series names receive `openai/`. Other IDs retain their exact provider/name. |
| Additional Nous models | `openai/gpt-6.1-sol`, `openai/gpt-5-mini`, `openai/gpt-4.1` | Enter a comma-separated list; each item is normalized and duplicates are removed. |
| Foundry OpenAI model | `gpt-6.1-sol`, `gpt-5-mini`, `gpt-4.1` | An optional `openai/` prefix is removed. Other provider prefixes are rejected by this OpenAI deployment flow. |

These examples illustrate format, not guaranteed availability in every account or
region. The portal uses provider-qualified IDs to identify the upstream provider;
Azure's deployment already specifies OpenAI separately. You can enter
`gpt-6.1-sol` or `openai/gpt-6.1-sol` in either model field.


When the selected azd environment identifies an existing Foundry project, setup
asks whether to reuse it before provisioning. Accepting updates that project in
place and preserves its identity; `--force` still rebuilds and reinstalls Hermes.
Declining pauses safely. The choice is saved for this installation so
`./setup.sh --continue` does not repeat the confirmation. Project and model
updates are serialized to avoid concurrent writes to the parent Foundry account.


Resuming a forced reinstall preserves completed work. A private checkpoint under
`deployment-output/reinstalls/` records the build inputs, image, rollback version,
and created version. Setup waits up to ten minutes for version readiness, validates
its requested definition, then rechecks the serving route before switching. If the
target is already serving, it validates without sending another PATCH. A lost create
response is reconciled against existing versions; ambiguous results are never blindly
reposted. Changed build inputs require a new `./deploy.sh --force` run.

`./setup.sh --continue` reuses the recorded build and version. Publication completion
is also recorded; an uncertain publication requires inspecting Microsoft 365 Requests
before resubmission. Graph and egress configuration use bounded readback waits without
repeating their writes. `HERMES_READY_TIMEOUT` and `HERMES_READY_POLL_INTERVAL` override
the default 600-second readiness timeout and five-second polling interval.

Routine RBAC automation uses a deterministic Logic App name derived from the selected project resource ID and Agent Identity Blueprint ID. A matching legacy installation is reused; a legacy workflow for a different target is left intact and a separate workflow is created. Removal discovers the same name automatically. An explicit `--workflow-name` must still match the requested target. Tenant, subscription, project, and blueprint IDs come from the selected environment and Azure responses; no existing customer IDs are required for a new tenant.

### Continue after Publish to Store

Wait until **Publish to Store** finishes. Publication makes Hermes available to the selected
users; setup must still configure where Teams sends their messages and verify the hosted endpoint.

1. Return to the terminal in this repository folder. If setup is waiting at **After Publish to
   Store finishes**, press **Enter**. If the command exited, run `./setup.sh --continue`.
   Completed provisioning will not repeat.
2. At **Press [Enter] to start the Microsoft sign-in**, press **Enter**.
3. Open the sign-in link printed by setup on the same computer. Sign in as the administrator
   for the deployment tenant and complete the requested consent and MFA.
4. Keep the terminal open. Setup saves and verifies the Teams connection automatically, then
   checks the hosted endpoint. Follow its remaining prompts; create an instance when setup asks.

The resume command requires the saved setup checkpoint. For an installation made directly with
`azd` without that checkpoint, use the manual connection procedure in the tenant administrator guide.

### Find Hermes in Teams after setup

After publication and successful connection checks, **Hermes Agent on Foundry** should become
available in Teams within a few minutes. Microsoft-side provisioning sometimes takes up to a
few hours; this is an observed delay, not a guaranteed deadline.

1. Open Teams and switch to the organization where Hermes was installed.
2. Select **Apps > Built for your org** and search for **Hermes Agent on Foundry**.
3. Open its card and select **Add**. Follow any instance-creation prompts shown by Teams.
4. Open the agent chat. Reuse your existing instance if you already created one.

If **Built for your org** is not displayed, search in **Apps**. If Hermes is missing, wait and
refresh Teams, then ask the administrator to check the selected audience and Teams app policy.
Microsoft documents the organization app category in
[Find apps in Teams](https://support.microsoft.com/en-us/teams/apps-service/find-teams-and-personal-apps-in-microsoft-teams).

### Deployment completion and safe recovery

`AZURE PROVISIONING PHASE FINISHED` means only that the Azure provisioning hook finished.
The setup wrapper still verifies the blueprint, Azure Bot, Teams connection, hosted endpoint,
and first-instance checks. An already approved blueprint skips the publication prompt.
A completed checkpoint reports `SETUP ALREADY COMPLETE` when its runtime checks still pass.

If setup reports an Azure Bot identity mismatch, publication may already be approved while the
Teams connection is still blocked. Do not publish again or delete the blueprint. Compare the
selected environment's Bot resource, application ID, tenant, blueprint, and Activity Protocol
endpoint. Correct the selected resource or deployment configuration before resuming. Setup
refuses to bind a different identity automatically.

Keep `.azure/`, `.deployment.local`, and `deployment-output/` private. They are excluded from
Git and container build inputs. Release records and upload ZIPs still describe a particular
installation; do not attach them to public issues. Before committing, run
`python3 scripts/audit-public-tree.py` to check candidate source files for secrets and local IDs.
