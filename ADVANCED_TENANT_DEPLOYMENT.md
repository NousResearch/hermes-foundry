# Advanced deployment of Hermes Agent on Foundry

This is a private, tenant-scoped custom-blueprint deployment. It does not create or submit a public
Marketplace, AppSource, or Teams Store listing. The target tenant's administrator performs the
deployment and Microsoft 365 approval using resources and identities belonging to that tenant.

This is the technical runbook for creating the Azure runtime, target-tenant blueprint, identity
configuration, and Agent 365 package behind **Hermes Agent on Foundry**. It is intended for Azure
deployment operators and identity administrators. A tenant administrator installing an already
prepared Hermes blueprint should use the no-CLI
[TENANT_ADMIN_INSTALLATION.md](TENANT_ADMIN_INSTALLATION.md) instead.

The procedure was checked against Microsoft's current Foundry Autopilot and Agent 365 documentation
on September 4, 2026. Autopilots with their own Agent User are still a Frontier feature, so verify
the linked Microsoft pages before a production purchase or rollout.

## Automated installation

For a new customer tenant, run the repository's tenant-neutral installer:

```bash
./setup.sh
```

`./deploy.sh` is an equivalent entry point, not a separate deployment implementation. Before Azure
provisioning, the script displays the license/enrollment, employee Teams, Azure IAM and model-quota
prerequisites with portal links and requires explicit confirmation. These are operator checks, not
an automated proof of Microsoft 365 eligibility or regional capacity.

After publication, setup pauses at Microsoft 365 **Requests**. Allow about five minutes for the
request to appear; rare delays can last a few hours. These are observed delays, not a guaranteed
service interval. Approve/activate the request at
`https://admin.cloud.microsoft/#/agents/all/requested`, then press **Enter** in setup.

To resume after closing the terminal, pending approval, or a failed step:

```bash
./setup.sh --continue
```

The saved tenant, subscription, environment, and deployment IDs are checked before continuing.
Completed provisioning is skipped; an interrupted provisioning phase retries the existing
deployment hooks. An existing-agent identity mismatch is not treated as a completed installation.
The checkpoint and local MSAL environment are private, Git-ignored files under `deployment-output`.
Do not delete the checkpoint to recover from an ambiguous app-creation result: inspect Entra first
to avoid duplicate objects. Only one setup process may run in this checkout at a time.

After Microsoft reports approval, setup requests a separate, administrator-specific delegated
consent for `AgentCommunicationConfiguration.ReadWrite` on a tenant-owned public-client setup app.
Creating that app and consent needs the administrator Graph permissions
`Application.ReadWrite.All` and `DelegatedPermissionGrant.ReadWrite.All`; Azure roles alone do not
provide them. Setup resolves the permission ID from Graph, rather than using tenant-specific IDs.
The local authentication dependency is MSAL 1.36.0, installed into a private virtual environment
if MSAL is not already available. On Debian/Ubuntu, Python's `venv` support must be installed.

Open the printed localhost link in your preferred browser on the setup computer, sign in as the
same administrator, and complete MFA. This uses authorization code + PKCE, not device-code login.
The script does not disable Security Defaults, persist tokens, or create client credentials.
This is the same authentication and Graph configuration path verified manually against a deployed
blueprint. Azure CLI authentication is used to prepare the setup app and its consent, not to request
the new communication scope through Microsoft's first-party CLI application. The separate browser
sign-in must return the same tenant and administrator identity before any binding is written.
It performs GET / PUT / GET on
`/beta/applications/{blueprintId}/microsoft.graph.agentIdentityBlueprint/communicationConfiguration`,
preserving other communication settings. Unknown fields or a readback mismatch stop setup.
Before authentication, setup also verifies the Azure Bot's tenant, application ID, and exact hosted-agent
Activity Protocol endpoint. Failed, expired, or wrong-account sign-ins retain the configuration phase;
rerun `./setup.sh --continue` for a fresh browser link, without rebuilding or republishing.
Microsoft documents this API as **beta**, not production-supported GA. Tenant-owned setup app
registration and consent can be removed after setup, but are not automatically deleted by it.

Automated tests exercise the local browser callback, sign-in failures, wrong identities, approval
gating, resume behavior, and configuration readback using mocked Microsoft services. They do not
replace a clean-tenant installation and Teams delivery acceptance test.

