namespace HelloWorldA365.AgentLogic.ResponsesApi;

using System.Net.Http.Headers;
using System.Text.Json;
using Azure.Core;
using HelloWorldA365.Models;
using HelloWorldA365.Services;
using Microsoft.Agents.Builder;
using Microsoft.Agents.Builder.App.UserAuth;

/// <summary>
/// Selects the edge implementation for an interactive Activity turn.
///
/// <para><b>HermesRouter is the supported Full Foundry runtime and the default.</b> The factory's
/// name is historical: <see cref="ResponsesApiAgentLogicService"/> is retained only as a
/// non-production compatibility/reference path for comparing the stock Microsoft Responses API
/// flow and diagnosing platform identity or MCP discovery. It is not the main mode, is never
/// selected implicitly, and is not expected to have feature parity with Hermes.</para>
/// </summary>
public sealed class ResponsesApiAgentLogicServiceFactory(
    IConfiguration configuration,
    ILogger<ResponsesApiAgentLogicServiceFactory> logger,
    AgentTokenHelper tokenHelper)
{
    private static readonly HttpClient HttpClient = new();

    public async Task<IAgentLogicService> CreateAsync(AgentMetadata agent, ITurnContext turnContext, UserAuthorization userAuthorization)
    {
        // PRODUCTION PATH: HermesRouter forwards the turn to the Hermes gateway in this same
        // Full Foundry container. Keep this as the default. In particular, do not change this to a
        // loose "anything other than HermesRouter means ResponsesApi" fallback: a misspelled setting
        // must fail loudly instead of silently replacing Hermes with the compatibility implementation.
        var agentLogic = configuration["AgentLogic"] ?? "HermesRouter";
        if (agentLogic.Equals("HermesRouter", StringComparison.OrdinalIgnoreCase))
        {
            logger.LogInformation(
                "Using production HermesRouter mode for agent {AgentId} (instance {InstanceId}); forwarding to the in-container Hermes gateway.",
                agent.AgentId, agent.AgentApplicationId);
            return new HermesRouter.HermesRouterAgentLogicService(agent, configuration, logger, tokenHelper, userAuthorization);
        }

        if (!agentLogic.Equals("ResponsesApi", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException(
                $"Unsupported AgentLogic value '{agentLogic}'. Use 'HermesRouter'. " +
                "'ResponsesApi' is available only as an explicit non-production compatibility mode.");
        }

        // NON-PRODUCTION COMPATIBILITY PATH. This directly invokes the Foundry Responses API with
        // discovered MCP servers. It bypasses the Hermes gateway, Hermes prompt/tool loop, sidecar,
        // memory, and authority behavior. It exists for controlled diagnostics and comparison with
        // Microsoft's stock sample flow; it is not a production fallback or an alternate Hermes mode.
        logger.LogWarning(
            "NON-PRODUCTION AgentLogic=ResponsesApi selected for agent {AgentId} (instance {InstanceId}). " +
            "This compatibility path bypasses Hermes and does not provide Full Foundry feature parity.",
            agent.AgentId, agent.AgentApplicationId);

        // Diagnostic-only identity metadata for comparing this stock-compatible path with the
        // HermesRouter path. Do not treat this log as routine credential provisioning.
        logger.LogWarning(
            "AGENTIC-IDS blueprint={Blueprint} instance={Instance} upn={Upn} tenant={Tenant} userId={UserId}",
            agent.AgentApplicationId, agent.AgentId, agent.EmailId, agent.TenantId, agent.UserId);

        // Acquire token for MCP servers
        // Public production audience for the Microsoft Agent 365 Tools Gateway.
        var requestContext = new TokenRequestContext(["ea9ffc3e-8a23-4a7d-836d-234d7c7565c1/.default"]);
        var tokenCredential = new AgentTokenCredential(tokenHelper, agent);
        var accessToken = await tokenCredential.GetTokenAsync(requestContext, CancellationToken.None);

        logger.LogInformation("Acquired token for Responses API MCP tools. Expires at: {Expiration}", accessToken.ExpiresOn);

        var mcpServers = await GetMcpServersAsync(agent.AgentId, accessToken.Token);

        IAgentLogicService service = new ResponsesApiAgentLogicService(
            agent,
            configuration,
            logger,
            accessToken.Token,
            mcpServers);

        return service;
    }

    private async Task<List<McpServerConfig>> GetMcpServersAsync(Guid agentInstanceId, string accessToken)
    {
        var source = configuration["McpDiscoverySource"] ?? "API";

        if (source.Equals("Manifest", StringComparison.OrdinalIgnoreCase))
        {
            logger.LogInformation("Loading MCP servers from ToolingManifest.json");
            return LoadFromManifest();
        }

        logger.LogInformation("Discovering MCP servers from API for agent {AgentId}", agentInstanceId);
        return await DiscoverFromApiAsync(agentInstanceId, accessToken);
    }

    private List<McpServerConfig> LoadFromManifest()
    {
        var manifestPath = Path.Combine(AppContext.BaseDirectory, "ToolingManifest.json");
        if (!File.Exists(manifestPath))
        {
            logger.LogWarning("ToolingManifest.json not found at {Path}", manifestPath);
            return [];
        }

        var json = File.ReadAllText(manifestPath);
        var manifest = JsonSerializer.Deserialize<ToolingManifest>(json);
        var servers = manifest?.McpServers ?? [];
        logger.LogInformation("Loaded {Count} MCP servers from ToolingManifest.json", servers.Count);
        return servers;
    }

    private async Task<List<McpServerConfig>> DiscoverFromApiAsync(Guid agentInstanceId, string accessToken)
    {
        var url = $"https://agent365.svc.cloud.microsoft/agents/v2/{agentInstanceId}/mcpServers";
        logger.LogInformation("Discovering MCP servers from {Url}", url);

        using var request = new HttpRequestMessage(HttpMethod.Get, url);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", accessToken);

        var response = await HttpClient.SendAsync(request);
        var responseContent = await response.Content.ReadAsStringAsync();

        if (!response.IsSuccessStatusCode)
        {
            logger.LogError("Failed to discover MCP servers. Status: {StatusCode}, Response: {Response}", response.StatusCode, responseContent);
            return [];
        }

        var servers = JsonSerializer.Deserialize<List<McpServerConfig>>(responseContent) ?? [];
        logger.LogInformation("Discovered {Count} MCP servers for agent {AgentId}", servers.Count, agentInstanceId);

        foreach (var server in servers)
        {
            logger.LogInformation("  MCP Server: {Name} ({Url})", server.McpServerName, server.Url);
        }

        return servers;
    }
}
