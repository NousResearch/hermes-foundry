# Hermes Foundry

Hermes Foundry runs Hermes Agent as a Microsoft Foundry hosted agent with its own Microsoft 365
identity. The result can chat in Microsoft 365 and, when the tenant grants the required access, use
its own mailbox, calendar, OneDrive, and other Microsoft 365 services.

> [!WARNING]
> **This repository is a work in progress and proof of concept. It demonstrates the Hermes Foundry
> architecture and working core Microsoft 365 flows, but it is not currently a production-ready,
> supported enterprise distribution.** Review the limitations below before deploying it beyond an
> isolated development tenant.

## Read this first

There are three different deployment tasks:

1. **Installing a prepared Hermes blueprint.** A Microsoft 365 tenant administrator uses the
   portal-only [TENANT_ADMIN_INSTALLATION.md](TENANT_ADMIN_INSTALLATION.md). It covers licensing,
   blueprint approval, consent, hiring scope, instance creation, and validation. It requires no CLI.
2. **Preparing Hermes in a new tenant.** An authorized tenant deployment operator runs
   `./deploy.sh` (an alias for `./setup.sh`). It installs missing Azure command-line tools, lets the
   operator choose an available tenant identity, guides prerequisite checks, and runs the packaged
   `azd provision` flow. It pauses for Microsoft 365 approval, configures Bot Based routing after
   admin sign-in, enables and verifies the hosted endpoint with admin confirmation, then guides
   instance creation and first-use tests. Resume with `./deploy.sh --continue`. See
   [ADVANCED_TENANT_DEPLOYMENT.md](ADVANCED_TENANT_DEPLOYMENT.md) for its exact operations and
   permission requirements.
3. **Building or updating Hermes after that installation exists.** This repository can build the
   complete container and safely create a new version of an existing hosted agent. That is the
   shorter workflow documented below.

Do not begin by copying the runtime settings table into a portal. Most values are outputs of the
first-installation process, not choices a user is expected to invent. The initial installer writes
them once; `new-version.sh` then copies them from the serving version during upgrades.

## What is and is not in this repository

This repository contains our Microsoft 365 edge, request gateway, M365/tool sidecar, startup logic,
and combined-container build. Hermes Agent itself is supplied as a pinned Nous Docker base image:

```dockerfile
ARG HERMES_IMAGE=docker.io/nousresearch/hermes-agent@sha256:<pinned-digest>
FROM ${HERMES_IMAGE} AS runtime
```

