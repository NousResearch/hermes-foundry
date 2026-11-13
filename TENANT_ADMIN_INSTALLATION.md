# Install Hermes Agent on Foundry

This guide covers a **private custom-blueprint installation in one customer tenant**. Nothing in
this flow submits Hermes to Azure Marketplace, AppSource, the public Teams Store, or another public
catalog. In this guide, **publish** means making the custom blueprint available inside the
customer's own Microsoft 365 tenant after its administrator approves it.

This is the primary installation guide for a Microsoft 365 tenant administrator. There are two paths:

- **New tenant, nothing deployed:** obtain this repository/package from Nous, open a terminal in its
  root folder and run `./deploy.sh` (the same installer as `./setup.sh`). It installs its Azure tools,
  guides sign-in and tenant/subscription selection, builds the image in Azure, provisions the runtime,
  submits the private blueprint, and pauses for the portal actions below. No Nous-operated shell or
  copied tenant IDs from another deployment are required. Use macOS or Ubuntu/Debian (WSL on Windows).
- **Runtime and blueprint already prepared:** use the portal steps below with your deployment
  operator. A ZIP alone does not deploy the runtime. Only this prepared path is portal-only.

Resume an interrupted installation with `./deploy.sh --continue`. Do not delete its local checkpoint
or create another instance to retry a failed step. The installer prints portal URLs and asks for
confirmation at each manual boundary; it does not buy licenses or accept legal terms for you.

For an older deployment without a setup checkpoint, use `./deploy.sh --existing` and select its
existing azd environment. This reads the live agent and finds its matching Azure Bot, displays the
target for confirmation, and starts at approval/routing checks without provisioning infrastructure,
publishing a new blueprint or assigning roles. If old azd blueprint/bot outputs differ, the script
warns explicitly and preserves them; the confirmed live target is saved only in the private setup
checkpoint. Later runs use `--continue`. This is not a migration of the old Bicep environment.

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

### Choose inference during setup

For a new installation, `./deploy.sh` asks whether Hermes should use Nous portal
inference or Azure Foundry, and which providers employees may select. Nous is the
new-install default. Choose the Nous model; when Foundry is enabled, choose its
Azure model too; its catalog version is resolved automatically. No shared portal credential is requested.
Each employee sends `/portal login` in a one-to-one Teams chat, signs into their
own Nous account in the browser, then sends `/portal status`. They can disconnect
with `/portal logout`. The Agent User needs working OneDrive access to retain
these encrypted, isolated sessions across restarts.

