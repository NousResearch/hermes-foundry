# Roadmap

This roadmap covers work required to turn Hermes Foundry into a repeatable, tenant-neutral
distribution. It intentionally excludes tenant-specific deployment state and internal research.

## 1. Reproducible release baseline

- Verify a clean clone can build without files from another repository.
- Record supported .NET, Python, Azure CLI, and Foundry API versions.
- Add automated source, secret, and tenant-identifier scans.
- Produce immutable image digests and release notes for each version.
- Define compatibility and rollback policy for the pinned Hermes base image.

## 2. Tenant installer

- Provide an idempotent installer for the Foundry project, hosted agent, identity permissions,
  model deployments, toolboxes, storage, and optional Key Vault integration.
- Accept tenant and resource coordinates only as installer inputs; keep defaults tenant-neutral.
- Validate regional model quota and required Azure roles before provisioning.
- Print a deployment summary that redacts secrets and security-sensitive identifiers.
- Keep deployment validation and guarded routine-automation removal commands covered by tests; add
  a complete hosted-agent uninstall only after Microsoft 365 instance lifecycle can be verified.
- Research a supported tenant-admin API for setting an Agent Identity Blueprint to **Bot Based** and
  assigning its Bot ID. Until Microsoft documents one, keep the exact post-install Developer Portal
  action visible and blocking rather than calling its private portal endpoint from automation.

## 3. Routine authentication

- Keep the implemented signed instance binding and Entra Agent User OBO flow covered by integration
  tests for mail, calendar, and OneDrive.
- Productionize the included project-role reconciler and validate installation/removal in a second
  tenant.
- Add immediate binding revocation, execution deduplication, signing-key rotation, and downstream
  business-completion telemetry.
- Track the Foundry Routines preview contract and migrate when Microsoft publishes a stronger native
  Agent 365 instance-binding surface.

## 4. Model governance

- Keep project deployment discovery, automatic priority, and Teams `/model` selection covered by
  release tests.
- Add tenant policy integration for per-user or per-group model entitlements; the current allow-list
  applies to a complete blueprint version.
- Add compatibility probes and quota-aware failover beyond static administrator priority.
- Publish the Teams slash-command manifest entry so `/model` appears in autocomplete.

## 5. Microsoft 365 capability parity

- Maintain Graph-backed mail, calendar, OneDrive, sharing, and large-file paths.
- Evaluate first-party Microsoft 365 MCP tools separately where they provide platform-specific
  behavior; do not make the non-production `ResponsesApi` path an implicit production fallback.
- Add a capability matrix describing which tool path is authoritative for each operation.
- Test permission failures, unprovisioned workloads, throttling, retries, and token expiry.
- Add opt-in policies for high-impact outbound actions.

## 6. Reliability and observability

- Add deterministic startup health checks for edge, gateway, Hermes, and sidecar.
- Measure cold-start and warm-turn latency with the complete image.
- Add structured correlation across all four processes without logging tokens or message bodies.
- Verify persisted state across idle teardown, restart, image upgrade, and rollback.
- Add bounded retries and clear terminal errors for Foundry, Graph, and MCP failures.

## 7. Test and release gates

- Run unit tests and static checks on every change.
- Add container smoke tests for readiness and internal service connectivity.
- Add tenant-isolated integration tests for mail, calendar, OneDrive create/read/share, and routines.
- Require secret scanning and tenant-identifier scanning before release.
- Test new hosted-agent versions before traffic is routed and retain a verified rollback target.

## 8. Distribution documentation

- Keep the tenant-administrator [deployment runbook](DEPLOYMENT.md) aligned with the tested stack
  and current Microsoft licensing. Convert it from maintainer-assisted instructions to a runnable
  beginner installer when the bootstrap package in section 2 is complete.
- Document required Microsoft Entra, Microsoft 365, Foundry, registry, storage, and Key Vault roles.
- Document data boundaries, retention, outbound actions, and operational ownership.
- Add troubleshooting guidance based on stable error contracts rather than deployment-specific logs.
- Define licensing and support terms before public distribution.