When `build.sh` submits the build to Azure Container Registry, Azure downloads that pinned image and
layers this repository's components on top. There is therefore no Hermes source directory or
separate Hermes download script here. See [Dockerfile](Dockerfile) for the exact pinned digest and
[DEPLOYMENT.md §10](DEPLOYMENT.md#10-build-and-publish-the-hermes-container) for the build flow.

The application build does not reach into another local repository. The guided installer uses the
packaged Bicep templates to create the Azure resources and builds the image in ACR. It publishes a
private tenant blueprint through Foundry; a cross-tenant ZIP is not used. Licensing/enrollment,
Microsoft 365 approval and managed instance creation remain explicit portal steps. Setup records
API readiness checks separately from operator-confirmed Teams/OneDrive/mail tests. A fresh-tenant
end-to-end acceptance run is still required; mocked installer tests are not evidence of that run.

## Architecture

```text
Microsoft 365 / Foundry
          |
          v
  edge :8088            Activity and Responses protocols
          |
          v
  gateway :8080         request translation and short-lived token handoff
          |
          +-----------> Hermes :8642
          |
          +-----------> M365 sidecar :8010
                         Graph, web, document, and remote code/browser tools
```

Only the edge port is exposed. All internal services communicate over loopback.

A normal message follows this path:

1. Foundry or Microsoft 365 delivers the message to the .NET edge on port 8088.
2. The edge adds the Agent User identity, caller authority, session information, and a short-lived
   token for the agent's own Microsoft 365 account.
3. The gateway forwards the prompt to Hermes on port 8642.
4. Hermes can call tools exposed by the local sidecar on port 8010.
5. The reply returns through the gateway and edge to the original Microsoft 365 channel.

### Runtime modes

- `HermesRouter` is the default, main Hermes runtime. Interactive Activity turns are forwarded
  to the in-container Hermes gateway, Hermes, and the Microsoft 365 sidecar.
- `AgentLogic=ResponsesApi` is a retained **non-production compatibility and diagnostic mode**. It
  calls the Foundry Responses API directly with discovered MCP servers and bypasses Hermes, its
  memory, sidecar tools, prompt/tool loop, and authority behavior. It is never selected implicitly
  and is not a production fallback.
- The **Responses protocol** endpoint is different from `AgentLogic=ResponsesApi`. It is a wire
  protocol required for Foundry routines and forwards scheduled invocations to the normal Hermes
  runtime.

## Repository layout

```text
src/edge/       .NET Foundry and Microsoft 365 agent edge
src/gateway/    edge-to-Hermes gateway
src/sidecar/    Microsoft 365 and utility MCP tools
src/hermes-patches/ build-time compatibility overlays for the pinned Hermes image
tests/          sidecar unit tests
infra/          tenant-admin Azure automation templates
scripts/        hosted-agent creation/publication and tenant-automation commands
deployment/     tenant-neutral Microsoft 365 package templates
Dockerfile      combined hosted-agent image
start.sh        process startup and supervision
build.sh        Azure Container Registry cloud image build
new-version.sh  safe hosted-agent version creation and verification
```

Tenant-specific blueprint records and upload ZIPs are written below the ignored
`deployment-output/` directory. Local `edge/`, `gateway/`, and `sidecar/` staging directories from
the older build path remain ignored; current ACR builds compile and copy directly from `src/`.

## Which instructions should I follow?

### I am deploying to a new organization

Hermes is installed as a private custom blueprint in the customer's tenant. This repository does
not publish Hermes to Azure Marketplace, AppSource, or the public Teams Store.

If Nous or a deployment operator has already published a tenant-ready **Hermes Agent on Foundry**
blueprint, follow the no-CLI [TENANT_ADMIN_INSTALLATION.md](TENANT_ADMIN_INSTALLATION.md). It covers:

- Microsoft 365 and Agent 365 licensing;
- blueprint review, consent, policies, and hiring scope;
- administrator-created and employee-created instances;
- waiting for Microsoft 365 workload provisioning; and
- testing Teams, mail, files, and routines.

If the runtime and blueprint do not yet exist in the target tenant, run:

```bash
./setup.sh
```

The script installs missing Azure CLI and Azure Developer CLI packages, shows the Azure tenants and
administrator identities already available on the machine, offers **Login into other one**, and
then runs `azd provision`. That command creates the tenant's Azure/Foundry resources, builds Hermes
in the tenant's registry, creates the Foundry-managed blueprint, declares the permissions that the
administrator will review, installs the scoped routine-role automation, and submits a private
tenant publication request. It does not approve the request, grant the blueprint's Microsoft 365
runtime consent, assign a license, create an Agent User, or publish to a public marketplace.

Setup then pauses at **Requests** and prints the exact admin-center URL. The request can take about
five minutes to appear, and in rare cases a few hours (observed delays, not a Microsoft SLA).
Approve/activate it, return to the terminal, and press **Enter**. Setup checks the approval status
and configures **Bot Based + Bot ID** through Microsoft Graph, preserving existing notification
settings and verifying the result with a separate read.

If interrupted or waiting for approval, resume in the same checkout:

```bash
./setup.sh --continue
```

Completed provisioning is not repeated. A failure *during* provisioning retries that phase using
the existing idempotent deployment hooks. Checkpoints live in Git-ignored
`deployment-output/setup-state.json` and contain IDs/status, not tokens. Keep that directory and
the `.azure` environment to resume; don't copy checkpoints between tenants. Only one setup process
can run in a checkout at a time. Use separate checkouts for separate installations.

The routing step explicitly asks permission to create/reuse a tenant-owned setup app with delegated
`AgentCommunicationConfiguration.ReadWrite`, consented for the administrator only. It then prints
a browser sign-in link: use the same administrator and complete MFA. It does not open your browser,
use device codes, save tokens, create client secrets, or assign roles to agent instances. The setup
app/consent remain in Entra for future setup; administrators can revoke/remove them after setup.
This Graph API is **beta**, and this step does not replace licensing, activation, or a real Teams
delivery test. See [the admin guide](TENANT_ADMIN_INSTALLATION.md#step-6-connect-the-foundry-hosted-agent-to-its-azure-bot)
for the manual fallback.

Before provisioning, the same operator can run `./setup.sh --validate`. It performs the normal
tenant/account sign-in and then checks the selected tenant, subscription, Azure and Foundry token
audiences, required resource-provider registrations, optional region, Bicep support, and `azd`
authentication without creating Azure resources or assigning roles. Microsoft 365 licensing,
blueprint approval, hiring scope, and workload readiness remain portal checks and are reported as
such rather than guessed by the script.

See [ADVANCED_TENANT_DEPLOYMENT.md](ADVANCED_TENANT_DEPLOYMENT.md) for the resources and required
administrator privileges. `new-version.sh` remains the update command for an existing serving
agent.

### I already have a provisioned Hermes Foundry stack

You need:

- Azure CLI signed into the correct tenant and subscription;
- permission to push/build in the target Azure Container Registry;
- `Foundry Project Manager` on the existing Foundry project;
- the .NET SDK version selected by `src/edge/global.json`;
- Python 3; and
- the existing Foundry account, project, hosted-agent, registry, and model deployment names.

Then use the build and update steps below.

## Build an image

Set `REG` to the existing Azure Container Registry name without `.azurecr.io`, choose a new image
tag, and run:

```bash
export REG="<registry-name>"
./build.sh v1
```

The ACR cloud build compiles the edge, copies the gateway and sidecar, downloads the pinned Hermes
base image, and stores the combined image in the target registry. It does not require local Docker
or .NET and does not deploy or route the image yet.

## Compatibility baseline

This is the reproducible build and API baseline for the current POC. A released container digest,
not a mutable image tag or whichever dependency is newest, is the deployable artifact.

| Surface | Current baseline |
| --- | --- |
| .NET edge | `net9.0`; SDK `9.0.305` selected by `src/edge/global.json`; SDK and ASP.NET container manifest lists pinned by digest |
| Agent SDK | Direct NuGet versions are exact, including `Microsoft.Agents.*` `1.6.109-beta`; wildcard prereleases are rejected by tests |
| Python | CI uses Python 3.12; the production sidecar uses a dedicated distro-Python venv with exact application-library versions in the Dockerfile |
| Azure CLI | 2.80 or later |
| Azure Developer CLI | 1.27.1 is the oldest deployment version exercised by this POC; `setup.sh` installs the current stable release when absent |
| Foundry project APIs | Hosted agents, versions, deployments, and routines use `api-version=v1`; routines also require `Routines=V1Preview` |
| Blueprint/publication APIs | Managed identity blueprint and private Microsoft 365 publication use `2025-11-15-preview` |
| Container/API protocols | Activity endpoint `2025-05-15-preview`; non-production Responses compatibility client `2025-03-01-preview` |
| Microsoft Graph | `v1.0` for production mail, calendar, directory, and OneDrive operations |

The preview APIs above are compatibility constraints, not promises of future support. Revalidate
them before each release and whenever Microsoft changes the hosted-agent or Agent 365 contracts.
The Python interpreter is installed from the base distribution's package repository during the
image build, so a source rebuild is not yet bit-for-bit hermetic. The final ACR image digest is the
authoritative record of the resulting runtime.

## Where the runtime settings come from

You normally do **not** set these one by one during an update. The first-installation/bootstrap step
creates an initial hosted-agent version with the complete environment. `new-version.sh` reads the
currently serving version, preserves its environment values without printing them, and changes only
the image and selected model names.

This table exists so operators can diagnose the initial installation and understand ownership of
each value:

| Created or chosen by | Settings | Where to obtain them |
| --- | --- | --- |
| Foundry bootstrap | `AZURE_FOUNDRY_BASE_URL`, `AZURE_AI_PROJECT_ENDPOINT` | Outputs of the Foundry account/project deployment. See [deployment §8](DEPLOYMENT.md#8-bootstrap-the-first-foundry-and-agent-365-stack). |
| Foundry model deployment | `MODEL_DEPLOYMENT_NAME`, `FALLBACK_MODEL_DEPLOYMENT_NAME`, `MODEL_PRIORITY` | **Deployment names**, not catalog model IDs. Automatic selection uses the first currently available name in `MODEL_PRIORITY`. See [Model discovery and Teams selection](#model-discovery-and-teams-selection). |
| Release script | `MODEL_DEPLOYMENTS_SNAPSHOT` | Compatible project deployments captured automatically by `new-version.sh`; used only when live enumeration is unavailable. |
| Tenant model policy | `MODEL_ALLOWED_DEPLOYMENTS` | Optional blueprint-version-wide subset of compatible project deployments. Leave unset to expose all compatible chat deployments. |
| Tenant action policy | `M365_WRITE_POLICY`, `M365_OUTBOUND_POLICY` | Optional hard boundaries: `internal` (default, preserves current behavior), `owner-only`, or `disabled`. The outbound policy covers email/replies, invitations and event notifications, sharing invitations, and generic Graph writes. Invalid values fail closed. |
| Agent 365/Entra bootstrap | `AgentBlueprintId`, `AgentTenantId` | Outputs produced when the blueprint is registered in the tenant. |
| Agent User creation | `AgentInstanceId`, `AgentUserObjectId`, `AgentUpn` | The ID, Entra object ID, and address of the Agent User created through Agent 365. See [deployment §12](DEPLOYMENT.md#12-approve-and-instantiate-the-agent-user). |
| Human owner/admin | `AgentOwnerUpn`, `AgentOwnerOid`, `AgentTenantDomain` | The owner's Entra address/object ID and the organization's tenant domain. The runtime can later resolve the configured owner through the Agent User's manager, but the initial values are still required. |
| Bootstrap-generated | `IdentitySignKey` | A random signing value generated and stored by the installer. Do not invent a memorable value or commit it. |
| Routine credential bootstrap | `RoutineCredentialVaultUri`, `RoutineCredentialCertificateName` | Optional Key Vault certificate used by the blueprint as its confidential-client credential for non-turn Agent OBO. The public key is registered on the blueprint; the private key stays in tenant-controlled Key Vault. |
| Fixed runtime choice | `AgentLogic=HermesRouter`, `McpDiscoverySource=Manifest` | These select the normal Hermes path and the committed Microsoft 365 tool manifest. They are not tenant-specific. |
| Optional diagnostics | `A365AgentTracingId` | A deployment-specific tracing value, only when Agent 365 tracing is configured. |

Setup keeps the default inference provider in the selectable-provider list. When Foundry is enabled, enter its model name (for example `openai/gpt-6.1-sol`); setup resolves the version automatically from the Azure catalog. `FOUNDRY_MODEL_VERSION` remains available as an advanced override.

Scheduled routines use the blueprint's Key Vault credential for Agent OBO and a stable
`RoutineBindingSignKey` stored through deployment automation. They also require each child Agent
Identity to have **Foundry User** at project scope so it can manage its own routines. Install the
tenant-admin reconciler once as described in
[ROUTINE_RBAC_AUTOMATION.md](ROUTINE_RBAC_AUTOMATION.md); do not grant roles to test identities by
hand.

You do not supply `RouterUrl`, internal ports, or internal service keys. The image fixes all service
addresses to loopback, and `start.sh` generates matching ephemeral keys for the edge, gateway,
sidecar, and Hermes each time a container session starts.

## Create an updated hosted-agent version

This is for an **existing deployment only**. Obtain the resource names from the original
deployment output or Azure/Foundry portal; they are resource names, not secrets:

| Shell variable | Meaning |
| --- | --- |
| `ACCOUNT` | Existing Foundry account name |
| `PROJECT` | Existing project inside that Foundry account |
| `AGENT` | Existing hosted-agent name with a serving version |
| `REG` | Existing Azure Container Registry name, without `.azurecr.io` |
| `PRIMARY_MODEL` | Existing primary model **deployment** name |
| `FALLBACK_MODEL` | Existing fallback model **deployment** name |
| `MODEL_PRIORITY` | Optional ordered comma-separated deployment names |
| `MODEL_ALLOWED_DEPLOYMENTS` | Optional approved subset of project chat deployments |

If the original deployment worksheet is unavailable:

- Open [Microsoft Foundry](https://ai.azure.com/), select the project, and copy the Foundry account,
  project, hosted-agent, and model **deployment** names from that project.
- Open [Azure portal](https://portal.azure.com/), select the same resource group, open its Container
  Registry resource, and copy the registry name shown on its Overview page.
- Confirm that Azure CLI is signed into that resource's tenant and subscription before running the
  script. Names from a different subscription can resolve to the wrong resource or simply 404.

```bash
export ACCOUNT="<foundry-account>"
export PROJECT="<foundry-project>"
export AGENT="<hosted-agent-name>"
export REG="<registry-name>"
export PRIMARY_MODEL="<primary-model-deployment-name>"
export FALLBACK_MODEL="<fallback-model-deployment-name>"

./new-version.sh v1
```

The argument `v1` must match the image tag previously passed to `build.sh`.

The script:

1. Resolves the image tag to an immutable registry digest.
2. Reads the currently serving hosted-agent version.
3. Preserves its environment, metadata, CPU, memory, and protocol settings.
4. Enumerates the project's chat-capable deployments and records a safe fallback snapshot.
5. Creates a new version with the selected image and model policy.
6. Compares the old and new definitions without printing environment values.
7. Writes a mode-0600, tenant-neutral release record below the ignored
   `deployment-output/release-records/` directory.
8. Prints an explicit routing command but does not execute it.

Review the comparison and test the new version before routing production traffic.

For first-installation details and the manual routing/rollback process, see
[DEPLOYMENT.md §11](DEPLOYMENT.md#11-create-a-hermes-hosted-agent-version).

### Hermes base compatibility and rollback

Changing `HERMES_IMAGE` is a runtime upgrade, even when none of this repository's source changes.
Use a new image tag and hosted-agent version for every base-image digest; never rebuild an existing
release tag and assume it is equivalent.

Before routing a version built from a new Hermes digest:

1. Run the Python, .NET, shell, and public-tree release checks from a clean checkout.
2. Complete the ACR build. Its Dockerfile guards must compile the compatibility overlays, register
   the required sidecar tools, and execute the copied .NET runtime.
3. Run `new-version.sh`. It resolves the registry tag to a digest, preserves the serving
   definition, and refuses configuration loss before any traffic changes.
4. Test the new version in an isolated session for chat, one Graph read, one harmless Graph write,
   file creation/readback, and a Routine. Do not infer success from the model's prose.
5. Route with `scripts/route-agent-version.sh`, supplying the version currently serving through
   `--expect-current`. The script validates the target and reads the route back after the PATCH.

Keep the previous hosted-agent version and its image digest until the validation window closes. To
roll back, run the same routing command with the previous version as `--target-version` and the bad
release as `--expect-current`; do not rebuild either image and do not blindly replay a failed PATCH.
Start a new Microsoft 365 session after routing because existing sessions can remain pinned to the
version on which they started.

## Persistence

Hermes state defaults to `/files/hermes`, within Foundry's persisted filesystem. `start.sh` writes
configuration to the locations expected by Hermes and starts the edge before slow model services so
the hosted session can satisfy its readiness window.

Foundry filesystem persistence is session-scoped, so the gateway additionally synchronizes a
deliberately small durable subset of `HERMES_HOME` to the Agent User's own OneDrive App Folder. Each
Agent User therefore has a separate archive without a tenant-wide bucket, storage credential, email
address, or platform user ID in the key. Only verified owner turns and cryptographically bound
owner-authorized Routines restore or update it. Colleague sessions remain isolated: they neither
receive the owner's native Hermes memory nor teach the owner's durable skill library. The separate
`recall` tool is owner-only. Colleague access is controlled by the structured permissions described
below; ordinary memory, including historical permission text, never grants access.

The image supplies defaults from `config/state-sync.defaults.json`; the Agent User's persisted state
stores only owner changes in `/files/hermes/state-sync-overrides.json`. A new image can therefore add
a safe default path without overwriting an owner's prior choices. During the release-80 compatibility
window, `/files/hermes/state-sync.json` is also materialized with the effective configuration because
release 80 does not understand sparse overrides. Its defaults preserve `memories/`, all of `skills/`
except curator backup copies, `SOUL.md`, skill bundles, and pending memory/skill approvals.
Administrators can edit the packaged defaults, and the verified owner can inspect or replace the
effective policy through the constrained `state_sync_config` tool. The tool requires complete lists
so an ambiguous partial request cannot silently discard an existing path. Its read action also lists
safe, unselected top-level paths, making newly introduced Hermes state visible for an explicit owner
decision. Turning synchronization off writes one final checkpoint containing that setting; later
turns continue checking the remote head but do not upload state until the owner enables it again.

The whitelist cannot override the gateway's hard safety exclusions. Credentials and tokens,
deployment-generated configuration, runtime code and dependencies, conversation/session databases,
logs, caches, checkpoints, temporary files, sockets, PID/lock files, and SQLite WAL/SHM files are
never archived. Symlinks are not followed. Restore validates every tar member and atomically overlays
regular selected files; it never extracts a path supplied outside `HERMES_HOME`.

### Model continuation compatibility

The Foundry image includes a narrow Responses-compatibility overlay: replayed assistant text keeps
its `annotations` array, and encrypted reasoning keeps its original `id`. Foundry rejects reasoning
replayed without that ID even with `store=false`; this can otherwise break model continuation after
tools across primary and fallback models. Missing-ID legacy reasoning records are omitted rather
than given fabricated IDs. Structured stream failures are surfaced as a stopped request, not a
successful empty response; earlier tool side effects must be checked before retrying.

### Approved access through Hermes

Colleagues need structured owner-approved grants to read or change the agent's Microsoft 365
resources. The default `internal` write policy still permits delegation within the tenant, but is
not sufficient authorization by itself. Administrator `owner-only` or `disabled` policies remain
upper bounds and cannot be overridden by a grant.

The owner can say, for example, "Let Alex read documents in the Invoices folder." Hermes resolves
the person and resource, then calls `propose_access_change`. A fixed confirmation shows the exact
person, scope, actions, allowed outbound recipients, and expiry. The owner replies exactly `Yes`
or `No` in the same conversation within 15 minutes. The verified inbound reply is handled before
calling Hermes. Other text cancels the pending proposal and returns to Hermes so it can prepare a
revision. Repeating `Yes` does not apply a proposal twice. `operation=replace` atomically replaces an
existing grant after confirmation, allowing a downgrade without leaving an older write grant active.
`list_access_permissions` shows the owner
all active grants and a colleague only their own; removing a grant uses the same confirmation flow.

For natural requests such as "Let Alex work on Acme," Hermes proposes the `collaboration` preset:
read, create and edit matching files, plus public web research. For a single existing file, it
proposes read/edit rather than creation elsewhere. The confirmation explains the current and
proposed access in ordinary language, without internal IDs or JSON. Nothing changes until approved.
Explicit restrictions use custom permissions. Ambiguous requests such as "full access plus email"
require clarification first; email, calendar, deletion and sharing are not included by default.
Multiple resource grants are confirmed separately. Filename rules match names, not document topics.

These permissions grant **access through the agent**, not native Microsoft access. A colleague
with `read` can ask for searches and summaries. They cannot edit, delete, send, or share unless
those actions are separately approved. A later `share_file` request requires an explicit `share`
grant for that file and destination. Delegated native sharing currently grants read access only.
Revoking a Hermes grant does not revoke an already created Microsoft sharing permission or recall
data already delivered to someone.

| Resource | Supported scopes | Actions |
| --- | --- | --- |
| Agent OneDrive | All eligible items, an item ID, a folder ID (optionally recursive), filename wildcard | Read/search, create files, edit, delete files, share with approved recipients |
| Agent mailbox | All messages, a message ID, a folder ID, subject wildcard; optional sender/date restrictions | Read/search, mark read/category/flag, delete, send, reply, forward to approved recipients |
| Agent calendar | All events, an event ID, a calendar ID, subject wildcard; optional date restrictions | Read/search, create, edit, delete, invite approved attendees |

Folder/all/pattern grants include future matching items. Patterns match filenames or subjects, not
arbitrary code or regular expressions. Native folder deletion/moves, upload sessions, outgoing mail
attachments, alternate drives, groups, Teams resources, skills/code execution, and Routine management
are not delegated by this feature. Unknown Graph routes/options fail closed. Explicitly approved
`public_research` permits public web search and page fetching. The confirmation discloses that queries
and URLs reach external services and research is not constrained by the filename scope. Instructions
prohibit sending private contents or secrets; this is not a content-level data-loss-prevention filter.
Browser automation and code execution remain owner-only. Routine execution remains tied to its signed owner
binding; routines cannot propose or confirm access changes.

The permission service verifies actual caller IDs and current resource metadata on each delegated
tool call. Search/list results are filtered before reaching Hermes, and preauthenticated download
links are removed. Lists examine at most 100 candidates per call and report when more may exist;
they do not expose raw continuation URLs. Delegated downloads are capped at 16 MB. Generic Graph
and binary document paths use the same broker. A change in a colleague's effective grants starts a
fresh Hermes conversation key so old model history is not reused under narrower permissions.

State is stored separately as `hermes-access-policy-v1.json` in the Agent User's OneDrive app folder,
outside `HERMES_HOME` and memory/skill snapshots. It contains grants, expiring proposals, and a bounded
100-entry approval audit. Records are signed using a domain-separated HMAC with `IdentitySignKey`,
bound to tenant and Agent User, and updated with ETag preconditions. Model-facing tools cannot sign,
approve, read, or overwrite the policy record; direct and item-ID paths are guarded. This is an
application boundary within our trusted runtime, not a separate Azure storage principal. A tenant
administrator with the underlying credentials can still modify storage; corrupted signatures fail
closed. Rotating `IdentitySignKey` requires a controlled policy migration/reapproval because existing
signatures no longer validate. Do not restore old policy files as part of ordinary memory rollback.

Old free-text permissions in `SHARED_MEMORY.md` are not migrated automatically: the owner must
confirm structured grants. `remember`/`recall` now retain only private owner preferences/context and
cannot authorize a colleague. All agent instructions use the same "Permissions for this
conversation" language and tell Hermes to perform approved actions without asking again.

Rollout must update all serving versions and retire sessions running the old memory-based rules.
Local tests cover approval interception, signatures, conflicts, scope checks, search filtering,
binary paths, calendar notifications and revocation. A live Teams/M365 acceptance run is still
required before treating this as a deployed tenant security boundary.

### Save and restore lifecycle

Before every verified-owner turn, the gateway compares the OneDrive head with the state last seen by
that container. It restores the full selected state on the first turn and merges a newer remote head
before later turns. Unchanged turns require only metadata checks; the archive is not downloaded
again. This matters when, for example, Teams and email have separate Foundry sessions for the same
Agent User. If remote validation or migration fails, chat remains available but state writes are
blocked for that turn so a stale container cannot replace a newer or incompatible archive.

After the response has been delivered, the gateway saves while that turn's short-lived Agent User
Graph token still exists. A second pass after the configured three-second settle period catches quick
background memory or skill creation. Longer post-response work is saved on the next verified-owner
turn in that same session. Non-owner conversations never restore or update this state. A process
failure after the answer but before the upload can therefore delay or, if the session disappears,
lose only that last unsaved change; the response path is intentionally not made dependent on
OneDrive availability.

The App Folder contains these state-control objects:

- `hermes-owner-state-v1.tar.gz` is the mutable release-80 compatibility head.
- `hermes-state-snapshot-<sha256>.tar.gz` is an immutable, content-addressed snapshot.
- `hermes-state-current-v2.json` is the small mutable pointer to the current snapshot. It records
  the state schema, individual component schemas, writer release/protocol, the matching legacy ETag,
  and up to five previous snapshot references.

A save first updates the compatibility head with an ETag precondition, creates the immutable
snapshot, and then advances the pointer with another ETag precondition. A pointer never references
bytes that have not already been written and hash-verified. Snapshot files outside the bounded
pointer history are currently retained; automatic garbage collection is deliberately deferred until
safe retention and recovery policy are defined.

### Concurrent sessions

Turns inside one container are serialized. Separate Foundry sessions are not, so simultaneous Teams,
email, web, or Routine turns can race. The first compatibility-head writer succeeds. A stale writer
receives an ETag conflict, reads the winner, performs a three-way file merge against the head it
originally loaded, and retries against the new ETag. Remote-only changes and deletions are applied;
local-only changes remain. If both sessions changed the same file, the active local version stays at
the expected path and the remote bytes are retained under
`pending/*/state-sync-conflicts/<hash>/...` for explicit reconciliation. A third writer winning the
bounded retry defers this session's save rather than silently overwriting it.

This provides conflict-safe eventual consistency, not a global distributed transaction. Two truly
simultaneous turns may answer from the same older snapshot before either has saved. Serializing every
channel through a distributed per-Agent-User lock would give stronger ordering but would also put a
remote lock and its failure mode on every interaction; it is not part of the current design.

### Updates, migrations, and rollback

The current writer protocol is `dual-v1-v2`. New sessions read and write the v2 snapshot/pointer and
also maintain the release-80 archive. If an older pinned session writes after a pointer update, its
new legacy ETag no longer matches the ETag recorded in the pointer. A new session detects that fact,
uses the legacy write as the newest head, merges it, and advances the pointer again. This bridge must
remain enabled while release-80 sessions may still exist.

Legacy complete whitelist configuration is migrated lazily, immediately before the first owner turn,
into packaged defaults plus sparse owner overrides without changing its effective behavior. The v2
pointer versions the overall state and `memory`, `skills`, `config_overrides`, and
`pending_approvals` independently. Unknown or newer schemas fail closed for restore and upload;
support for a breaking format must ship as an explicit, tested migration before that version is
routed. The safe release sequence is expand first (read old and new, dual-write), wait for pinned old
sessions to drain, verify the fleet, and only then contract in a separate release. The v2-only
contraction and snapshot garbage collection are not implemented yet.

Runtime rollback and state rollback are different operations. Routing back to a previous immutable
hosted-agent version is safe while that version still understands the active writer protocol. It does
not rewind user state. The pointer retains prior snapshot references for recovery, but automated
state rollback is intentionally not exposed yet because it must use compare-and-swap and preserve the
displaced head rather than hand-editing or deleting OneDrive files.

The default compressed-archive ceiling is 240 MB, below Graph's 250 MB direct-upload limit. It is
configurable downward but cannot be raised above 250 MB through the editable file. This mechanism is
for curated memory and procedural skills, not large work products or complete container backups.

Owner-approved long-term memory is stored in the Agent User's own OneDrive through `remember` and
`recall`. Caller-scoped `/model` preferences are stored on the persisted Hermes state volume using a
one-way hash of the conversation key.

Inbound email delivery claims are also persisted per Agent User, in that identity's OneDrive app
folder. Atomic marker creation prevents two Foundry sessions from answering the same notification;
the marker contains only a one-way hash of the message ID, never the sender, subject, body, or raw
message ID. Abandoned claims can be recovered after 30 minutes, and completed markers are retained
for eight days before bounded cleanup.

## Microsoft 365 access

The sidecar exposes Microsoft 365 tools using the agent's own identity. Graph-backed tools cover
mail, calendar, OneDrive, sharing, document operations, and generic Graph requests. First-party
Microsoft 365 MCP discovery remains only in the explicit non-production `ResponsesApi` diagnostic
mode; it is not part of the normal `HermesRouter` production path.

Permissions and identity provisioning belong to the target tenant. This repository intentionally
contains no tenant IDs, user addresses, blueprint IDs, client secrets, certificates, or deployment
credentials.

The current tool surface covers mail, calendar, OneDrive create/read/search/share, Word and Excel
creation, in-place Word editing, PDF generation, web search/fetch, durable owner memory, and Foundry
Routine creation/list/removal. Teams uploads are downloaded using the recipient Agent User's access,
and long tool runs update one progress activity before posting the final answer.

### Authoritative capability paths

There is no automatic fallback between Graph, first-party MCP, and Foundry tools. Each production
operation has one authoritative path so a failure cannot be hidden by silently changing identity or
permission semantics.

| Capability | Production path | Acting identity and boundary |
| --- | --- | --- |
| Mail read, search, send, and reply | Microsoft Graph through the sidecar | The instance's Agent User; Graph delegated scopes inherited and consented through the blueprint |
| Calendar read, create, update, invite, and attachments | Microsoft Graph through the sidecar | The instance's Agent User |
| Directory person resolution | Microsoft Graph tenant-directory search | The instance's Agent User; ambiguous matches are returned rather than guessed |
| OneDrive list, search, read, create, share, and folders | Microsoft Graph through the sidecar | The instance's Agent User and its own drive |
| Owner memory and email-delivery deduplication | Microsoft Graph OneDrive app-folder data | Durable per Agent User; no container-local authority or message content is stored in markers |
| Word, Excel, PowerPoint, and PDF artifacts | Local document libraries, then Microsoft Graph upload | Document bytes are produced in the isolated hosted session; the Agent User owns the uploaded item |
| Existing Word-document edits | Graph download, local OOXML edit, conditional Graph upload | The Agent User; ETag guards prevent overwriting a concurrently changed document |
| Teams file attachments | Microsoft Agents SDK downloader, then Agent User Graph fallback | Restricted to the current turn's temporary attachment directory |
| Static URL retrieval | Sidecar HTTPS fetcher | No Microsoft 365 token; public HTTPS targets only, with redirect and private-network checks |
| Live web search | Foundry Responses API `web_search` | Hosted Agent/Foundry project identity; search transactions are separately metered |
| Browser automation and screenshots | Foundry Browser Automation Toolbox plus remote Playwright/CDP | Foundry project/toolbox identity; Chromium runs outside the Hermes container |
| Python execution | Foundry Code Interpreter Toolbox | Foundry project/toolbox identity; the remote sandbox receives no Graph token or tenant network access |
| Recurring work | Foundry Routines API plus signed instance binding | Owner authorizes creation; execution obtains a fresh Agent User token through Entra Agent OBO |
| Model inference and model inventory | Foundry project model endpoints | Hosted Agent identity and project RBAC; the administrator's deployments remain authoritative |
| First-party Microsoft 365 MCP servers | Non-production `ResponsesApi` diagnostic mode only | Never an implicit fallback for the production Hermes path |

This separation also defines portability. The Graph and local-document rows can move with Hermes
after replacing the Agent User token bootstrap. Foundry model, search, browser, code, and Routine
rows require either continued remote access to those Azure services or explicit replacement
adapters on another host.

## Model discovery and Teams selection

The Azure administrator remains the source of truth for model access. Hermes calls the documented
project-scoped `GET {project-endpoint}/deployments?api-version=v1`, follows pagination, and retains
only chat/Responses-capable `ModelDeployment` records. It cannot deploy models or bypass project
RBAC, Azure Policy, deployment state, or quota.

Automatic mode follows `MODEL_PRIORITY`; absent deployments are skipped. The release script stores
the same inventory in `MODEL_DEPLOYMENTS_SNAPSHOT`, while the runtime refreshes live discovery every
five minutes and uses the snapshot only if enumeration is unavailable. A snapshot grants no model
access: the live Foundry inference endpoint remains authoritative.

Teams users can type:

- `/model` or `/model list` to see the current and available deployment names;
- `/model <deployment-name>` to choose an allowed model for that caller-scoped conversation;
- `/model auto` to return to the administrator's priority order; and
- `/model refresh` to refresh the project inventory.

`MODEL_ALLOWED_DEPLOYMENTS` is a deployment-wide allow-list, not a per-employee entitlement. For `/`
autocomplete, the separately published Teams manifest must declare `supportsTargetedMessages` and a
`commandLists` entry with the `slash` trigger. Typed commands work without that autocomplete entry.

After a release, verify `/model`, select one listed deployment, send an ordinary prompt, then run
`/model auto`. If `/model` is treated as ordinary chat, the conversation is still pinned to an older
hosted session; let that session idle or stop that specific session before testing again.

## Scheduled routines

Hermes/OS cron is disabled. Recurring requests use Foundry Routines and require an explicit IANA time
zone. If the owner has not supplied one, Hermes asks once and stores it through `remember` for later
requests by the same Agent User.

During a verified owner turn, the edge signs the exact task, schedule, owner, Agent Identity, and
Agent User binding before creating the Routine. A fired Routine is accepted only after that binding
is verified. The edge then uses Entra's agent-specific OBO flow to obtain a fresh short-lived Graph
token for the paired Agent User; no human token, Graph access token, or refresh token is stored in
the Routine.

Agent OBO still requires the blueprint to authenticate as a confidential client. The tested setup
registers one X.509 public certificate on the blueprint and keeps its private key in Azure Key Vault,
readable by the hosted runtime. An administrator configures it once per deployment and rotates it
according to tenant policy; it is not created or exposed for each Routine.

Routine management is a separate Azure authorization boundary. New Agent Identities do not inherit
project RBAC from the blueprint, so a tenant administrator installs the included Logic App once. It
discovers future child identities and grants only **Foundry User** at the selected project scope.
See [ROUTINE_RBAC_AUTOMATION.md](ROUTINE_RBAC_AUTOMATION.md) for installation, verification,
security boundaries, cost, and removal.

## Tests

Run the committed Python tests from the repository root:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py'
```

Release CI additionally rejects skipped tests after installing the production document libraries.
Build-time guards verify that the sidecar imports, its required MCP tools register, and the .NET
runtime is executable in the final image.

## Security notes

- Never commit hosted-agent environment values or generated deployment responses.
- Keep certificate private keys in a tenant-controlled secret store such as Azure Key Vault.
- Prefer managed identity and short-lived tokens over static credentials.
- Pin release images by digest when creating agent versions.
- Review Graph and Microsoft 365 permissions under least-privilege principles.
- Treat generic Graph write tools as privileged capabilities.

## Project status

The image and core Microsoft 365 file/mail paths are functional, but the project remains a WIP/POC.
Known non-production areas include:

- **Installation and portability:** there is no one-click tenant installer or complete
  infrastructure-as-code package. A tenant administrator must first create/configure the Foundry
  project, hosted agent, registry, agent identity, permissions, toolboxes, and model deployments.
  `new-version.sh` updates an existing hosted agent by cloning its serving definition; it does not
  bootstrap a fresh tenant.
- **Models and releases:** compatible project deployments are discovered and can be selected through
  Teams, with administrator priority and allow-list controls. The allow-list is currently
  blueprint-version-wide rather than per employee; model compatibility beyond the project OpenAI
  Responses surface, quota-aware routing, and a tenant policy UI remain incomplete.
- **Routines/cron:** routine CRUD, instance-bound signed actions, Entra Agent User OBO execution, and
  automatic project-role reconciliation are implemented. Foundry Routines remain a preview service;
  immediate binding revocation, execution deduplication, signing-key rotation, and mature
  business-completion telemetry remain incomplete.
- **Microsoft 365 provisioning:** Graph and native MCP operations depend on the tenant having
  provisioned the Agent User's workloads and granted the necessary permissions. The repository does
  not automate tenant-wide consent, mailbox/OneDrive readiness, or every service-specific policy.
- **Files:** OneDrive, Teams attachment download, sharing, document creation, and in-place Word
  editing work for tested formats. Resumable upload sessions, robust large-file transfer,
  retry/resume, and broad file-type testing are not complete.
- **Tool dependencies:** browser automation and code interpretation are custom wrappers around
  Foundry toolboxes rather than unrestricted processes inside the container. Missing toolbox
  resources, permissions, or regional availability reduce functionality. Local arbitrary command
  execution is intentionally not exposed to the agent.
- **Container/runtime:** edge, gateway, Hermes, and sidecar run as multiple processes in one hosted
  container with shell-based supervision. Only the edge is public; internal services are loopback
  only. Local scratch paths are ephemeral, browser execution is external, and behavior across
  scale-to-zero, restarts, concurrent sessions, resource pressure, and platform upgrades needs more
  validation.
- **Security:** generic Graph writes, outbound email, sharing, and calendar actions are powerful
  capabilities. Tenant-wide `internal`, `owner-only`, and `disabled` hard boundaries are available,
  but natural-language owner delegation remains a prompt/memory policy rather than a cryptographic
  approval. The current authority checks and short-lived-token handoff are POC controls, not a
  completed enterprise security posture. Formal threat modeling, granular approvals, least-privilege
  review, DLP/retention integration, audit guarantees, and penetration testing remain work.
- **Reliability and operations:** coverage is currently a small unit-test suite plus build-time
  checks. There is no complete CI/release pipeline, tenant-isolated integration matrix, load/chaos
  testing, SLO, rollback automation, or mature metrics/alerting package.
- **Distribution and support:** upgrade compatibility, support policy, SBOM/signing/scanning,
  licensing, disaster recovery, and long-term state migration/backup procedures are not finalized.

The prioritized productionization work is tracked in [ROADMAP.md](ROADMAP.md).

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

### Inference provider selection

Hermes can use Nous portal inference or Azure Foundry inference while remaining
hosted in Foundry. New installations default to Nous; existing deployments retain
Foundry unless explicitly reconfigured.

Run `./deploy.sh` (equivalent to `./setup.sh`). The interactive installer asks for
the default provider, providers users may select, Nous model IDs, and the Foundry
model to deploy, with automatic catalog version lookup. There is no shared portal credential to configure.
Each user sends `/portal login` in a one-to-one Teams chat, signs into their own
Nous account in the browser, then sends `/portal status`. `/portal logout`
disconnects that user's account. Before login, the gateway responds with procedural
sign-in instructions and offers `/provider azure-foundry` when allowed; no inference
is needed for onboarding. Credentials are encrypted and isolated by caller,
agent, and tenant; billing follows the signed-in user's portal account.

**Egress is configured automatically during deployment.** For Nous-enabled versions,
the scripts create the portal authorization and inference hostname rules, attach the resulting RAI
policy to the new agent version, and verify Azure retained it. On managed networks
that allow only approved outbound traffic, they also create the required FQDN
firewall rules. No manual policy creation or policy-ID copying is required.
`./new-version.sh <image-tag>` performs the same work for upgrades while preserving
existing policy controls; it leaves traffic switching to the operator. Existing
unrestricted outbound access stays unrestricted. Customer-managed VNet firewalls
are outside this automation. These controls use Microsoft's preview APIs.

In Teams, users can run `/provider nous`, `/provider azure-foundry`, and `/model`
to select from the administrator's allowlists. `/provider default` restores the
configured default. Choices apply to the caller's conversation; scheduled routines
use administrator defaults. Users cannot change credentials or network policies
through these commands.

See [inference configuration](DEPLOYMENT.md#configurable-nous-portal-and-foundry-inference)
and [automatic egress setup](DEPLOYMENT.md#portal-inference-egress) for environment
variables, billing identity, secret handling, and verification details.

### License

[MIT](LICENSE), matching NousResearch/hermes-agent.


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
