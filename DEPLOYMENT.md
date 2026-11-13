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

## 12. Approve and instantiate the Agent User

1. Open [Agents > Requests](https://admin.cloud.microsoft/#/agents/all/requested), find the requested
   blueprint, and approve it for only the pilot users/group.
2. Open [Agents > All agents](https://admin.cloud.microsoft/#/agents/all), select the blueprint,
   click **Enable**, and confirm that it is Available.
3. Open
   `https://dev.teams.microsoft.com/tools/agent-blueprint/<BLUEPRINT_ID>/configuration`, replacing
   `<BLUEPRINT_ID>` with the ID emitted by the bootstrap. Change **Agent Type** from
   **Notification/API Based** to **Bot Based**, set **Bot ID** to that same Blueprint ID, save, and
   verify the values survive a reload.
4. Only then create an agent instance through the Microsoft 365/Teams Agent 365 experience and
   assign its human owner.

The bootstrap prints the exact Developer Portal URL and Bot ID. Automatic mutation of this setting
is not part of the supported installer yet; a previously recorded Developer Portal API call returned
HTTP 403, so supported tenant-admin automation remains a research item rather than an assumed API.

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
