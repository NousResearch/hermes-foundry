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

There are two very different deployment tasks:

1. **First installation into a Microsoft organization.** An administrator must create and connect
   Microsoft 365, Agent 365, Entra identity, Azure, Foundry, models, permissions, and an initial
   hosted-agent version. Start with
   [TENANT_ADMIN_INSTALLATION.md](TENANT_ADMIN_INSTALLATION.md). It includes the current manual
   portal flow, the safe new-blueprint command, the Agent 365 upload package, approval, model
   controls, and validation. [DEPLOYMENT.md](DEPLOYMENT.md) remains the lower-level operator and
   troubleshooting reference.
2. **Building or updating Hermes after that installation exists.** This repository can build the
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

The application build does not reach into another local repository. Azure resource creation is
currently a documented administrator-led portal procedure rather than one-click infrastructure as
code. Once the Foundry project, models, and registry exist, this repository can build the image,
create a new hosted agent and its Foundry-managed blueprint, and generate a tenant-specific ZIP for
manual Microsoft 365 admin-center upload.

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
scripts/        installation and removal commands for that automation
deployment/     tenant-neutral Microsoft 365 package templates
Dockerfile      combined hosted-agent image
start.sh        process startup and supervision
build.sh        local publish and Azure Container Registry build
new-version.sh  safe hosted-agent version creation and verification
```

Generated publish directories (`edge/`, `gateway/`, and `sidecar/`) are ignored and created by
`build.sh`. Tenant-specific blueprint records and upload ZIPs are written below the ignored
`deployment-output/` directory.

## Which instructions should I follow?

### I am deploying to a new organization

Follow [TENANT_ADMIN_INSTALLATION.md](TENANT_ADMIN_INSTALLATION.md) from the beginning. It covers:

- Microsoft 365 and Agent 365 licensing;
- the Azure subscription and administrator roles;
- Foundry, model, registry, bot, logging, and hosted-agent resources;
- the agent identity blueprint, Graph consent, and inheritable permissions;
- creating the Agent User and waiting for mailbox/OneDrive provisioning;
- building this image, creating a version, and testing mail and files; and
- optional model, cost, hiring, session, observability, and routine controls.

The current repository is not a one-command Azure infrastructure installer. However,
`scripts/create-hosted-blueprint.sh` creates a first hosted agent safely after the Foundry project,
models, registry, and image exist. `new-version.sh` remains the update command for an existing
serving agent.

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

The build publishes the edge, stages the gateway and sidecar, downloads the pinned Hermes base
image as part of the remote ACR build, and stores the combined image in the target registry. It does
not deploy or route the image yet.

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
| Agent 365/Entra bootstrap | `AgentBlueprintId`, `AgentTenantId` | Outputs produced when the blueprint is registered in the tenant. |
| Agent User creation | `AgentInstanceId`, `AgentUserObjectId`, `AgentUpn` | The ID, Entra object ID, and address of the Agent User created through Agent 365. See [deployment §12](DEPLOYMENT.md#12-approve-and-instantiate-the-agent-user). |
| Human owner/admin | `AgentOwnerUpn`, `AgentOwnerOid`, `AgentTenantDomain` | The owner's Entra address/object ID and the organization's tenant domain. The runtime can later resolve the configured owner through the Agent User's manager, but the initial values are still required. |
| Bootstrap-generated | `IdentitySignKey` | A random signing value generated and stored by the installer. Do not invent a memorable value or commit it. |
| Routine credential bootstrap | `RoutineCredentialVaultUri`, `RoutineCredentialCertificateName` | Optional Key Vault certificate used by the blueprint as its confidential-client credential for non-turn Agent OBO. The public key is registered on the blueprint; the private key stays in tenant-controlled Key Vault. |
| Fixed runtime choice | `AgentLogic=HermesRouter`, `McpDiscoverySource=Manifest` | These select the normal Hermes path and the committed Microsoft 365 tool manifest. They are not tenant-specific. |
| Optional diagnostics | `A365AgentTracingId` | A deployment-specific tracing value, only when Agent 365 tracing is configured. |

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
7. Prints an explicit routing command but does not execute it.

Review the comparison and test the new version before routing production traffic.

For first-installation details and the manual routing/rollback process, see
[DEPLOYMENT.md §11](DEPLOYMENT.md#11-create-a-hermes-hosted-agent-version).

## Persistence

Hermes state defaults to `/files/hermes`, within Foundry's persisted filesystem. `start.sh` writes
configuration to the locations expected by Hermes and starts the edge before slow model services so
the hosted session can satisfy its readiness window.

Owner-approved long-term memory is stored in the Agent User's own OneDrive through `remember` and
`recall`. Caller-scoped `/model` preferences are stored on the persisted Hermes state volume using a
one-way hash of the conversation key.

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
python3 -m unittest discover -s tests -p 'test_*.py'
```

Build-time guards also verify that the sidecar imports, its required MCP tools register, and the
.NET runtime is executable in the final image.

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
  capabilities. The current authority checks and short-lived-token handoff are POC controls, not a
  completed enterprise security posture. Formal threat modeling, approvals for high-impact actions,
  least-privilege review, DLP/retention integration, audit guarantees, and penetration testing remain
  work.
- **Reliability and operations:** coverage is currently a small unit-test suite plus build-time
  checks. There is no complete CI/release pipeline, tenant-isolated integration matrix, load/chaos
  testing, SLO, rollback automation, or mature metrics/alerting package.
- **Distribution and support:** upgrade compatibility, support policy, SBOM/signing/scanning,
  licensing, disaster recovery, and long-term state migration/backup procedures are not finalized.

The prioritized productionization work is tracked in [ROADMAP.md](ROADMAP.md).
