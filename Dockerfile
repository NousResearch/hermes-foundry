# Plan F — "full Foundry": Hermes and its edge in ONE container, hosted by Foundry.
#
# The canonical (Phase 1b) topology is four processes across three Azure resources:
#
#   Teams/Outlook ─► Foundry hosted edge ─► gateway (ACA) ─► Hermes (ACA) ─► sidecar (localhost)
#
# Plan F keeps all four processes and deletes the *network* between them. Nothing is rewritten;
# RouterUrl simply becomes localhost. That is the whole trick, and it is why this is a packaging
# change rather than a port — see PLAN_F.md §5.
#
#   Foundry hosted agent container:
#     :8088  edge      (.NET, HelloWorldA365Agent) — Activity + Responses, what Foundry calls
#     :8080  gateway   (edge-gateway/app.py)       — translation + agent-token vending
#     :8010  sidecar   (agent365_mcp.py)           — the agent's own M365 tools
#     :<hermes> Hermes (the real image)            — the brain
#
# Base is the Hermes image because it is the largest and the hardest to reproduce; everything else
# is copied on top. That also means the s6 supervision Hermes already ships with is what runs the
# extra processes, rather than a second supervisor fighting it.
# PINNED BY DIGEST. `:latest` moved under us and broke this image: a build on 2026-08-16 produced a
# working sidecar, and the same source rebuilt on 2026-08-17 produced one that could not even import
# (`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`, Python 3.13.5) because the base had
# changed underneath. Two builds of identical source were not the same image, which made the failure
# look like a config difference between stacks for hours.
# v2026.8.3 — the build that ran through 2026-08-13/14 when the tools last worked end to end.
ARG HERMES_IMAGE=docker.io/nousresearch/hermes-agent@sha256:16788311e2fa3035456bdc1bafb8ec2b1777db64ebf020af9bb7eb73c3712c9e

# Keep the Microsoft build and runtime layers reproducible too. These are manifest-list digests,
# not architecture-specific child digests, so the same Dockerfile still resolves correctly on
# ARM64 developer machines and AMD64 Azure builders.
ARG DOTNET_SDK_IMAGE=mcr.microsoft.com/dotnet/sdk:9.0@sha256:20387c6674c30e46def0cc8cb557bd2a69b35afdf18c19c3694638d0a90897e4
ARG DOTNET_ASPNET_IMAGE=mcr.microsoft.com/dotnet/aspnet:9.0@sha256:779b0b298964fe93e6c6388c8bb823393a7c301b79cd5b3d80632bc886b2ffd4

# Build the edge inside the container build so a new tenant can use Azure Container Registry's
# cloud builder without installing .NET locally. global.json remains authoritative for the SDK
# feature band and fails the image build if the pinned project can no longer compile.
FROM ${DOTNET_SDK_IMAGE} AS edge-build
WORKDIR /src
COPY src/edge/ ./
RUN dotnet publish HelloWorldA365Agent.csproj -c Release -o /out

FROM ${DOTNET_ASPNET_IMAGE} AS aspnet-runtime

FROM ${HERMES_IMAGE} AS runtime

USER root