**The deployment script creates and attaches the required portal egress policy.**
It also adds exact hostname rules to an existing Foundry managed-network firewall
when required, and verifies the saved configuration. You do not create a firewall
policy manually or copy a policy ID. This does not configure customer-managed VNet
firewalls. Existing deployments change through `new-version.sh`, not `--existing`.
See the [operator setup details](ADVANCED_TENANT_DEPLOYMENT.md#inference-and-outbound-access-during-setup).

Once the agent is running, employees can use:

- `/provider` to see their current provider and the enabled choices;
- `/provider nous` or `/provider azure-foundry` to switch within the admin allowlist;
- `/provider default` to restore the administrator's default;
- `/model` to list permitted models, then `/model <model-id>` to select one.

These commands change the caller's conversation, not fleet credentials, firewall
rules, or scheduled routines. Routines use the administrator's defaults. Portal
inference uses each signed-in user's own portal billing identity and does not
register each instance as a portal-managed Hermes Cloud machine.

### Guided installation sequence

| Stage | Script / administrator action |
| --- | --- |
| Prerequisites | Admin confirms Frontier enrollment, a free agent seat, Teams access, Azure deployment/role-assignment permissions and regional model quota before resource creation |
| Azure deployment | Script registers providers, deploys infrastructure/models, builds the image, creates and publishes the tenant blueprint, declares Graph scopes, and installs routine role automation by default |
| Microsoft 365 approval | Script pauses; admin opens Requests, reviews scopes, grants consent, selects hiring audience and activates the blueprint, then presses Enter |
| Bot configuration | Admin completes the script's separate Microsoft sign-in; script configures and verifies Bot Based routing |
| Runtime activation | Script verifies the serving version; if disabled, asks permission and calls the Foundry enable API, then reads back state |
| First instance | Admin uses Microsoft 365 Add/Create instance, or the allowed employee uses Teams Create instance; script verifies the supplied instance email belongs to this blueprint using read-only Graph queries |
| Acceptance | Owner tests Teams, OneDrive and mail, then a routine if installed; script records operator confirmations separately from API checks |

The installer code and mocked tests do not establish a completed clean-tenant acceptance run. If a
portal prerequisite or test fails, leave setup paused and resume after resolving it. A successful
deployment or approval alone is not proof of message delivery.

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

## Step 1: satisfy the tenant-level Frontier prerequisite

### 1.1 Check what the tenant already owns

1. Open [Billing > Your products](https://admin.cloud.microsoft/#/subscriptions).
2. Look specifically for **Microsoft 365 Copilot** with SKU part number
   `Microsoft_365_Copilot`.
3. Open [Billing > Licenses](https://admin.cloud.microsoft/#/licenses), select **Microsoft 365
   Copilot**, and confirm that at least one seat is active and assigned to the Global Administrator
   performing the Frontier enrollment.

This is a tenant-level prerequisite in the path tested by Nous. It is not a license for a Hermes
instance and it does not need to be assigned to every employee who receives a Hermes instance.
Microsoft's current documentation also names Microsoft Agent 365 and Microsoft 365 E7 as possible
qualifying products, but Nous has not validated those substitutions end to end for this specific
Frontier Autopilot flow.

### 1.2 Buy the tested prerequisite when it is absent

1. Open [Billing > Purchase services](https://admin.cloud.microsoft/#/catalog).
2. Search for **Microsoft 365 Copilot** and select **Details**.
3. Check that the product SKU is `Microsoft_365_Copilot`. Do not buy **Microsoft 365 Copilot
   Business** (`MICROSOFT_365_COPILOT_FOR_BUSINESS`) for this runbook. The Business SKU passed part
   of enrollment in our testing but did not expose the complete Frontier blueprint flow.
4. Review the prerequisite shown by Microsoft for the tenant's existing base subscription.
5. Select **Buy**, purchase one seat for the initial tenant activation, select the billing frequency,
   and complete checkout.
6. If **Buy** is unavailable because the tenant purchases through an Enterprise Agreement or Cloud
   Solution Provider, use the organization's Microsoft reseller or the **Contact Sales** action on
   the [Microsoft 365 Copilot enterprise pricing page](https://www.microsoft.com/microsoft-365/copilot/pricing/enterprise).

Standalone Agent 365 and Microsoft 365 E7 have their own per-human commercial licensing rules for
Agent 365 governance. Those rules are separate from the technical creation of the Hermes Agent User.
Neither a Copilot nor an Agent 365 human license is assigned to a Hermes instance in this Frontier
flow.

### 1.3 Assign the purchased license

1. Open [Billing > Licenses](https://admin.cloud.microsoft/#/licenses).
2. Select **Microsoft 365 Copilot**.
3. Select **Assign licenses**.
4. Add the Global Administrator who will enable Frontier and approve the first blueprint.
5. Save and wait for Microsoft 365 to show the assignment as active.

Do not assign this Copilot seat to each Hermes owner merely because they own an agent. An employee
needs a Copilot license only if the organization separately wants that employee to use licensed
Microsoft 365 Copilot features.

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
| Microsoft 365 Copilot (`Microsoft_365_Copilot`) | Global Administrator performing initial enrollment | One seat is the tenant-level prerequisite proven by Nous; it is not assigned per Hermes instance | [Purchase services](https://admin.cloud.microsoft/#/catalog) |
| Microsoft 365 Frontier for AI Teammates | One seat per Hermes instance | Microsoft adds 25 preview seats after Frontier enrollment; it is not bought separately | [Licenses](https://admin.cloud.microsoft/#/licenses) |
| Microsoft Teams | Human employees using the Teams channel | Existing Microsoft 365 plan or separate Teams purchase | [Purchase services](https://admin.cloud.microsoft/#/catalog) |
| Microsoft Agent 365 or Microsoft 365 E7 | Human users covered by the organization's GA governance licensing | Separate commercial/governance entitlement; not assigned to the Hermes Agent User and not relied on by this tested installation path | [Purchase services](https://admin.cloud.microsoft/#/catalog) |
| Azure subscription | Customer tenant, operated by the customer or an authorized deployment operator | Consumption-billed Azure subscription; this is not a Microsoft 365 license | [Azure subscriptions](https://portal.azure.com/#view/Microsoft_Azure_Billing/SubscriptionsBladeV2) |

## Step 2: enroll in the own-identity Frontier preview

**Microsoft Frontier** is the broader preview program. It is not an Azure service or a component of
Microsoft 365 Copilot. Microsoft currently exposes one of its user-audience controls under
**Copilot > Settings > Copilot Frontier**, while the separate Agent 365 enrollment and terms live
under **Agents > Overview**.

Accepting the Agent 365 Frontier terms enrolls the tenant in the preview and makes the preview agent
licenses available. The later managed **Create instance** operation is what creates an Agent
Identity, Agent User, and Microsoft 365 workloads.

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

To let a pilot employee discover and use newly published Frontier agents, open
[Copilot Frontier settings](https://admin.cloud.microsoft/#/copilot/settings/ViewAll/:/CopilotSettings/Frontier)
and select **Specific users**, then add the pilot group. This is a Frontier audience setting; it
does not create Agent Users or provision the 25 seats. It is also separate from the Hermes
blueprint's hiring scope, which controls who may create Hermes instances after activation.

Frontier is a preview with no specific SLA. Microsoft states that preview support can change or be
removed, so this installation remains a POC until the own-identity Autopilot path has a suitable GA
licensing and support model.

## Step 3: confirm the Azure subscription used by Hermes

This is not a license or an OAuth consent. Foundry hosting, models, registry, logging, and routines
are charged to an Azure subscription.

1. Open [Azure subscriptions](https://portal.azure.com/#view/Microsoft_Azure_Billing/SubscriptionsBladeV2).
2. Confirm that the subscription selected for Hermes is **Active** and belongs to the customer's
   Entra tenant.
3. If Nous operates the prepared runtime, Nous operates resources in that customer-authorized
   subscription; this guide does not assume that an unrelated Nous-tenant subscription can host the
   customer's tenant-local blueprint.
4. If no suitable customer subscription exists, select **Add** on that page or
   use the organization's Microsoft account team/CSP to create one.

For a new tenant, `./deploy.sh` performs that Azure preparation and publication. For a prepared
installation, continue with the tenant-ready blueprint already submitted by the deployment operator.

## Step 4: find the Hermes blueprint

1. Open [Agents > All agents > Requests](https://admin.cloud.microsoft/#/agents/all/requested).
2. Look for **Hermes Agent on Foundry** with the status **Pending activate**.

If it is present, select it and continue to Step 5.

After submission, the request can take about **five minutes** to appear; in rare cases it can take
**a few hours**. These are observed delays, not a Microsoft SLA. Refresh Requests and wait before
assuming publication failed or uploading another copy. If the operator is running `./setup.sh`,
it saves progress and pauses here. After approving/activating the request, the operator presses
**Enter**, or resumes later with `./setup.sh --continue`.

If Nous supplied a tenant-specific package instead:

1. Open [Agents > All agents](https://admin.cloud.microsoft/#/agents/all) and select
   **Upload custom agent**.
2. Upload `Hermes-Agent-on-Foundry.zip`.
3. Wait for validation to finish and select the uploaded agent.

If neither option is available, stop here. The runtime and target-tenant blueprint have not yet been
prepared; use the advanced deployment guide rather than trying to create identity objects manually.

## Step 5: review permissions, grant consent, and activate Hermes

Select **Hermes Agent on Foundry** in [Agents > Requests](https://admin.cloud.microsoft/#/agents/all/requested) and choose **Publish to Store** to open the Microsoft publication wizard.

1. Confirm the agent name, publisher, description, icon, and declared capabilities.
2. Enable the Microsoft 365 host products required by the organization, including Teams.
3. Under **Activate (optional)**, use **Select users who can create agent instances**:
   - **None**: no users are selected to create agent instances.
   - **All**: all users are selected to create agent instances.
   - **Specific**: select the users or groups who can create agent instances (use this for a controlled pilot).
   This is the instance-creation audience, also referred to as the hiring scope. Selecting an
   audience does not itself create an agent instance.
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

## Step 6: connect the Foundry-hosted agent to its Azure Bot

For this Hermes deployment, configure the blueprint's **Bot Based** binding before testing an
instance. A wrong or missing binding can prevent message delivery. This does not imply that every
Create/Add instance spinner is caused by the binding, or that API Based is unsupported for all
Microsoft agents.

**When the deployment operator uses `./setup.sh`:** after you complete **Publish to Store**, they
press **Enter** at its approval prompt, or run `./setup.sh --continue`. Setup verifies the approved
state and offers to configure the binding automatically. It creates/reuses a tenant-owned setup
application with one delegated Graph permission, `AgentCommunicationConfiguration.ReadWrite`,
consented for that administrator only. This is separate from the Hermes blueprint's runtime
permissions. The operator opens the printed browser sign-in link on the same computer and completes
MFA. No device code or manually copied Bot ID is needed. The script reads existing settings,
preserves notification preferences, updates the Bot Based binding, and reads it back to verify it.

**Runtime activation is a separate step.** After Bot Based routing is verified, setup checks that
the selected Hermes version is active and receives 100% of traffic. If the hosted endpoint is
disabled, it asks permission to enable it through Foundry and checks the result. This does not keep
the container running permanently: an enabled endpoint still scales compute to zero when idle.
Microsoft 365 blueprint approval does not substitute for this check.

Older setup checkpoints marked complete after routing alone are advanced to this activation and
first-instance check on `--continue`, without reprovisioning. Once the new acceptance flow has
completed, a later resume checks endpoint readiness but will not silently reverse an administrator's
subsequent disable action. See Microsoft's [hosted-agent lifecycle documentation](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-agent#disable-or-enable-an-agent).

The operator needs permission to register an application/service principal and grant delegated
consent (the setup calls require `Application.ReadWrite.All` and
`DelegatedPermissionGrant.ReadWrite.All` in its administrator Graph token). A tenant Global
Administrator can perform this consent step; Azure subscription ownership alone is not sufficient.
If those permissions are unavailable, ask the identity administrator to perform setup or use the
manual fallback below. Do not grant these permissions to agent instances.

The binding API is currently Microsoft Graph **beta**. The setup application has no secret,
certificate, or application permissions. It and its administrator-specific consent remain in Entra;
after setup, an administrator can revoke its consent and remove it if no further setup is needed.
Future resumes then require reviewing/recreating the setup client. Do not delete the Hermes
blueprint or its Azure Bot when retiring this separate setup application.

**Manual fallback (also usable for a no-CLI installation):**

1. Copy the **Blueprint ID** printed by `./setup.sh`.
2. Open
   `https://dev.teams.microsoft.com/tools/agent-blueprint/<BLUEPRINT_ID>/configuration`, replacing
   `<BLUEPRINT_ID>` with that printed value.
3. Change **Agent Type** from **Notification/API Based** to **Bot Based**.
4. In **Bot ID**, enter the same Blueprint ID.
5. Select **Save**.
6. Reload the page and confirm that it still shows **Bot Based** and the same Bot ID.

Verify a real Teams message after either method. Approval and a successful configuration write do
not themselves verify runtime enablement, welcome-message delivery, or Microsoft 365 provisioning.

API reference: [Update blueprint communication configuration](https://learn.microsoft.com/en-us/graph/api/agentidentityblueprint-update-communicationconfiguration?view=graph-rest-beta).

### Consent and approval checklist

| Action | Who performs it | Where | Required once or repeatedly? |
| --- | --- | --- | --- |
| Accept Agent 365 Frontier terms | Global Administrator | [Agent 365 Overview](https://admin.cloud.microsoft/#/agents/overview) > **Try now** | Once per tenant |
| Enable Frontier for pilot users, if required | Global Administrator | [Copilot Frontier settings](https://admin.cloud.microsoft/#/copilot/settings/ViewAll/:/CopilotSettings/Frontier) | Once per audience; change when the pilot group changes |
| Approve the Hermes blueprint and its Graph scopes | Global Administrator, or AI Administrator where tenant consent policy permits | [Agents > Requests](https://admin.cloud.microsoft/#/agents/all/requested) > Hermes > **Accept permissions** | Once for the tenant-specific blueprint permission set; repeat when a later version requests new scopes |
| Choose the Hermes policy template and hiring scope | Global Administrator or AI Administrator | The same Hermes publication wizard | Once at publication; administrators can change policy and scope later |
| Set Agent Type to Bot Based and set Bot ID | Deployment/identity administrator | `./setup.sh` after approval, or the Teams Developer Portal fallback | Once per blueprint; setup reads back the saved configuration |
| Give Hermes access to a document, site, mailbox, team, or other resource | Resource owner or Microsoft 365 administrator | The normal SharePoint, OneDrive, Exchange, or Teams sharing UI | Per resource, group, or policy |

Azure role assignments used by the hosted runtime and routines are RBAC, not Microsoft Graph
consent. They belong to the one-time advanced deployment. There is no employee OAuth-consent step
in this own-identity design.

## Step 7: create the first Hermes instance

There are two UI paths in the current preview. Microsoft's public Foundry quickstart documents the
employee path. The administrator path below exists in the Microsoft 365 admin center we tested, but
its exact fields and behavior are not yet documented as a stable public contract.

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

## Step 8: wait for Microsoft provisioning

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

## Step 9: perform a short acceptance test

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
| Agent policy template | [Agents > All agents](https://admin.cloud.microsoft/#/agents/all) > Hermes > policy template; custom templates and advanced controls depend on the tenant's Agent 365/E7 licensing |
| Blueprint activation, disable, or removal | [Agents > All agents](https://admin.cloud.microsoft/#/agents/all) |
| Agent identities | [Entra Agent identities](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/AllAgents.MenuView/~/allAgentIds) |
| Conditional Access policies | [Entra Conditional Access](https://entra.microsoft.com/#view/Microsoft_AAD_ConditionalAccess/ConditionalAccessBlade/~/Policies/menuId//fromNav/Identity); agent Conditional Access requires the applicable Entra ID and Agent 365 licensing |
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
Identity needs an Azure role on the Foundry project. `./deploy.sh` installs it by default. For a
prepared portal-only installation, the deployment operator installs it once using
[ADVANCED_TENANT_DEPLOYMENT.md](ADVANCED_TENANT_DEPLOYMENT.md); tenant admins should not assign
roles manually to individual agents.

## Responsibility boundary

| Party | Responsibility |
| --- | --- |
| Tenant administrator | Enables licensing, activates Hermes, grants consent, selects the hiring scope and policies, and manages instances |
| Microsoft Foundry | Runs the hosted Hermes container, model endpoints, agent endpoint, sessions, and scale-to-zero infrastructure |
| Microsoft Agent 365 and Microsoft 365 | Handles publication governance and the managed Agent Identity, Agent User, license, Teams, and Microsoft 365 workload provisioning |
| Hermes runtime | Provides the agent behavior, tools, model selection, memory, file handling, and routine logic within the permissions granted to that instance |
| Nous | Supplies the code/package, pinned base image and deployment instructions |
| Customer deployment operator | Runs the guided installer in the customer's tenant; reviews Azure-side automation and configuration |

## Current packaging boundary

The intended enterprise experience is a Microsoft-distributed Hermes offer that deploys the runtime
and publishes a tenant-ready blueprint before the administrator reaches this guide. The present POC
does not yet provide one portable ZIP that creates the Azure runtime, Entra blueprint, Microsoft 365
publication, and instance workloads in any tenant. Today, Nous or the customer's deployment operator
must perform that one-time preparation in each target tenant. `./deploy.sh` is the guided path for
that preparation; it includes the subsequent portal approval, activation and acceptance checkpoints.

For that one-time technical preparation, use
[ADVANCED_TENANT_DEPLOYMENT.md](ADVANCED_TENANT_DEPLOYMENT.md). For updating an already deployed
runtime, use [DEPLOYMENT.md](DEPLOYMENT.md).


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

On reprovisioning, the blueprint deployment step first reads the hosted agent.
If it exists, setup reuses its bound blueprint client ID for the bot, publication,
and role automation. It creates a blueprint only when the agent lookup returns
404; permission and service failures stop setup. An unrelated blueprint created
by an earlier attempt is not automatically deleted.


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
