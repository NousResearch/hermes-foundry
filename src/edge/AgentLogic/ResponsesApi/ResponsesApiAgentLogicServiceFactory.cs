namespace HelloWorldA365.AgentLogic.ResponsesApi;

using System.Net.Http.Headers;
using System.Text.Json;
using Azure.Core;
using HelloWorldA365.Models;
using HelloWorldA365.Services;
using Microsoft.Agents.Builder;
using Microsoft.Agents.Builder.App.UserAuth;

/// <summary>
/// Factory for creating ResponsesApiAgentLogicService instances.
/// Discovers MCP servers either from the Agent365 API or from a local ToolingManifest.json,
/// controlled by the "McpDiscoverySource" config setting ("API" or "Manifest").
/// </summary>
public sealed class ResponsesApiAgentLogicServiceFactory(
    IConfiguration configuration,
    ILogger<ResponsesApiAgentLogicServiceFactory> logger,
    AgentTokenHelper tokenHelper,
    RoutineBindingService routineBindings,
    ModelPolicyService modelPolicy)
{
    private static readonly HttpClient HttpClient = new();

    public async Task<IAgentLogicService> CreateAsync(AgentMetadata agent, ITurnContext turnContext, UserAuthorization userAuthorization)
    {
        // THE THIN EDGE (default). Foundry supplies identity, hosting and governance; the turn itself
        // runs in the customer's own ACA sandbox behind sandbox-router. Returning here — before any
        // token acquisition — is deliberate: everything below this line talks to Foundry's data plane
        // (agentic-user token for MCP, MCP discovery, then the Responses API), which is precisely the
        // work the thin-edge architecture moves off Foundry. Set AgentLogic=ResponsesApi to run the
        // stock sample logic instead.
        var agentLogic = configuration["AgentLogic"] ?? "HermesRouter";
        if (agentLogic.Equals("HermesRouter", StringComparison.OrdinalIgnoreCase))
        {
            logger.LogInformation(
                "Using the Hermes router for the current agent instance");
            return new HermesRouter.HermesRouterAgentLogicService(
                agent, configuration, logger, tokenHelper, userAuthorization, routineBindings,
                modelPolicy);
        }

        // LOGGING ONLY. These are the ids on a turn that DEMONSTRABLY works — the stock path that
        // read the agent's mailbox. Every failure of the turn-less (routine) path came from guessing
        // agentAppInstanceId; this prints the value Microsoft's own code uses, so config can be set
        // from fact. Read it with :logstream. Do not change behaviour here.
        logger.LogWarning("Using the non-production ResponsesApi compatibility mode");

        // Acquire token for MCP servers
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

        logger.LogInformation("Discovering MCP servers from API for the current agent instance");
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
        logger.LogInformation("Discovering MCP servers from the Agent 365 service");

        using var request = new HttpRequestMessage(HttpMethod.Get, url);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", accessToken);

        var response = await HttpClient.SendAsync(request);
        var responseContent = await response.Content.ReadAsStringAsync();

        if (!response.IsSuccessStatusCode)
        {
            logger.LogError("Failed to discover MCP servers. Status: {StatusCode}", response.StatusCode);
            return [];
        }

        var servers = JsonSerializer.Deserialize<List<McpServerConfig>>(responseContent) ?? [];
        logger.LogInformation("Discovered {Count} MCP servers for the current agent instance", servers.Count);

        return servers;
    }
}
