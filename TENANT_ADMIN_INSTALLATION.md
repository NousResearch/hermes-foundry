# Install Hermes Agent on Foundry in a Microsoft tenant

This runbook installs a new blueprint with the visible name **Hermes Agent on Foundry**. It is for
a tenant administrator and an Azure deployment operator working together. It does not modify an
existing Hermes agent.

The procedure was checked against Microsoft's current Foundry Autopilot and Agent 365 documentation
on September 4, 2026. Autopilots with their own Agent User are still a Frontier feature, so verify
the linked Microsoft pages before a production purchase or rollout.

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

- enrollment in the [Microsoft 365 Frontier program](https://learn.microsoft.com/en-us/microsoft-agent-365/frontier);
- acceptance of the Microsoft Agent 365 terms;
- at least one Microsoft 365 Copilot license or Microsoft Agent 365 license, including Microsoft
  365 E7; and
- an available Microsoft Agent 365 Frontier license.

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

Install Git, Azure CLI, .NET 9, Python 3, and OpenSSL. Docker is not required because `build.sh`
uses Azure Container Registry cloud builds.

```bash
az version
dotnet --version
python3 --version
openssl version
git --version
```

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

## Step 8: create the manual-upload package

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

This manual ZIP path is different from the Foundry-native publish API. The official Foundry sample
calls `POST <project-endpoint>/agents/<agent-name>/microsoft365/publish` and creates a pending admin
request automatically. The ZIP is used here because the installation video is intended to show the
administrator uploading the package manually. Do not do both for the same package version.

References:

- [Agent 365 manual publishing](https://learn.microsoft.com/en-us/microsoft-agent-365/developer/publish)
- [Foundry Autopilot publish API](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/agent-365#the-publish-api)

## Step 9: connect the blueprint to the Foundry Activity endpoint

Open this page while signed into the target tenant:

```text
https://dev.teams.microsoft.com/tools/agent-blueprint/<blueprint-id>/configuration
```

Set the agent type to **API Based** and set the notification URL to:

```text
https://<foundry-account>.services.ai.azure.com/api/projects/<project>/agents/hermes-agent-on-foundry/endpoint/protocols/activityProtocol?api-version=2025-05-15-preview
```

Save, reopen the page, and confirm the same value is still present. This is the route Teams and
Microsoft 365 use for interactive and agentic Activity delivery. It is not a model endpoint and it
must not point directly at the container's internal port.

## Step 10: upload, review, and activate the blueprint

1. Open [Microsoft 365 admin center](https://admin.cloud.microsoft/).
2. Select **Agents → All agents → Upload custom agent**.
3. Upload `Hermes-Agent-on-Foundry.zip`.
4. Open the new `Hermes Agent on Foundry` request.
5. Under **Publish to users**, select the host products and choose the hiring scope:
   - **None** keeps creation disabled;
   - **Specific users or groups** is the recommended pilot setting;
   - **All users** lets any tenant user create an instance.
6. Apply the organization's Agent 365 policy template.
7. Review every requested Graph permission and select **Grant admin consent** only if the list
   matches the approved Hermes capability set.
8. Review the audience, hiring scope, available Autopilot licenses, and policy template, then
   publish/activate.
9. Confirm the blueprint appears on the Registry tab with status **Available**.

Admin consent grants the approved scopes to the blueprint principal. Because step 7 configured
enumerated inheritance, present and future child Agent Identities can receive those same scopes in
their own Agent User tokens.

## Step 11: create the first instance

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

## Step 12: validate the installation

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
- [ ] ZIP generated with the visible name `Hermes Agent on Foundry`
- [ ] Teams Developer Portal notification URL verified
- [ ] Package uploaded, permissions reviewed, and admin consent granted
- [ ] Hiring scope limited to the intended pilot group
- [ ] First instance and Microsoft 365 workloads finished provisioning
- [ ] Identity, mail, files, sharing, attachments, and model selection smoke-tested
- [ ] Optional routine automation configured and tested, if required
- [ ] Azure budget alerts and operational monitoring configured
