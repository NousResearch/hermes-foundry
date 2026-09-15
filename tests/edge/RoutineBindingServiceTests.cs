namespace HelloWorldA365Agent.Tests;

using System.Net;
using System.Text;
using System.Text.Json;
using HelloWorldA365.Models;
using HelloWorldA365.Services;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging.Abstractions;
using Xunit;

public sealed class RoutineBindingServiceTests
{
    private static readonly Guid TenantId = Guid.Parse("11111111-1111-1111-1111-111111111111");
    private static readonly Guid BlueprintId = Guid.Parse("22222222-2222-2222-2222-222222222222");
    private static readonly Guid AgentIdentityId = Guid.Parse("33333333-3333-3333-3333-333333333333");
    private static readonly Guid AgentUserId = Guid.Parse("44444444-4444-4444-4444-444444444444");

    [Fact]
    public async Task OwnerCapabilityCreatesBoundRoutineWithRequiredFoundryContract()
    {
        var handler = new RecordingHandler(RecordingHandler.Json("{}"));
        var httpContext = new DefaultHttpContext();
        httpContext.Request.Headers["x-agent-foundry-call-id"] = "opaque-call-id";
        var service = CreateService(handler, httpContext);
        var capability = service.MintOwnerCapability(
            Agent(), "owner", "owner-object", "owner@example.com");

        Assert.NotNull(capability);
        var result = await service.CreateAsync(capability!, new CreateRoutineRequest
        {
            Name = "Monday summary",
            Task = "Summarize the agent inbox and send the owner an email.",
            CronExpression = "0 8 * * 1",
            TimeZone = "America/Los_Angeles",
        }, CancellationToken.None);

        Assert.True(result.Ok, result.Message);
        var request = Assert.Single(handler.Requests);
        Assert.Equal(HttpMethod.Put, request.Method);
        Assert.Equal("v1", ParseQuery(request.Uri)["api-version"]);
        Assert.Equal("Routines=V1Preview", request.Headers["Foundry-Features"]);
        Assert.Equal("opaque-call-id", request.Headers["x-agent-foundry-call-id"]);
        Assert.Equal("Bearer header.eyJvaWQiOiJ0ZXN0In0.signature",
            request.Headers["Authorization"]);

        using var body = JsonDocument.Parse(request.Body!);
        Assert.Equal("invoke_agent_responses_api",
            body.RootElement.GetProperty("action").GetProperty("type").GetString());
        var input = body.RootElement.GetProperty("action").GetProperty("input").GetString()!;
        Assert.True(service.TryReadInvocation(input, out var binding, out var error), error);
        Assert.Equal(AgentIdentityId.ToString(), binding.AgentIdentityId);
        Assert.Equal(AgentUserId.ToString(), binding.AgentUserId);
        Assert.Equal("owner@example.com", binding.OwnerUpn);
        Assert.Equal(RoutineBindingService.AgentUserAuthorizationMode, binding.AuthorizationMode);
        Assert.Equal("0 8 * * 1", binding.CronExpression);
    }

    [Fact]
    public void NonOwnerCannotMintRoutineAuthority()
    {
        var service = CreateService(new RecordingHandler());
        Assert.Null(service.MintOwnerCapability(Agent(), "colleague", null, null));
        Assert.Null(service.MintOwnerCapability(Agent(), "outsider", null, null));
    }

    [Fact]
    public async Task TamperedCapabilityFailsBeforeAnyFoundryRequest()
    {
        var handler = new RecordingHandler();
        var service = CreateService(handler);
        var capability = service.MintOwnerCapability(
            Agent(), "owner", "owner-object", "owner@example.com")!;
        var tampered = capability[..^1] + (capability[^1] == 'A' ? "B" : "A");

        var result = await service.CreateAsync(tampered, new CreateRoutineRequest
        {
            Name = "test",
            Task = "test",
            CronExpression = "0 8 * * 1",
            TimeZone = "UTC",
        }, CancellationToken.None);

        Assert.False(result.Ok);
        Assert.Contains("routine authority", result.Message);
        Assert.Empty(handler.Requests);
    }

    [Fact]
    public async Task FoundryErrorIsReportedWithoutLeakingNestedResponseData()
    {
        var handler = new RecordingHandler(RecordingHandler.Json(
            """{"error":{"message":"permission denied","internal":"do-not-return"}}""",
            HttpStatusCode.Forbidden));
        var service = CreateService(handler);
        var capability = service.MintOwnerCapability(
            Agent(), "owner", "owner-object", "owner@example.com")!;

        var result = await service.CreateAsync(capability, new CreateRoutineRequest
        {
            Name = "test",
            Task = "test",
            CronExpression = "0 8 * * 1",
            TimeZone = "UTC",
        }, CancellationToken.None);

        Assert.False(result.Ok);
        Assert.Contains("HTTP 403", result.Message);
        Assert.Contains("permission denied", result.Message);
        Assert.DoesNotContain("do-not-return", result.Message);
    }

    private static RoutineBindingService CreateService(
        HttpMessageHandler handler,
        HttpContext? context = null)
    {
        var values = new Dictionary<string, string?>
        {
            ["RoutineBindingSignKey"] = new string('s', 48),
            ["FoundryProjectEndpoint"] =
                "https://example.services.ai.azure.com/api/projects/demo",
            ["FoundryAgentName"] = "hermes-test",
            ["AgentBlueprintId"] = BlueprintId.ToString(),
            ["AgentTenantId"] = TenantId.ToString(),
        };
        var accessor = new HttpContextAccessor { HttpContext = context };
        return new RoutineBindingService(
            new ConfigurationBuilder().AddInMemoryCollection(values).Build(),
            new FixedHttpClientFactory(handler),
            accessor,
            NullLogger<RoutineBindingService>.Instance,
            new FixedTokenCredential("header.eyJvaWQiOiJ0ZXN0In0.signature"));
    }

    private static AgentMetadata Agent() => new()
    {
        TenantId = TenantId,
        AgentApplicationId = BlueprintId,
        AgentId = AgentIdentityId,
        UserId = AgentUserId,
        EmailId = "agent@example.com",
    };

    private static Dictionary<string, string> ParseQuery(Uri uri) =>
        uri.Query.TrimStart('?')
            .Split('&', StringSplitOptions.RemoveEmptyEntries)
            .Select(part => part.Split('=', 2))
            .ToDictionary(
                part => Uri.UnescapeDataString(part[0]),
                part => Uri.UnescapeDataString(part.Length > 1 ? part[1] : ""),
                StringComparer.OrdinalIgnoreCase);
}
