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

## Administrator accounts required

Use a **Global Administrator** for the initial Frontier enrollment and first blueprint approval.
Microsoft documents **Global Administrator** or **AI Administrator** for approving a published
blueprint, but only a Global Administrator can accept the current Agent 365 Frontier terms. A
**Billing Administrator** can purchase licenses if the organization separates billing duties.

Have the following ready:

- the employee who will own or manage the first Hermes instance;
- a user or security group for the initial hiring scope;
- either a pending **Hermes Agent on Foundry** request or a tenant-specific ZIP supplied by Nous;
  and
- access to the tenant's Microsoft 365 and Azure billing accounts.

## Step 1: buy and assign the required licenses

### 1.1 Check what the tenant already owns

1. Open [Billing > Your products](https://admin.cloud.microsoft/#/subscriptions).
2. Check whether the tenant already has at least one of these qualifying products:
   - **Microsoft Agent 365**;
   - **Microsoft 365 Copilot**; or
   - **Microsoft 365 E7**, which includes Agent 365.
3. Open [Billing > Licenses](https://admin.cloud.microsoft/#/licenses), select the qualifying
   product, and confirm that it is assigned to the employee who will own or manage the first Hermes
   instance.

These are alternatives; do not buy all three. Do not purchase another product when the tenant
already has a suitable one. The current Foundry
Autopilot prerequisites require the tenant to have at least one qualifying Agent 365, Microsoft 365
Copilot, or Microsoft 365 E7 license before Frontier can provision agent seats.

### 1.2 Buy a qualifying product when none is present

1. Open [Billing > Purchase services](https://admin.cloud.microsoft/#/catalog).
2. Search for one of the following products and select **Details**:
   - **Microsoft Agent 365** when the tenant already has an eligible Microsoft 365 base subscription
     and needs Agent 365 separately;
   - **Microsoft 365 Copilot** when the selected employees also need Microsoft 365 Copilot; or
   - **Microsoft 365 E7** when the organization wants the full E7 package, including Agent 365.
3. Review the prerequisite shown by Microsoft for the tenant's existing base subscription.
4. Select **Buy**, choose the required number of human-user seats, select the billing frequency, and
   complete checkout.
5. If **Buy** is unavailable because the tenant purchases through an Enterprise Agreement or Cloud
   Solution Provider, use the organization's Microsoft reseller or the **Contact Sales** action on
   the [Agent 365 plans page](https://www.microsoft.com/en-us/microsoft-agent-365#plans-and-pricing).

Agent 365 is licensed per human user at general availability. Purchase and assign it to the people
who own, sponsor, manage, or use governed agents according to the organization's licensing plan. Do
not assign this human-user license to the Hermes Agent User.

### 1.3 Assign the purchased license

1. Open [Billing > Licenses](https://admin.cloud.microsoft/#/licenses).
2. Select the purchased product.
3. Select **Assign licenses**.
4. Add the employee who will own or manage the first Hermes instance. For the initial setup, also
   assign the qualifying license to the Global Administrator if the Agent 365 or Frontier controls
   are not visible to that administrator.
5. Save and wait for Microsoft 365 to show the assignment as active.

If Microsoft 365 Copilot was selected, its purchase screen might require an eligible Microsoft 365
base subscription. Purchase any missing prerequisite from the same
[Purchase services](https://admin.cloud.microsoft/#/catalog) page before assigning Copilot.

### 1.4 Confirm Teams access

Employees need Microsoft Teams access only when Teams will be used as a Hermes channel. Existing
Microsoft 365 enterprise subscriptions commonly provide it, but Teams is sold separately in some
regions and plans.

1. Open [Billing > Licenses](https://admin.cloud.microsoft/#/licenses) and check that the pilot
   employee has a product containing Microsoft Teams.
2. If not, open [Purchase services](https://admin.cloud.microsoft/#/catalog), search for
   **Microsoft Teams Enterprise** or the Teams product appropriate for the tenant, purchase it, and
   assign it to the employee.

The agent-specific Frontier seat is separate from the human employee's Teams entitlement.

### License checklist

| Item | Who receives it | How it is obtained | Direct page |
| --- | --- | --- | --- |
| Agent 365, Microsoft 365 Copilot, or Microsoft 365 E7 | Human owner/manager | Buy only if the tenant has no qualifying product | [Purchase services](https://admin.cloud.microsoft/#/catalog) |
| Microsoft 365 Frontier for AI Teammates | One seat per Hermes instance | Microsoft adds 25 preview seats after Frontier enrollment; it is not bought separately | [Licenses](https://admin.cloud.microsoft/#/licenses) |
| Microsoft Teams | Human employees using the Teams channel | Existing Microsoft 365 plan or separate Teams purchase | [Purchase services](https://admin.cloud.microsoft/#/catalog) |
| Azure subscription | Tenant or Nous deployment account, not a person | Consumption-billed Azure subscription; this is not a Microsoft 365 license | [Azure subscriptions](https://portal.azure.com/#view/Microsoft_Azure_Billing/SubscriptionsBladeV2) |

## Step 2: enable Agent 365 Frontier and accept its terms

This consent enables the tenant-level preview in which Autopilot instances receive their own Agent
Identity, Agent User, and Microsoft 365 workload provisioning.

1. Sign in as **Global Administrator**.
2. Open [Agent 365 Overview](https://admin.cloud.microsoft/#/agents/overview).
3. In the Agent 365 Frontier banner, select **Try now**.
4. Read the preview notice and select **I agree to the Terms of Service**.
5. Wait for the success confirmation.
6. Open [Billing > Licenses](https://admin.cloud.microsoft/#/licenses).
7. Confirm that **Microsoft 365 Frontier for AI Teammates** now appears with 25 active seats.

Do not buy those 25 agent seats and do not assign them to human users. Microsoft consumes one seat
automatically each time the managed **Create instance** flow creates a Hermes Agent User. If the
product does not appear, confirm that Step 1 is complete, sign out and back in, and then return to
[Agent 365 Overview](https://admin.cloud.microsoft/#/agents/overview).

If pilot users need access to other Frontier features, open
[Copilot Frontier settings](https://admin.cloud.microsoft/#/copilot/settings/ViewAll/:/CopilotSettings/Frontier)
and select **Specific users**, then add the pilot group. This audience setting is separate from the
Hermes blueprint's hiring scope.

## Step 3: confirm the Azure subscription used by Hermes

This is not a license or an OAuth consent. Foundry hosting, models, registry, logging, and routines
are charged to an Azure subscription.

1. Open [Azure subscriptions](https://portal.azure.com/#view/Microsoft_Azure_Billing/SubscriptionsBladeV2).
2. Confirm that the subscription selected for Hermes is **Active** and belongs to the same Entra
   tenant.
3. If Nous operates the prepared runtime, record the Nous-provided subscription arrangement and do
   not create a second subscription.
4. If the customer operates the runtime and no subscription exists, select **Add** on that page or
   use the organization's Microsoft account team/CSP to create one.

The no-CLI installation can continue only after Nous or the deployment operator has published a
tenant-ready Hermes blueprint using that runtime.

## Step 4: find the Hermes blueprint

1. Open [Agents > All agents > Requests](https://admin.cloud.microsoft/#/agents/all/requested).
2. Look for **Hermes Agent on Foundry** with the status **Pending activate**.

If it is present, select it and continue to Step 5.

If Nous supplied a tenant-specific package instead:

1. Open [Agents > All agents](https://admin.cloud.microsoft/#/agents/all) and select
   **Upload custom agent**.
2. Upload `Hermes-Agent-on-Foundry.zip`.
3. Wait for validation to finish and select the uploaded agent.

If neither option is available, stop here. The runtime and target-tenant blueprint have not yet been
prepared; use the advanced deployment guide rather than trying to create identity objects manually.

## Step 5: review permissions, grant consent, and activate Hermes

Use the Microsoft publication wizard for **Hermes Agent on Foundry**.

1. Confirm the agent name, publisher, description, icon, and declared capabilities.
2. Enable the Microsoft 365 host products required by the organization, including Teams.
3. Set **Hiring scope**:
   - choose **Specific users or groups** for a controlled pilot;
   - choose **All users** only when every employee should be allowed to create an instance; or
   - choose **None** when instances will be created only by administrators.
4. Apply the organization's Agent 365 policy template, if one is configured.
5. On **Accept permissions**, review the requested Microsoft Graph permissions listed below.
6. Select **Grant admin consent**. Sign in again as the Global Administrator if Microsoft prompts.
7. Select **Publish** or **Activate** and wait until the agent is shown as **Available**.

The current Hermes blueprint requests one tenant admin consent containing these delegated,
inheritable Microsoft Graph permissions:

| Permission | What Hermes uses it for |
| --- | --- |
| `Chat.ReadWrite` | Read and respond in its permitted Microsoft Teams chats |
| `Mail.ReadWrite` | Read, organize, create, and update messages in its own mailbox |
| `Mail.Send` | Send mail from its own mailbox |
| `Calendars.ReadWrite` | Read and manage events in its own calendar |
| `Files.ReadWrite.All` | Read, create, edit, and share files available to its Agent User |
| `Sites.ReadWrite.All` | Work with permitted SharePoint sites and libraries |
| `User.Read.All` | Resolve people and directory identities needed for collaboration |
| `User.ReadBasic.All` | Read basic profiles used for people selection and display |

These scopes define what an Agent Identity may request; they do not automatically share every
employee's files, mail, or sites with Hermes. Resource access still follows Microsoft 365 sharing,
membership, and policy. The exact permission screen shown by Microsoft is authoritative. If it
differs from this table, do not grant consent or activate the blueprint; return it to the deployment
operator.

Do not repeat these permissions as eight separate consent operations. The **Grant admin consent**
button in the Hermes publication wizard grants the approved set to the tenant-specific blueprint in
one operation. Employees do not sign into Hermes or grant their personal OAuth access because Hermes
acts as its own Agent User.

To verify or later revoke the consent, open
[Entra Enterprise applications](https://entra.microsoft.com/#view/Microsoft_AAD_IAM/StartboardApplicationsMenuBlade/~/AppAppsPreview),
search for the Hermes blueprint's enterprise application, and open **Security > Permissions**. Do
not modify permissions there during normal installation; the Microsoft 365 publication wizard is the
authoritative installation flow.

The approval above is for the reusable blueprint. Employees included in the hiring scope can then
create instances without a separate per-instance approval. Restrict the hiring scope when the
organization needs centralized approval or naming control.

### Consent and approval checklist

| Action | Who performs it | Where | Required once or repeatedly? |
| --- | --- | --- | --- |
| Accept Agent 365 Frontier terms | Global Administrator | [Agent 365 Overview](https://admin.cloud.microsoft/#/agents/overview) > **Try now** | Once per tenant |
| Enable Frontier for pilot users, if required | Global Administrator | [Copilot Frontier settings](https://admin.cloud.microsoft/#/copilot/settings/ViewAll/:/CopilotSettings/Frontier) | Once per audience; change when the pilot group changes |
| Approve the Hermes blueprint and its Graph scopes | Global Administrator, or AI Administrator where tenant consent policy permits | [Agents > Requests](https://admin.cloud.microsoft/#/agents/all/requested) > Hermes > **Accept permissions** | Once for the tenant-specific blueprint permission set; repeat when a later version requests new scopes |
| Choose the Hermes policy template and hiring scope | Global Administrator or AI Administrator | The same Hermes publication wizard | Once at publication; administrators can change policy and scope later |
| Give Hermes access to a document, site, mailbox, team, or other resource | Resource owner or Microsoft 365 administrator | The normal SharePoint, OneDrive, Exchange, or Teams sharing UI | Per resource, group, or policy |

Azure role assignments used by the hosted runtime and routines are RBAC, not Microsoft Graph
consent. They belong to the one-time advanced deployment. There is no employee OAuth-consent step
in this own-identity design.

## Step 6: create the first Hermes instance

There are two supported UI paths.

### Administrator-created instance

Use this path when IT must control the name, alias, owner, or rollout order.

1. Open [Agents > All agents](https://admin.cloud.microsoft/#/agents/all) and select the available
   Hermes blueprint.
2. Select **Create instance**.
3. Choose the employee who will own or manage the instance.
4. Enter the approved display name, alias/domain, and manager fields offered by Microsoft.
5. Confirm that a **Microsoft 365 Frontier for AI Teammates** seat is available and create the
   instance.

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

## Step 7: wait for Microsoft provisioning

Instance creation is asynchronous. Wait until Microsoft reports the instance as ready. Do not test
mail or files while the identity or workloads are still provisioning.

After completion, confirm that:

- the instance has its own Agent Identity and Agent User;
- the instance is visible as a distinct colleague in Teams and the directory;
- the intended employee is its owner or manager;
- one Frontier for AI Teammates seat was consumed for the instance; and
- the user can open a chat with it.

Mailbox and OneDrive readiness can lag behind identity creation. If those tools initially report
that the resource cannot be found, wait for workload provisioning and retry before changing access.

## Step 8: perform a short acceptance test

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
