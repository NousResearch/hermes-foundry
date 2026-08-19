# Hermes Full Foundry packages four processes into one Foundry-hosted container:
#
#   Foundry hosted agent container:
#     :8088  edge      (.NET, HelloWorldA365Agent) — Activity + Responses, what Foundry calls
#     :8080  gateway   (edge-gateway/app.py)       — translation + agent-token vending
#     :8010  sidecar   (agent365_mcp.py)           — the agent's own M365 tools
#     :<hermes> Hermes (the real image)            — the brain
#
# The Hermes base image is pinned by digest so identical source produces an identical base layer.
ARG HERMES_IMAGE=docker.io/nousresearch/hermes-agent@sha256:16788311e2fa3035456bdc1bafb8ec2b1777db64ebf020af9bb7eb73c3712c9e

# ---- build the edge -------------------------------------------------------------------------
# Published on the host by build.sh because global.json pins the required SDK band.

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

# Take the runtime from Microsoft's official image.
COPY --from=mcr.microsoft.com/dotnet/aspnet:9.0 /usr/share/dotnet /usr/share/dotnet

# Keep sidecar dependencies in a dedicated, pinned virtual environment so they cannot conflict with
# Hermes' Python environment. Pillow is pinned explicitly because attachment fitting depends on it.
RUN set -eux; \
    /usr/bin/python3 -m venv /opt/sidecar-venv; \
    /opt/sidecar-venv/bin/pip install --no-cache-dir --upgrade pip; \
    /opt/sidecar-venv/bin/pip install --no-cache-dir \
        "mcp==1.9.4" "pypdf==5.1.0" "playwright==1.49.1" \
        "python-docx==1.1.2" "openpyxl==3.1.5" "python-pptx==1.0.2" \
        "pillow==12.1.1" \
        "fastapi==0.115.6" "uvicorn==0.34.0" "httpx==0.28.1"

COPY edge/       /opt/edge/
COPY gateway/    /opt/gateway/
COPY sidecar/    /opt/sidecar/
COPY start.sh    /opt/start.sh
RUN chmod +x /opt/start.sh

# Build-time guards prevent an incomplete tool surface or unusable .NET runtime from deploying.
#   1. The sidecar imports AND registers its full tool surface with the venv interpreter.
#   2. The .NET runtime actually executes (a script-installed one aborted with SIGABRT at startup).
RUN cd /opt/sidecar && /opt/sidecar-venv/bin/python -c "\
import asyncio, agent365_mcp as m;\
tools = asyncio.run(m.mcp.list_tools());\
names = sorted(t.name for t in tools);\
assert len(names) >= 20, f'only {len(names)} tools registered: {names}';\
required = ['send_email','read_email','list_inbox','create_event','create_docx','create_xlsx','create_pptx','create_pdf','attach_to_event','share_file','web_search','fetch_url','browse_url','screenshot_url','run_code'];\
missing = [r for r in required if r not in names];\
assert not missing, f'missing tools: {missing}';\
print('SIDECAR GUARD OK:', len(names), 'tools')"
RUN /usr/share/dotnet/dotnet --list-runtimes | grep -q "Microsoft.AspNetCore.App 9" \
    && echo "DOTNET GUARD OK"

# Foundry calls the edge; the rest are localhost-only and deliberately NOT exposed.
EXPOSE 8088
# Internal services communicate only over loopback.
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

# Sidecar settings that carry the agent's M365 capability. Both are easy to omit because neither is
# required to *start*, and neither failure is visible in chat:
#   AGENT_TOKEN_TTL          how long a fetched Graph token is reused before re-polling the gateway.
#   BROWSER_TOOLBOX_VERSION  selects the Microsoft-hosted Chromium contract used by browse_url.
#                            Absent, web browsing degrades to fetch-only with no error.
# Keep these explicit so release behavior does not drift with dependency defaults.
ENV AGENT_TOKEN_TTL=60 \
    BROWSER_TOOLBOX_VERSION=1

# start.sh supervises the edge, gateway, sidecar, and Hermes processes.
ENTRYPOINT ["/opt/start.sh"]
