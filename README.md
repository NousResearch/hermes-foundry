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
   hosted-agent version. Start with [DEPLOYMENT.md](DEPLOYMENT.md). This remains
   maintainer-assisted because the sanitized zero-to-one infrastructure installer is not yet in
   this repository.
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

The application build does not reach into another local repository. The missing part is the
sanitized infrastructure-as-code package for creating the first tenant stack; its required behavior
is documented in [DEPLOYMENT.md §8](DEPLOYMENT.md#8-bootstrap-the-first-foundry-and-agent-365-stack).

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
tests/          sidecar unit tests
Dockerfile      combined hosted-agent image
start.sh        process startup and supervision
build.sh        local publish and Azure Container Registry build
new-version.sh  safe hosted-agent version creation and verification
```

Generated publish directories (`edge/`, `gateway/`, and `sidecar/`) are ignored and created by
`build.sh`.

## Which instructions should I follow?

### I am deploying to a new organization

Follow [DEPLOYMENT.md from the beginning](DEPLOYMENT.md). It covers:

- Microsoft 365 and Agent 365 licensing;
- the Azure subscription and administrator roles;
- Foundry, model, registry, bot, logging, and hosted-agent resources;
- the agent identity blueprint, Graph consent, and inheritable permissions;
- creating the Agent User and waiting for mailbox/OneDrive provisioning;
- building this image, creating a version, and testing mail and files; and
- the current installer limitations.

The current repository is not a one-command new-tenant installer. In particular,
`new-version.sh` cannot create the first hosted agent—it needs a valid serving version to clone.

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
| Foundry model deployment | `MODEL_DEPLOYMENT_NAME`, `FALLBACK_MODEL_DEPLOYMENT_NAME` | The **deployment names**, not model catalog names, shown under the Foundry project's model deployments. See [deployment §11](DEPLOYMENT.md#11-create-a-hermes-hosted-agent-version). |
| Agent 365/Entra bootstrap | `AgentBlueprintId`, `AgentTenantId` | Outputs produced when the blueprint is registered in the tenant. |
| Agent User creation | `AgentInstanceId`, `AgentUserObjectId`, `AgentUpn` | The ID, Entra object ID, and address of the Agent User created through Agent 365. See [deployment §12](DEPLOYMENT.md#12-approve-and-instantiate-the-agent-user). |
| Human owner/admin | `AgentOwnerUpn`, `AgentOwnerOid`, `AgentTenantDomain` | The owner's Entra address/object ID and the organization's tenant domain. The runtime can later resolve the configured owner through the Agent User's manager, but the initial values are still required. |
| Bootstrap-generated | `IdentitySignKey` | A random signing value generated and stored by the installer. Do not invent a memorable value or commit it. |
| Fixed runtime choice | `AgentLogic=HermesRouter`, `McpDiscoverySource=Manifest` | These select the normal Hermes path and the committed Microsoft 365 tool manifest. They are not tenant-specific. |
| Optional diagnostics | `A365AgentTracingId` | A deployment-specific tracing value, only when Agent 365 tracing is configured. |

Optional scheduled routines add `RoutineCredentialVaultUri` and
`RoutineCredentialCertificateName`. Beginners should leave routines disabled: the certificate flow
is still manually administered. See [deployment §14](DEPLOYMENT.md#14-routines-and-non-turn-identity)
and [ROADMAP.md](ROADMAP.md).

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
4. Creates a new version with the selected image and models.
5. Compares the old and new definitions without printing environment values.
6. Prints an explicit routing command but does not execute it.

Review the comparison and test the new version before routing production traffic.

For first-installation details and the manual routing/rollback process, see
[DEPLOYMENT.md §11](DEPLOYMENT.md#11-create-a-hermes-hosted-agent-version).

## Persistence

Hermes state defaults to `/files/hermes`, within Foundry's persisted filesystem. `start.sh` writes
configuration to the locations expected by Hermes and starts the edge before slow model services so
the hosted session can satisfy its readiness window.

## Microsoft 365 access

The sidecar exposes Microsoft 365 tools using the agent's own identity. Graph-backed tools cover
mail, calendar, OneDrive, sharing, document operations, and generic Graph requests. First-party
Microsoft 365 MCP discovery remains only in the explicit non-production `ResponsesApi` diagnostic
mode; it is not part of the normal `HermesRouter` production path.

Permissions and identity provisioning belong to the target tenant. This repository intentionally
contains no tenant IDs, user addresses, blueprint IDs, client secrets, certificates, or deployment
credentials.

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
- **Models and releases:** release defaults currently name `gpt-5.6-terra` and `gpt-5.6-luna`.
  They can be overridden, but automatic deployment discovery, compatibility validation, quota
  handling, and a tenant-aware model-routing policy are not implemented.
- **Routines/cron:** the Responses-protocol adapter can receive scheduled invocations, but non-turn
  agent identity currently requires manually configured blueprint credentials in Azure Key Vault.
  Certificate enrollment, rotation, expiry monitoring, revocation, and routine lifecycle management
  are not automated. Routine session isolation and end-to-end coverage are limited. We are waiting
  for Microsoft guidance on a simpler or alternative supported non-turn identity flow.
- **Microsoft 365 provisioning:** Graph operations depend on the tenant having provisioned the
  Agent User's workloads and granted the necessary permissions. The repository does
  not automate tenant-wide consent, mailbox/OneDrive readiness, or every service-specific policy.
- **Files:** the current OneDrive implementation uses simple upload requests. Resumable upload
  sessions, robust large-file transfer, retry/resume, and broad file-type testing are not complete.
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