Setup then validates the active serving version and calls
`POST {projectEndpoint}/agents/{agentName}:enable?api-version=v1` if the endpoint is disabled and
the administrator confirms activation. It reads back enabled state with bounded polling; failed or
ambiguous writes retain the phase and are not blindly retried. A resumed run reads state first.
It never changes another blueprint or route to make this check pass. Disabled is an endpoint lifecycle
state, not scale-to-zero. See [Microsoft's lifecycle API](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-agent#disable-or-enable-an-agent).

Finally the installer guides Microsoft-managed instance creation, asks for the instance's email,
and verifies its enabled Agent User/Agent Identity belong to this blueprint through read-only Graph
beta queries. The administrator must be able to read these directory objects. A propagation delay
or denied read stops this check; it does not trigger new identity creation or role assignments.
It then guides owner-run Teams, OneDrive, email and routine tests. Individual successful operator
checks are checkpointed. `complete` now
requires that guided acceptance, not only Bot Based routing. Old routing-only checkpoints resume at
runtime activation; an already accepted deployment is checked read-only and never silently enabled
after a subsequent admin disable. No automatic workload creation or test email is sent by setup.
This expanded path is covered by mocked tests; a new-tenant deployment and delivery test remain
required to validate the complete customer journey. See
[Step 6 of the admin guide](TENANT_ADMIN_INSTALLATION.md#step-6-connect-the-foundry-hosted-agent-to-its-azure-bot)
for the manual fallback.

For a read-only prerequisite check before deployment, run:

```bash
./setup.sh --validate
```

This signs into the selected tenant but creates no Azure resources and assigns no roles. It checks
Azure/Foundry token acquisition, the active subscription, required resource-provider registration,
Bicep, `azd`, and `AZURE_LOCATION` when supplied. Microsoft 365 licensing and blueprint/workload
approval are separate control-plane checks covered by the tenant-admin guide.

It installs Azure CLI and Azure Developer CLI when missing, installs its small local scripting
dependencies, and then shows a numbered selector containing the Azure tenants and administrator
identities available on the workstation. **Login into other one** opens Microsoft sign-in and
refreshes the selector. After selection, the script verifies both CLI sessions against that tenant
and runs `azd provision`.

The packaged `azure.yaml` flow performs Steps 1 through 8 in this guide: it registers the required
resource providers, provisions a Foundry account/project, initial model deployment and private
container registry, builds the Hermes image in ACR, creates the hosted agent and its Foundry-managed
blueprint, declares the Graph permissions for later admin review, submits the tenant-scoped
publication request, and installs the constrained routine-role automation. The operator still
needs Azure Owner, or Contributor plus Role Based Access Control Administrator, because the
deployment creates narrowly scoped role assignments.

The template also assigns **Foundry User** to the selected deployment administrator at this new
project. Azure Owner/Contributor governs resource management but does not itself supply the Foundry
data action used to enumerate deployments or create hosted-agent versions. The post-provision hook
waits for this project-local role to propagate before calling the Foundry data plane.

The script does not accept Frontier terms, buy or assign licenses, grant Microsoft 365 admin
consent, approve or activate the request, choose a hiring scope, or create an agent instance. Those
steps remain in [TENANT_ADMIN_INSTALLATION.md](TENANT_ADMIN_INSTALLATION.md).

## What the installation creates

The installation has two independent halves:

1. **Azure and Microsoft Foundry runtime**
   - a Foundry account and project;
   - one or more model deployments;
   - an Azure Container Registry containing the Hermes image;
   - a Foundry hosted agent with Activity and Responses endpoints; and
   - an Entra Agent Identity Blueprint automatically created by Foundry.
2. **Microsoft 365 publication**
   - an Agent 365 package linked to the Foundry-created blueprint;
   - a tenant-approved hiring scope and policy template; and
   - one separately named Agent Identity and Agent User for every instance created from the
     blueprint.

The blueprint is the reusable software definition. An instance is one employee's separately
identifiable Hermes colleague. Each instance gets its own Microsoft 365 identity and, when licensed
and provisioned by Microsoft, its own mailbox, OneDrive, Teams presence, and directory profile.

## Before starting

### Licensing and preview enrollment

For the own-identity Autopilot path used here, Microsoft currently requires all of the following:

- Agent 365 Frontier enabled from
  [Agent 365 Overview](https://admin.cloud.microsoft/#/agents/overview) by selecting **Try now** and
  accepting the Agent 365 terms;
- at least one Microsoft 365 Copilot license or Microsoft Agent 365 license, including Microsoft
  365 E7; and
- an available Microsoft Agent 365 Frontier license.

Complete Steps 1 and 2 of
[TENANT_ADMIN_INSTALLATION.md](TENANT_ADMIN_INSTALLATION.md) for the direct purchase pages, license
assignment procedure, Frontier terms acceptance, and preview-seat verification. Complete Step 5 of
that guide for the exact Microsoft Graph consent procedure. Do not assign an ordinary human license
to the Agent User created by Microsoft's managed **Create instance** flow.

Eligible enrolled tenants currently receive a 25-seat preview subscription. Each Autopilot instance
consumes one of those seats because it creates an Agent User. This is a preview allocation, not a
documented production purchasing path for hundreds or thousands of instances.

The employee who creates or uses an instance also needs access to Microsoft Teams or Microsoft 365
Copilot. Exchange, SharePoint, OneDrive, and other workloads must be enabled in the tenant if Hermes
is expected to use them.

Microsoft reference: [Build your first Autopilot](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/agent-365).

### People and roles

Use separate administrators where the organization normally separates Azure and Microsoft 365
duties.

| Operation | Minimum documented role or access |
| --- | --- |
| Create Azure resources and role assignments | Azure `Owner`, or `Contributor` plus `Role Based Access Control Administrator`, at resource-group scope |
| Build the image | `AcrPush` or `Container Registry Repository Writer` on the registry |
| Create and publish the hosted-agent blueprint | `Foundry User` on the project |
| Configure owned blueprint permissions | `Agent ID Developer` when the operator owns the blueprint, or `Agent ID Administrator` |
| Upload the ZIP in Microsoft 365 admin center | `Global Administrator` according to the current Agent 365 manual-upload guide |
| Approve and activate the blueprint | `Global Administrator` or `AI Administrator` |
| Create an instance | Employee is included in the admin-selected hiring scope |

The Foundry project's managed identity also needs:

- `Container Registry Repository Reader` or `AcrPull` on the registry; and
- `Foundry User` on the Foundry account for model access.

Reference: [Hosted-agent permissions](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-permissions).

### Workstation tools

For the automated path, start with Bash and `curl`; `setup.sh` installs Azure CLI, Azure Developer
CLI, Python 3, and OpenSSL when missing. Docker and .NET are not required locally because Azure
Container Registry builds the complete image in the cloud. Git is needed only when cloning the
repository instead of using a prepared source archive.

Sign into the target tenant and subscription, then verify the selection before creating anything:

```bash
az login --tenant "<tenant-id>"
az account set --subscription "<subscription-id>"
az account show --query '{tenant:tenantId,subscription:id,name:name}' --output table
```

## Step 1: prepare the Azure resource group

1. Open [Azure portal](https://portal.azure.com/).
2. Select **Resource groups → Create**.
3. Select the subscription, enter a resource-group name, and choose a region that supports Foundry
   hosted agents and the required models.
4. Open the new resource group and select **Access control (IAM)**.
5. Give the deployment operator the temporary roles listed above. Remove excessive bootstrap roles
   after the installation is complete.
6. In **Subscriptions → Resource providers**, confirm that these providers are registered:
   `Microsoft.CognitiveServices`, `Microsoft.ContainerRegistry`, `Microsoft.Insights`, and
   `Microsoft.OperationalInsights`. Register `Microsoft.KeyVault` if routines will be enabled.

## Step 2: create the Foundry account and project

1. Open [Microsoft Foundry](https://ai.azure.com/) and switch to the target directory and
   subscription.
2. Select the project picker, then **Create new project**.
3. Create a new Foundry resource or select the resource created for this Hermes deployment.
4. Put the project in the resource group and region selected in step 1.
5. Open **Manage → Resource details** and record the Foundry account name, project name, and project
   endpoint in a private deployment worksheet.
6. In the Azure portal, open the Foundry account's **Access control (IAM)** and grant its project
   managed identity `Foundry User`.

Do not put subscription IDs, tenant IDs, account names, or project endpoints in Git.

## Step 3: deploy the models

1. In Foundry, select **Build → Models**, or the current equivalent **Models + endpoints** view.
2. Deploy at least one chat-capable model into this project.
3. Record the **deployment name**, not only the catalog model name. Hermes calls deployment names.
4. For a primary/fallback setup, create two deployments. For a minimal POC, the same compatible
   deployment may be used as both primary and fallback.
5. Open **Manage → Quota** and allocate sufficient TPM to each deployment. TPM is a rate limit, not
   a monthly budget or a prepaid token purchase.

Hermes performs multi-step turns and resends accumulated context after tool calls. A deployment
that is adequate for a small chat prompt can still rate-limit a realistic research-and-document
workflow. Start with a controlled pilot and adjust from measured usage.

Reference: [Manage model quota](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota).

## Step 4: create the container registry

1. In the Azure portal, select **Create a resource → Container Registry**.
2. Create a Basic registry in the Hermes resource group. Keep the admin user disabled.
3. Under the registry's **Access control (IAM)**:
   - give the deployment operator `AcrPush` or `Container Registry Repository Writer`;
   - give the Foundry project managed identity `AcrPull` or `Container Registry Repository Reader`.
4. Record the registry name without `.azurecr.io`.

## Step 5: build the Hermes image

```bash
git clone https://github.com/NousResearch/hermes-foundry.git
cd hermes-foundry

export REG="<registry-name>"
./build.sh v1
```

Azure builds the combined edge, gateway, Hermes runtime, and Microsoft 365 tool sidecar and stores
the image in the tenant's registry. The script does not route or modify any agent.

Verify the immutable image digest exists:

```bash
az acr repository show \
  --name "$REG" \
  --image "hermes-full-foundry:v1" \
  --query '{digest:digest,created:createdTime}' \
  --output table
```

## Step 6: create the new Foundry blueprint

Use `hermes-agent-on-foundry` as the Azure technical name. The employee-facing name is set later to
`Hermes Agent on Foundry`.

```bash
export ACCOUNT="<foundry-account-name>"
export PROJECT="<foundry-project-name>"
export AGENT="hermes-agent-on-foundry"
export REG="<registry-name>"
export TAG="v1"
export PRIMARY_MODEL="<primary-model-deployment-name>"
export FALLBACK_MODEL="<fallback-model-deployment-name>"

./scripts/create-hosted-blueprint.sh --dry-run
./scripts/create-hosted-blueprint.sh
```

The command fails rather than touching an agent with the same name. For the new agent only, it:

1. resolves the image tag to an immutable digest;
2. records the current compatible model inventory;
3. creates an initial hosted-agent version so Foundry creates the Entra blueprint;
4. creates a second version containing the new blueprint ID required by non-turn Agent OBO;
5. exposes both Activity and Responses protocols;
6. sets `BotServiceTenant` so tenant Microsoft 365 delivery can reach the endpoint; and
7. routes only the new agent to the second version.

It does **not** create an Agent User, grant admin consent, upload to Microsoft 365, or create Azure
role assignments. Its tenant-specific record is written with mode `600` to the ignored path:

```text
deployment-output/hermes-agent-on-foundry/blueprint.json
```

## Step 7: declare the Microsoft Graph access Hermes needs

The blueprint must contain both a reviewable required-resource declaration and an enumerated
inheritance list. Neither is consent by itself. The tenant administrator grants consent later.

Read the new blueprint ID from the local deployment record and run:

```bash
export BLUEPRINT_ID="$(python3 -c \
  'import json; print(json.load(open("deployment-output/hermes-agent-on-foundry/blueprint.json"))["blueprintId"])')"

./scripts/configure-blueprint-graph-permissions.sh --blueprint-id "$BLUEPRINT_ID"
```

The reconciliation is additive: it keeps any other valid inheritable scopes already configured by
the tenant administrator and adds the Hermes baseline below. It fails without patching inheritance
when the current Graph response is unreadable or malformed.

The declared baseline is:

- `Chat.ReadWrite`
- `Mail.ReadWrite`
- `Mail.Send`
- `Calendars.ReadWrite`
- `Files.ReadWrite.All`
- `Sites.ReadWrite.All`
- `User.Read.All`
- `User.ReadBasic.All`

These are delegated Agent User permissions. They allow an instance to work in its own mailbox and
OneDrive and in resources that colleagues explicitly share with it. They do not silently give the
Agent User every employee's files.

If the organization removes a scope, the corresponding Hermes capability must be considered
disabled and tested that way. For example, removing `Mail.Send` prevents outbound mail. Avoid
granting broad first-party MCP permissions unless the tenant explicitly enables the non-production
`ResponsesApi` diagnostic mode; normal Hermes uses the Graph-backed sidecar.

Reference: [Inheritable blueprint permissions](https://learn.microsoft.com/en-us/entra/agent-id/configure-inheritable-permissions-blueprints).

## Step 8: submit the blueprint for tenant-administrator review

Publishing the hosted agent is the final developer operation. It submits a pending Microsoft 365
request; it does not activate the blueprint or perform any tenant-administrator action.

```bash
export ACCOUNT="<foundry-account-name>"
export PROJECT="<foundry-project-name>"
export AGENT="hermes-agent-on-foundry"

./scripts/publish-autopilot-blueprint.sh --dry-run
./scripts/publish-autopilot-blueprint.sh
```

The script uses Foundry's native tenant-scoped Autopilot publication API. It verifies that the
hosted agent exposes the Activity protocol with `BotServiceTenant`, publishes the visible name
**Hermes Agent on Foundry**, links the Foundry-created Entra Agent Identity Blueprint, and leaves the
request pending for an administrator. It does not accept preview terms, grant Graph consent, select
a hiring scope, activate the blueprint, create an Agent User, consume an Autopilot seat, or create
Azure role assignments.

The publish call is non-idempotent when its response is lost. On a transport failure, the script
does not replay the request; it reports an uncertain outcome and tells the operator to inspect the
pending-agent requests before rerunning the same version. A structured `UserError` saying that the
version already exists is the only duplicate treated as an idempotent result. Other response bodies
are retained only in the temporary working directory and are not printed because they can contain
tenant and request identifiers.

Reference: [Foundry Autopilot publish API](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/agent-365#the-publish-api).

Stop here when separating developer preparation from tenant onboarding. The tenant administrator
continues with [TENANT_ADMIN_INSTALLATION.md](TENANT_ADMIN_INSTALLATION.md).

### Manual-package fallback

Use a ZIP only when the Foundry-native publication request cannot be used or the onboarding test is
specifically intended to exercise manual package upload.

Prepare a 192×192 color PNG and a 32×32 transparent white-outline PNG. Then run:

```bash
python3 scripts/package-agent365-blueprint.py \
  --blueprint-id "$BLUEPRINT_ID" \
  --display-name "Hermes Agent on Foundry" \
  --version 1.0.0 \
  --color-icon "/private/path/color.png" \
  --outline-icon "/private/path/outline.png"
```

The upload file is:

```text
deployment-output/hermes-agent-on-foundry/Hermes-Agent-on-Foundry.zip
```

The generator validates the blueprint ID, package version, 30-character Teams name limit, icon
dimensions, and ZIP contents. The package contains the blueprint ID because Microsoft 365 must know
which Foundry blueprint it represents. It contains no client secret, certificate, access token,
tenant ID, subscription ID, resource name, or user address.

Do not submit the native publication request and upload the ZIP for the same blueprint version.

References:

- [Agent 365 manual publishing](https://learn.microsoft.com/en-us/microsoft-agent-365/developer/publish-deploy-agent)
- [Foundry Autopilot publish API](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/agent-365#the-publish-api)

## Step 9: tenant administrator reviews and activates the blueprint

1. Open [Agents > All agents](https://admin.cloud.microsoft/#/agents/all).
2. Open the pending `Hermes Agent on Foundry` request. If the developer intentionally used the
   manual-package fallback, select **Upload custom agent** and upload the ZIP first.
3. Under **Publish to users**, select the host products and choose the hiring scope:
   - **None** keeps creation disabled;
   - **Specific users or groups** is the recommended pilot setting;
   - **All users** lets any tenant user create an instance.
4. Apply the organization's Agent 365 policy template.
5. Review every requested Graph permission and select **Grant admin consent** only if the list
   matches the approved Hermes capability set.
6. Review the audience, hiring scope, available Autopilot licenses, and policy template, then
   publish/activate.
7. Confirm the blueprint appears on the Registry tab with status **Available**.

Admin consent grants the approved scopes to the blueprint principal. Because step 7 configured
enumerated inheritance, present and future child Agent Identities can receive those same scopes in
their own Agent User tokens.

## Step 10: create the first instance

Use either of Microsoft's managed interfaces:

- **Microsoft Teams → Apps → Agents for your team → Hermes Agent on Foundry → Create instance**; or
- **Microsoft 365 Copilot → Agents → Agents for your team → Create instance**.

The employee selects an instance name, alias/domain, and manager. Microsoft currently allows names
up to 32 characters. Creation consumes one available Autopilot license and asynchronously creates
the Agent Identity, Agent User, Microsoft 365 workloads, Teams chat, and org-chart entry.

The current Microsoft experience does not expose a blueprint-specific naming regex or an external
approval callback in this package. Until Microsoft exposes the managed Create Instance pipeline to
tenant control planes, use a restricted hiring group or have administrators create instances in
the Microsoft 365 admin center when naming must be centrally controlled.

Allow several minutes for provisioning. A searchable identity does not prove that Exchange and
OneDrive provisioning has completed.

## Step 11: validate the installation

Before the user-facing checks, validate the deployed control-plane objects without changing them:

```bash
./scripts/validate-deployment.sh \
  --account "<foundry-account>" \
  --project "<foundry-project>" \
  --agent "hermes-agent-on-foundry" \
  --bot-service-id "<Azure Bot Service resource ID>"
```

The validator checks the 100% serving route, Activity and Responses protocols,
`BotServiceTenant`, immutable image digest, required runtime setting names, `HermesRouter` mode,
managed Agent Identity blueprint, Graph inheritable scopes, and optional Bot Service binding. It
does not print environment values and performs no writes.

For an image upgrade or rollback, create and inspect an unrouted version with `new-version.sh`, then
use the guarded routing command it prints. `scripts/route-agent-version.sh` requires both the target
version and the version expected to be serving. It refuses a stale expectation, validates the
target and Graph inheritance, changes the route, and reads it back. This keeps activation and
rollback explicit while preventing one release from silently overwriting another.

Start a new Teams chat with the new instance and test in this order:

1. Ask it to report its own Microsoft 365 identity.
2. Ask it to list up to three messages in its own mailbox.
3. Ask it to send a harmless message to its manager.
4. Ask it to create a uniquely named text file in its own OneDrive, read it back, and share it with
   its manager.
5. Upload a small DOCX in Teams and ask for a summary.
6. Ask it to list available models with `/model`.

Check the actual tool result and received artifact. A natural-language success statement is not
proof that Microsoft 365 completed an operation.

## Optional model controls

There are four different controls. They solve different problems.

### Restrict what models may be deployed

In [Azure portal](https://portal.azure.com/), open **Policy → Authoring → Definitions**, search for
**Foundry model deployments should only use approved models**, select **Assign**, and scope it to the
Hermes resource group, subscription, or management group. Set the effect to `Deny` and supply the
approved publishers and model asset IDs.

This generally available Azure Policy blocks new non-approved deployments. It does not hide catalog
cards and it is not the runtime model selector. Asset IDs are prefix-matched; use the exact IDs and
trailing slashes described in Microsoft's guide.

The separate **Foundry model deployments should meet eligibility requirements** policy can exclude
preview or non-Azure-direct models.

Reference: [Built-in Foundry model policies](https://learn.microsoft.com/en-us/azure/foundry/how-to/model-deployment-policy).

### Restrict what this Hermes blueprint exposes

Before creating the blueprint, set an explicit deployment-name allowlist:

```bash
export MODEL_ALLOWED_DEPLOYMENTS="approved-primary,approved-fast"
```

Hermes will expose only compatible project deployments in this list through `/model`. Azure RBAC,
Azure Policy, and the existence of the deployment remain the authoritative security boundary. The
Hermes allowlist is a blueprint runtime policy, not a substitute for Azure enforcement.

### Choose automatic priority

Set the order Hermes should try when the employee has not chosen a model:

```bash
export MODEL_PRIORITY="approved-primary,approved-fast"
```

At runtime:

- `/model` shows the permitted deployments;
- `/model <deployment-name>` selects one for that caller;
- `/model auto` returns to administrator priority; and
- `/model refresh` refreshes the current project inventory.

The employee cannot use `/model` to deploy a new model, expand the allowlist, or bypass Azure
policy.

### Limit throughput and monitor spend

- In Foundry, use **Manage → Quota** or edit the model deployment to set TPM/RPM capacity.
- In Foundry, use **Operate → Overview** for project estimates and each agent/model's **Monitor**
  tab for usage.
- In Azure portal, open the Hermes resource group or subscription and select **Cost Management →
  Cost analysis** for billed meters.
- Create a monthly budget under **Cost Management → Budgets** and configure actual and forecast
  alerts.

An Azure budget is an alert, not a hard spending cap; it does not stop the resources. Automated
shutdown or policy action after an alert requires separate tenant automation. TPM is a traffic
limit, not a monthly currency limit.

Reference: [Plan and manage Foundry costs](https://learn.microsoft.com/en-us/azure/foundry/concepts/manage-costs).

## Other optional tenant configuration

### Hiring and Microsoft 365 governance

- Limit hiring to a pilot security group in **Microsoft 365 admin center → Agents → All agents**.
- Create or select policy templates under **Agents → Settings → Templates**.
- Review the blueprint and linked Agent Identities in
  [Microsoft Entra admin center](https://entra.microsoft.com/) under **Entra ID → Agents**.
- Apply supported Conditional Access, Defender, Purview, SharePoint, retention, and DLP controls
  according to the organization's license and Agent 365 policy support.
- Give documents, sites, groups, and projects to an Agent User the same way access is granted to a
  human colleague. Graph consent allows an operation type; resource ACLs decide which resources the
  instance can actually reach.

### Hosted session cost and latency

`create-hosted-blueprint.sh` defaults to a five-minute idle timeout, the minimum currently
documented by Foundry. Set `IDLE_TIMEOUT_SECONDS` from 300 through 3,600 before creation. A shorter
timeout reduces idle compute but increases cold resumes. Foundry preserves the session filesystem
when it suspends the sandbox.

### Scheduled routines

Routines need additional non-turn identity configuration. Complete
[ROUTINE_RBAC_AUTOMATION.md](ROUTINE_RBAC_AUTOMATION.md) after the base agent works:

1. register an X.509 public certificate on this blueprint;
2. keep its private certificate in the tenant's Azure Key Vault;
3. allow the hosted runtime to read that certificate;
4. create a new agent version with `RoutineCredentialVaultUri` and
   `RoutineCredentialCertificateName`; and
5. install the included blueprint-scoped RBAC reconciler once so each new child Agent Identity gets
   only `Foundry User` on this project.

Do not grant per-instance Azure roles manually during testing. It hides a broken provisioning path
and produces instances whose behavior depends on undocumented operator actions.

### Observability

- Foundry: **Build → Agents → Hermes Agent on Foundry → Monitor**.
- Azure portal: the Foundry resource, Application Insights, and Log Analytics views created or
  connected for the project.
- Microsoft Entra: blueprint/Agent Identity sign-in and audit logs.
- Microsoft 365 admin center: Agent 365 inventory, policy, and lifecycle views.

Keep content logging and long retention disabled unless the organization's privacy policy explicitly
allows them. Do not put Graph tokens, attachment URLs, or document contents into custom telemetry.

## Updating the runtime later

Do not upload another Microsoft 365 package merely to change Hermes code. Build a new immutable
image and use `new-version.sh` to create a new hosted-agent version, validate it, then explicitly
route traffic. Repackage only when Microsoft 365-visible metadata or package version changes.

## Installation checklist

- [ ] Frontier enrollment, terms, and an available Autopilot license confirmed
- [ ] Foundry account/project and supported region selected
- [ ] Model deployments created and usable quota allocated
- [ ] Registry created; operator and project identity have only required roles
- [ ] Hermes image built and digest verified
- [ ] New technical agent `hermes-agent-on-foundry` created
- [ ] New blueprint ID recorded only under `deployment-output/`
- [ ] Graph declaration and enumerated inheritance verified
- [ ] Native Foundry publication request submitted with the visible name `Hermes Agent on Foundry`,
      or manual ZIP generated as an explicit fallback
- [ ] Package uploaded, permissions reviewed, and admin consent granted
- [ ] Hiring scope limited to the intended pilot group
- [ ] First instance and Microsoft 365 workloads finished provisioning
- [ ] Identity, mail, files, sharing, attachments, and model selection smoke-tested
- [ ] Optional routine automation configured and tested, if required
- [ ] Azure budget alerts and operational monitoring configured