# .NET runtime for the edge. aspnet 9.0 to match the edge's target framework and global.json band.
ENV DOTNET_ROOT=/usr/share/dotnet \
    PATH=$PATH:/usr/share/dotnet \
    DOTNET_RUNNING_IN_CONTAINER=true \
    DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates python3 python3-pip python3-venv iproute2; \
    apt-get clean; rm -rf /var/lib/apt/lists/*

# Take the runtime from Microsoft's own image rather than installing it with dotnet-install.sh.
# The script-installed runtime aborted at startup (SIGABRT, rc=134) with no usable message; the
# working thin edge uses this exact base, so copy from the source of truth instead of reproducing it.
COPY --from=aspnet-runtime /usr/share/dotnet /usr/share/dotnet

# Sidecar deps in a DEDICATED VENV — do not install into Hermes' environment.
#
# What went wrong before: `pip install --break-system-packages mcp==1.9.4` put the packages wherever
# the ambient `pip` pointed, while the `python3` that runs the sidecar is Hermes' own venv
# (`/opt/hermes/.venv/lib/python3.13/site-packages`). The two were not the same interpreter, so the
# sidecar died at import with `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` — and
# because Hermes attaches MCP servers only once at startup, hfull came up with 10 tools instead of 25:
# no mail, no calendar, no files. Chat worked perfectly, so it looked healthy.
#
# A venv built from /usr/bin/python3 fixes both halves: the sidecar gets the exact pins it needs, and
# it cannot break (or be broken by) Hermes' own dependency set — which matters because Hermes ships its
# own `mcp` and we must not overwrite it.
# Pinned versions for the usual reason: this process holds the agent's Graph token.
# pillow is pinned even though python-pptx already requires it (>=3.3.2). screenshot_url's shrink
# ladder depends on it DIRECTLY and was measured against exactly 12.1.1, so an explicit pin keeps a
# future python-pptx change from silently breaking image attachments — or shifting the encoder under
# a size budget that was chosen from measurements.
RUN set -eux; \
    /usr/bin/python3 -m venv /opt/sidecar-venv; \
    /opt/sidecar-venv/bin/pip install --no-cache-dir --upgrade pip; \
    /opt/sidecar-venv/bin/pip install --no-cache-dir \
        "mcp==1.9.4" "pypdf==5.1.0" "playwright==1.49.1" \
        "python-docx==1.1.2" "openpyxl==3.1.5" "python-pptx==1.0.2" \
        "pillow==12.1.1" \
        "fastapi==0.115.6" "uvicorn==0.34.0" "httpx==0.28.1"

# The pinned Hermes base accepts Entra ID for the primary Foundry model, but its fallback provider
# router drops the fallback entry's auth_mode and can silently construct an API-key client. Apply a
# narrow, asserted source overlay; the build fails if the pinned upstream layout changes instead of
# producing an image with an unverified partial patch.
COPY src/hermes-patches/apply_fallback_entra_patch.py /tmp/apply_fallback_entra_patch.py
COPY src/hermes-patches/apply_foundry_annotations_patch.py /tmp/apply_foundry_annotations_patch.py
RUN /usr/bin/python3 /tmp/apply_fallback_entra_patch.py \
    && /usr/bin/python3 /tmp/apply_foundry_annotations_patch.py \
    && /usr/bin/python3 -m py_compile \
        /opt/hermes/agent/auxiliary_client.py \
        /opt/hermes/agent/chat_completion_helpers.py \
        /opt/hermes/agent/codex_responses_adapter.py \
    && rm /tmp/apply_fallback_entra_patch.py /tmp/apply_foundry_annotations_patch.py

COPY --from=edge-build /out/ /opt/edge/
COPY src/gateway/app.py /opt/gateway/app.py
COPY src/gateway/state_sync.py /opt/gateway/state_sync.py
COPY src/gateway/access_policy.py /opt/gateway/access_policy.py
COPY src/sidecar/agent365_mcp.py /opt/sidecar/agent365_mcp.py
COPY config/state-sync.defaults.json /opt/state-sync.defaults.json
COPY start.sh    /opt/start.sh
RUN chmod +x /opt/start.sh

# BUILD-TIME GUARDS. Every one of these was a silent runtime failure first, found only by a human
# noticing the agent behaved oddly. Assert them here so a broken image never deploys.
#   1. The sidecar imports AND registers its full tool surface with the venv interpreter.
#   2. The .NET runtime actually executes (a script-installed one aborted with SIGABRT at startup).
RUN cd /opt/sidecar && /opt/sidecar-venv/bin/python -c "\
import asyncio, agent365_mcp as m;\
tools = asyncio.run(m.mcp.list_tools());\
names = sorted(t.name for t in tools);\
assert len(names) >= 20, f'only {len(names)} tools registered: {names}';\
required = ['remember','recall','propose_access_change','list_access_permissions','state_sync_config','create_routine','list_routines','remove_routine','resolve_person','send_email','read_email','list_inbox','create_event','create_docx','edit_docx','create_xlsx','create_pptx','create_pdf','attach_to_event','share_file','web_search','fetch_url','browse_url','screenshot_url','run_code','read_uploaded_file'];\
missing = [r for r in required if r not in names];\
assert not missing, f'missing tools: {missing}';\
print('SIDECAR GUARD OK:', len(names), 'tools')"
RUN /usr/share/dotnet/dotnet --list-runtimes | grep -q "Microsoft.AspNetCore.App 9" \
    && echo "DOTNET GUARD OK"

# Foundry calls the edge; the rest are localhost-only and deliberately NOT exposed.
EXPOSE 8088
# The whole Plan F change, in six lines: every hop that was an ACA ingress becomes loopback.
# Values mirror the live Phase 1b deployment exactly (hermes API server :8642, gateway :8080,
# sidecar :8010) so behaviour is unchanged by the move — only the network between them is gone.
ENV ASPNETCORE_URLS=http://+:8088 \
    MCP_PLATFORM_ENDPOINT=https://agent365.svc.cloud.microsoft \
    RouterUrl=http://127.0.0.1:8080 \
    AGENT_TOKEN_URL=http://127.0.0.1:8080 \
    HERMES_MCP_URL=http://127.0.0.1:8010/mcp \
    HERMES_URL=http://127.0.0.1:8642 \
    API_SERVER_ENABLED=true \
    API_SERVER_HOST=0.0.0.0 \
    API_SERVER_PORT=8642 \
    HERMES_COLD_START_WAIT=240

# Sidecar settings that carry optional Microsoft-hosted tool behavior. This is easy to omit because
# it is not required to *start*, and the failure is not visible in chat:
#   BROWSER_TOOLBOX_VERSION  selects the Microsoft-hosted Chromium contract used by browse_url.
#                            Absent, web browsing degrades to fetch-only with no error.
# Stated explicitly rather than left to defaults, because the live deployment states it and the
# point of this image is to be behaviourally identical to it.
ENV BROWSER_TOOLBOX_VERSION=1

# start.sh brings up our three processes, then execs the Hermes entrypoint verbatim — the same
# `/init /opt/hermes/docker/main-wrapper.sh gateway run` that infra/hermes-runtime.bicep already runs
# in production. Hermes therefore starts exactly as its maintainers intend, under its own s6, and we
# do not have to reverse-engineer the s6 unit layout to add processes beside it.
ENTRYPOINT ["/opt/start.sh"]
