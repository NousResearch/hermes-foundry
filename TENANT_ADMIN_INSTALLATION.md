# Install Hermes Agent on Foundry

This is the primary installation guide for a Microsoft 365 tenant administrator. It assumes Nous
has already prepared and published the **Hermes Agent on Foundry** blueprint for the target tenant,
or has supplied a tenant-specific Agent 365 installation package. The administrator does not need
to clone this repository, build a container, create certificates, or run Azure CLI commands.

> [!IMPORTANT]
> A ZIP package is linked to an Entra Agent Identity Blueprint in a specific tenant. Do not upload a
> package exported from another tenant. If Hermes is not already visible in the Microsoft 365 admin
> center and Nous has not supplied a package prepared for this tenant, a deployment operator must
> first follow [ADVANCED_TENANT_DEPLOYMENT.md](ADVANCED_TENANT_DEPLOYMENT.md).

## What the administrator does

The tenant administrator:

1. verifies that Agent 365/Frontier is enabled and a license is available;
2. reviews and activates the Hermes blueprint;
3. chooses who is allowed to create Hermes instances;
4. reviews the requested Microsoft Graph permissions and grants tenant consent;
5. creates the first instance, or lets an allowed employee create it; and
6. verifies that the new Hermes colleague appears in Teams and Microsoft 365.

Microsoft performs the managed instance provisioning. Each instance receives a separate Agent
Identity and Agent User. When the required workload and license are available, Microsoft provisions
the instance's Teams presence, mailbox, calendar, OneDrive, and directory profile.

## Before starting

Have the following ready:

- a tenant administrator account with **Global Administrator** or **AI Administrator** access;
- Agent 365 Frontier enabled from
  [Agent 365 Overview](https://admin.cloud.microsoft/#/agents/overview): select **Try now** and
  accept the Agent 365 terms;
- an available Microsoft Agent 365 Frontier agent seat;
- either a pending **Hermes Agent on Foundry** request in the Microsoft 365 admin center or a
  tenant-specific ZIP supplied by Nous; and
- a user or security group for the initial pilot.

After accepting the terms, open
[Billing > Licenses](https://admin.cloud.microsoft/#/licenses) and confirm that the tenant has an
available **Microsoft 365 Frontier for AI Teammates** seat. The current Frontier allocation is
intended for evaluation; large production licensing and automated cross-tenant distribution remain
dependent on Microsoft's commercial and platform availability.

## Step 1: find the Hermes blueprint

1. Open [Agents > All agents > Requests](https://admin.cloud.microsoft/#/agents/all/requested).
2. Look for **Hermes Agent on Foundry** with the status **Pending activate**.

If it is present, select it and continue to Step 2.

If Nous supplied a tenant-specific package instead:

1. Open [Agents > All agents](https://admin.cloud.microsoft/#/agents/all) and select
   **Upload custom agent**.
2. Upload `Hermes-Agent-on-Foundry.zip`.
3. Wait for validation to finish and select the uploaded agent.

If neither option is available, stop here. The runtime and target-tenant blueprint have not yet been
prepared; use the advanced deployment guide rather than trying to create identity objects manually.

## Step 2: review and activate it

Use the Microsoft publication wizard for **Hermes Agent on Foundry**.

1. Confirm the agent name, publisher, description, icon, and declared capabilities.
2. Enable the Microsoft 365 host products required by the organization, including Teams.
3. Set **Hiring scope**:
   - choose **Specific users or groups** for a controlled pilot;
   - choose **All users** only when every employee should be allowed to create an instance; or
   - choose **None** when instances will be created only by administrators.
4. Apply the organization's Agent 365 policy template, if one is configured.
5. Review the requested Microsoft Graph permissions.
6. Grant tenant admin consent only for the approved permissions.
7. Select **Publish** or **Activate** and wait until the agent is shown as **Available**.

Hermes currently declares the permissions needed for its own mailbox, calendar, OneDrive, sharing,
Teams chat, and directory lookup. The exact consent screen is authoritative. If its permissions do
not match the approved deployment, do not activate it; return the package to the deployment operator.

The approval above is for the reusable blueprint. Employees included in the hiring scope can then
create instances without a separate per-instance approval. Restrict the hiring scope when the
organization needs centralized approval or naming control.

## Step 3: create the first Hermes instance

There are two supported UI paths.

### Administrator-created instance

Use this path when IT must control the name, alias, owner, or rollout order.

1. Open [Agents > All agents](https://admin.cloud.microsoft/#/agents/all) and select the available
   Hermes blueprint.
2. Select **Create instance**.
3. Choose the employee who will own or manage the instance.
4. Enter the approved display name, alias/domain, and manager fields offered by Microsoft.
5. Confirm the available Agent 365 license and create the instance.

### Employee-created instance

Use this path when employees in the hiring scope may provision their own Hermes colleague.

1. Open [Microsoft Teams](https://teams.microsoft.com/v2/) or
   [Microsoft 365 Copilot Chat](https://m365.cloud.microsoft/chat/).
2. Open **Agents for your team** and select **Hermes Agent on Foundry**.
3. Select **Create instance**.
4. Enter the instance name and the other fields offered by Microsoft, then confirm creation.

The current Microsoft employee flow allows the employee to choose the instance name. If strict
naming or a separate per-instance approval is required, use the administrator-created path and set
the hiring scope to **None** or a tightly controlled group. This repository does not replace
Microsoft's managed Create Instance pipeline.

## Step 4: wait for Microsoft provisioning

Instance creation is asynchronous. Wait until Microsoft reports the instance as ready. Do not test
mail or files while the identity or workloads are still provisioning.

After completion, confirm that:

- the instance has its own Agent Identity and Agent User;
- the instance is visible as a distinct colleague in Teams and the directory;
- the intended employee is its owner or manager;
- its Agent 365 license is assigned; and
- the user can open a chat with it.

Mailbox and OneDrive readiness can lag behind identity creation. If those tools initially report
that the resource cannot be found, wait for workload provisioning and retry before changing access.

## Step 5: perform a short acceptance test

From the owner's Teams chat, ask Hermes to perform read-only checks first:

1. identify its own Microsoft 365 account;
2. list up to three files from its own OneDrive; and
3. list recent messages from its own mailbox.

Then use a disposable test to verify approved write access:

1. create a small text file in its own OneDrive and read it back;
2. send a test email to the owner; and
3. create, list, and delete a test routine if the advanced deployment enabled routine automation.

The agent must report failures as failures. A successful chat response by itself does not prove that
the requested Microsoft 365 operation completed.

## Optional controls, without CLI

| Control | Where the administrator configures it |
| --- | --- |
| Who may create an instance | [Agents > All agents](https://admin.cloud.microsoft/#/agents/all) > Hermes > hiring scope |
| Agent policy and allowed capabilities | [Agents > All agents](https://admin.cloud.microsoft/#/agents/all) > Hermes > policy template |
| Blueprint activation, disable, or removal | [Agents > All agents](https://admin.cloud.microsoft/#/agents/all) |
| Agent identities | [Entra Agent identities](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/AllAgents.MenuView/~/allAgentIds) |
| Conditional Access policies | [Entra Conditional Access](https://entra.microsoft.com/#view/Microsoft_AAD_ConditionalAccess/ConditionalAccessBlade/~/Policies/menuId//fromNav/Identity) |
| Licenses and available seats | [Billing > Licenses](https://admin.cloud.microsoft/#/licenses) |
| Instance owner, manager, and lifecycle | [Agents > All agents](https://admin.cloud.microsoft/#/agents/all) and [Entra Agent identities](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/AllAgents.MenuView/~/allAgentIds) |
| Models deployed in a tenant-owned Foundry project | [Microsoft Foundry](https://ai.azure.com/) |
| Azure model policy | [Azure Policy](https://portal.azure.com/#view/Microsoft_Azure_Policy/PolicyMenuBlade/~/Overview) |
| Azure cost analysis and budget alerts | [Azure Cost Management](https://portal.azure.com/#view/Microsoft_Azure_CostManagement/Menu/~/overview) |
| Runtime logs and metrics | [Azure Monitor](https://portal.azure.com/#view/Microsoft_Azure_Monitoring/AzureMonitoringBrowseBlade/~/overview) |

Model, quota, budget, and runtime controls are available to the tenant administrator only when the
Hermes runtime is deployed in that tenant's Azure subscription. Azure budgets provide alerts and
automation hooks; they are not a universal hard per-agent spending cap. The current Hermes runtime
also exposes an approved model list and priority order, documented in the repository README.

Routine authorization requires one tenant-level automation deployment because every new Agent
Identity needs an Azure role on the Foundry project. It is intentionally kept out of this no-CLI
guide. The deployment operator installs it once by following the routine section in
[ADVANCED_TENANT_DEPLOYMENT.md](ADVANCED_TENANT_DEPLOYMENT.md); tenant admins should not assign
roles manually to individual agents.

## Responsibility boundary

| Party | Responsibility |
| --- | --- |
| Tenant administrator | Enables licensing, activates Hermes, grants consent, selects the hiring scope and policies, and manages instances |
| Microsoft Foundry and Agent 365 | Runs the hosted agent and performs the managed Agent Identity, Agent User, Teams, license, and Microsoft 365 workload provisioning |
| Hermes runtime | Provides the agent behavior, tools, model selection, memory, file handling, and routine logic within the permissions granted to that instance |
| Nous/deployment operator | Prepares the target-tenant runtime and blueprint, publishes the package, and installs advanced Azure-side automation and observability |

## Current packaging boundary

The intended enterprise experience is a Microsoft-distributed Hermes offer that deploys the runtime
and publishes a tenant-ready blueprint before the administrator reaches this guide. The present POC
does not yet provide one portable ZIP that creates the Azure runtime, Entra blueprint, Microsoft 365
publication, and instance workloads in any tenant. Today, Nous or the customer's deployment operator
must perform that one-time preparation in each target tenant. After it is prepared, the approval and
instance-provisioning steps above are portal-only and require no CLI work.

For that one-time technical preparation, use
[ADVANCED_TENANT_DEPLOYMENT.md](ADVANCED_TENANT_DEPLOYMENT.md). For updating an already deployed
runtime, use [DEPLOYMENT.md](DEPLOYMENT.md).
