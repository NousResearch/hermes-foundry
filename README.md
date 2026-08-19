# Hermes Full Foundry

Hermes Full Foundry packages a Microsoft 365 agent edge, a request gateway, Hermes, and an
agent-identity Microsoft 365 sidecar into one container hosted by Microsoft Foundry.

> [!WARNING]
> **This repository is a work in progress and proof of concept. It demonstrates the Full Foundry
> architecture and working core Microsoft 365 flows, but it is not currently a production-ready,
> supported enterprise distribution.** Review the limitations below before deploying it beyond an
> isolated development tenant.

The repository is self-contained. It does not depend on source files, scripts, or infrastructure
definitions from another repository.

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
                         Graph, native M365 MCP, web, document, and code tools
```

Only the edge port is exposed. All internal services communicate over loopback.

### Execution modes: do not confuse these names

- `HermesRouter` is the default, main Full Foundry runtime. Interactive Activity turns are forwarded
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
Dockerfile      full hosted-agent image
start.sh        process startup and supervision
build.sh        local publish and Azure Container Registry build
new-version.sh  safe hosted-agent version creation and verification
```

Generated publish directories (`edge/`, `gateway/`, and `sidecar/`) are ignored and created by
`build.sh`.

## Prerequisites

- Azure CLI authenticated to the target tenant and subscription
- Access to an Azure Container Registry
- A Microsoft Foundry project with a hosted-agent definition
- .NET SDK compatible with `src/edge/global.json`
- Python 3 for deployment-script validation
- Model deployments available to the Foundry project
- Agent identity, Microsoft 365 permissions, and platform resources appropriate to the enabled tools

## Build

Set the registry name without the `.azurecr.io` suffix and provide an image tag:

```bash
export REG="<registry-name>"
./build.sh v1
```

The build publishes the edge, stages the gateway and sidecar, and submits the complete image build
to Azure Container Registry. The Hermes base image is pinned by digest for reproducibility.

## Runtime configuration

The hosted-agent version must provide these settings. Values shown in angle brackets are examples,
not real deployment values.

| Variable | Purpose |
| --- | --- |
| `MODEL_DEPLOYMENT_NAME` | Primary Azure Foundry model deployment |
| `FALLBACK_MODEL_DEPLOYMENT_NAME` | Hermes fallback model deployment |
| `AZURE_FOUNDRY_BASE_URL` | Foundry inference base URL |
| `AZURE_AI_PROJECT_ENDPOINT` | Foundry project endpoint used by data-plane tools |
| `IdentitySignKey` | Signs edge-to-gateway identity assertions |
| `AgentLogic` | Use `HermesRouter` for the main runtime. `ResponsesApi` is explicit non-production compatibility mode only. |
| `AgentBlueprintId` | Agent blueprint application identifier |
| `AgentInstanceId` | Agent instance identifier |
| `AgentUpn` | Agent user principal name |
| `AgentTenantId` | Microsoft Entra tenant identifier |
| `AgentUserObjectId` | Agent user object identifier |
| `AgentOwnerUpn` | Owner user principal name used by the authority policy |
| `AgentOwnerOid` | Owner object identifier used by the authority policy |
| `AgentTenantDomain` | Tenant domain used by the authority policy |
| `McpDiscoverySource` | Use `Manifest` for the committed Microsoft 365 MCP manifest |
| `A365AgentTracingId` | Optional deployment-specific tracing identifier |

Optional routine credentials use `RoutineCredentialVaultUri` and
`RoutineCredentialCertificateName`. See [ROADMAP.md](ROADMAP.md) for the remaining automation work.

Loopback service keys are generated per container session when they are not supplied. They are
shared only among processes inside the container and are never printed.

## Create a hosted-agent version

`new-version.sh` requires explicit target coordinates so the repository contains no tenant-specific
defaults:

```bash
export ACCOUNT="<foundry-account>"
export PROJECT="<foundry-project>"
export AGENT="<hosted-agent-name>"
export REG="<registry-name>"

./new-version.sh v1
```

The script:

1. Resolves the image tag to an immutable registry digest.
2. Reads the currently serving hosted-agent version.
3. Preserves its environment, metadata, CPU, memory, and protocol settings.
4. Creates a new version with the selected image and models.
5. Compares the old and new definitions without printing environment values.
6. Prints an explicit routing command but does not execute it.

Review the comparison and test the new version before routing production traffic.

## Persistence

Hermes state defaults to `/files/hermes`, within Foundry's persisted filesystem. `start.sh` writes
configuration to the locations expected by Hermes and starts the edge before slow model services so
the hosted session can satisfy its readiness window.

## Microsoft 365 access

The sidecar exposes Microsoft 365 tools using the agent's own identity. Graph-backed tools cover
mail, calendar, OneDrive, sharing, and generic Graph requests. Native Microsoft 365 MCP servers are
also available through the edge manifest for supported workloads.

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
- **Microsoft 365 provisioning:** Graph and native MCP operations depend on the tenant having
  provisioned the Agent User's workloads and granted the necessary permissions. The repository does
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
