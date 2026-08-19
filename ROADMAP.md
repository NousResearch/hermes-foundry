# Roadmap

This roadmap covers work required to turn Hermes Full Foundry into a repeatable, tenant-neutral
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
- Provide matching validation and uninstall commands.

## 3. Routine authentication

- Finalize the documented credential flow for routines that run without an interactive turn.
- Add an administrator bootstrap command that creates or imports the routine certificate, stores the
  private key in Azure Key Vault, and grants only the hosted-agent identity permission to use it.
- Confirm whether certificate rotation can be automatic; document the required administrative
  action when platform policy requires it.
- Add expiry monitoring, rotation tests, and fail-closed behavior when credentials are unavailable.
- Verify scheduled routines can use the agent identity for email, calendar, and OneDrive without
  placing reusable credentials in the image or hosted-agent environment.

## 4. Microsoft 365 capability parity

- Maintain Graph-backed mail, calendar, OneDrive, sharing, and large-file paths.
- Keep native Microsoft 365 MCP tools where they provide platform-specific behavior.
- Add a capability matrix describing which tool path is authoritative for each operation.
- Test permission failures, unprovisioned workloads, throttling, retries, and token expiry.
- Add opt-in policies for high-impact outbound actions.

## 5. Reliability and observability

- Add deterministic startup health checks for edge, gateway, Hermes, and sidecar.
- Measure cold-start and warm-turn latency with the complete image.
- Add structured correlation across all four processes without logging tokens or message bodies.
- Verify persisted state across idle teardown, restart, image upgrade, and rollback.
- Add bounded retries and clear terminal errors for Foundry, Graph, and MCP failures.

## 6. Test and release gates

- Run unit tests and static checks on every change.
- Add container smoke tests for readiness and internal service connectivity.
- Add tenant-isolated integration tests for mail, calendar, OneDrive create/read/share, and routines.
- Require secret scanning and tenant-identifier scanning before release.
- Test new hosted-agent versions before traffic is routed and retain a verified rollback target.

## 7. Distribution documentation

- Publish a minimal installation guide for tenant administrators.
- Document required Microsoft Entra, Microsoft 365, Foundry, registry, storage, and Key Vault roles.
- Document data boundaries, retention, outbound actions, and operational ownership.
- Add troubleshooting guidance based on stable error contracts rather than deployment-specific logs.
- Define licensing and support terms before public distribution.
