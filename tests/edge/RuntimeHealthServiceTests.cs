namespace HelloWorldA365Agent.Tests;

using System.Net;
using HelloWorldA365.Services;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging.Abstractions;
using Xunit;

public sealed class RuntimeHealthServiceTests
{
    [Fact]
    public async Task HealthyGatewayReportsAllFourComponentsWithoutTopology()
    {
        var handler = new RecordingHandler(RecordingHandler.Json(
            """{"ok":true,"components":{"gateway":true,"hermes":true,"m365_sidecar":true}}"""));
        var service = CreateService(handler);

        var result = await service.CheckAsync(CancellationToken.None);

        Assert.True(result.Ok);
        Assert.Equal(new[] { "edge", "gateway", "hermes", "m365_sidecar" },
            result.Components.Keys.OrderBy(value => value));
        Assert.All(result.Components.Values, Assert.True);
        var request = Assert.Single(handler.Requests);
        Assert.Equal(HttpMethod.Get, request.Method);
        Assert.Equal("/healthz", request.Uri.AbsolutePath);
        Assert.DoesNotContain("example.internal", System.Text.Json.JsonSerializer.Serialize(result));
    }

    [Fact]
    public async Task UnhealthyDependencyFailsClosedEvenIfGatewayReturnsSuccess()
    {
        var handler = new RecordingHandler(RecordingHandler.Json(
            """{"ok":true,"components":{"gateway":true,"hermes":false,"m365_sidecar":true}}"""));
        var service = CreateService(handler);

        var result = await service.CheckAsync(CancellationToken.None);

        Assert.False(result.Ok);
        Assert.True(result.Components["edge"]);
        Assert.True(result.Components["gateway"]);
        Assert.False(result.Components["hermes"]);
        Assert.True(result.Components["m365_sidecar"]);
    }

    [Fact]
    public async Task MalformedOrDetailedFailureReturnsOnlyComponentBooleans()
    {
        var handler = new RecordingHandler(new HttpResponseMessage(HttpStatusCode.ServiceUnavailable)
        {
            Content = new StringContent("tenant endpoint and secret diagnostic")
        });
        var service = CreateService(handler);

        var result = await service.CheckAsync(CancellationToken.None);

        Assert.False(result.Ok);
        Assert.True(result.Components["edge"]);
        Assert.All(result.Components.Where(item => item.Key != "edge"),
            item => Assert.False(item.Value));
        var rendered = System.Text.Json.JsonSerializer.Serialize(result);
        Assert.DoesNotContain("tenant", rendered, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("secret", rendered, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("endpoint", rendered, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task MissingRouterConfigurationFailsWithoutNetworkAccess()
    {
        var handler = new RecordingHandler();
        var configuration = new ConfigurationBuilder().Build();
        var service = new RuntimeHealthService(
            configuration,
            new FixedHttpClientFactory(handler),
            NullLogger<RuntimeHealthService>.Instance);

        var result = await service.CheckAsync(CancellationToken.None);

        Assert.False(result.Ok);
        Assert.Empty(handler.Requests);
    }

    private static RuntimeHealthService CreateService(HttpMessageHandler handler)
    {
        var configuration = new ConfigurationBuilder().AddInMemoryCollection(
            new Dictionary<string, string?>
            {
                ["RouterUrl"] = "http://example.internal:8080/"
            }).Build();
        return new RuntimeHealthService(
            configuration,
            new FixedHttpClientFactory(handler),
            NullLogger<RuntimeHealthService>.Instance);
    }
}
