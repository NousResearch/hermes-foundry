namespace HelloWorldA365Agent.Tests;

using HelloWorldA365.Services;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging.Abstractions;
using Xunit;

public sealed class ModelPolicyServiceTests : IDisposable
{
    private readonly string _state = Path.Combine(
        Path.GetTempPath(), $"hermes-model-tests-{Guid.NewGuid():N}");

    [Fact]
    public async Task DiscoveryFollowsSafePaginationFiltersCapabilitiesAndAppliesPriority()
    {
        var handler = new RecordingHandler(
            RecordingHandler.Json("""
                {
                  "value": [
                    {"type":"ModelDeployment","name":"small","capabilities":{"chat_completion":true}},
                    {"type":"ModelDeployment","name":"embedding","capabilities":{"embeddings":true}},
                    {"type":"Other","name":"ignored","capabilities":{"responses":true}}
                  ],
                  "nextLink":"https://example.services.ai.azure.com/api/projects/demo/deployments?api-version=v1&page=2"
                }
                """),
            RecordingHandler.Json("""
                {"value":[
                  {"type":"ModelDeployment","name":"best","capabilities":{"responses":"true"}},
                  {"type":"ModelDeployment","name":"small","capabilities":{"chatCompletion":true}}
                ]}
                """));
        var service = CreateService(handler, new Dictionary<string, string?>
        {
            ["MODEL_PRIORITY"] = "best,small",
            ["MODEL_ALLOWED_DEPLOYMENTS"] = "small,best",
        });

        var selection = await service.ResolveAsync("session-a", CancellationToken.None);

        Assert.Equal("best", selection.Model);
        Assert.Equal(["small", "best"], selection.Available);
        Assert.Equal(2, handler.Requests.Count);
        Assert.All(handler.Requests, request =>
        {
            Assert.Equal("Bearer", request.Headers["Authorization"].Split(' ')[0]);
            Assert.Equal("test-token", request.Headers["Authorization"].Split(' ')[1]);
        });
    }

    [Fact]
    public async Task UnsafePaginationHostFallsBackToDeploymentSnapshot()
    {
        var handler = new RecordingHandler(RecordingHandler.Json("""
            {
              "value":[{"type":"ModelDeployment","name":"live","capabilities":{"responses":true}}],
              "nextLink":"https://untrusted.example/deployments?page=2"
            }
            """));
        var service = CreateService(handler, new Dictionary<string, string?>
        {
            ["MODEL_DEPLOYMENTS_SNAPSHOT"] = "snapshot-model",
            ["MODEL_PRIORITY"] = "snapshot-model",
        });

        var selection = await service.ResolveAsync("session-a", CancellationToken.None);

        Assert.Equal("snapshot-model", selection.Model);
        Assert.Equal("deployment snapshot", selection.DiscoverySource);
        Assert.Contains("unavailable", selection.Warning);
        Assert.Single(handler.Requests);
    }

    [Fact]
    public async Task ModelCommandPersistsOnlyAnAvailableDeploymentForThatSession()
    {
        var handler = new RecordingHandler(RecordingHandler.Json("""
            {"value":[
              {"type":"ModelDeployment","name":"best","capabilities":{"responses":true}},
              {"type":"ModelDeployment","name":"small","capabilities":{"chat_completion":true}}
            ]}
            """));
        var service = CreateService(handler);

        var rejected = await service.HandleCommandAsync(
            "/model imaginary", "session-a", CancellationToken.None);
        var accepted = await service.HandleCommandAsync(
            "/model small", "session-a", CancellationToken.None);
        var selection = await service.ResolveAsync("session-a", CancellationToken.None);

        Assert.True(rejected.Handled);
        Assert.Contains("not an available model", rejected.Reply);
        Assert.True(accepted.Handled);
        Assert.Equal("small", selection.Model);
        Assert.True(selection.IsExplicit);
    }

    private ModelPolicyService CreateService(
        HttpMessageHandler handler,
        IDictionary<string, string?>? overrides = null)
    {
        var values = new Dictionary<string, string?>
        {
            ["AZURE_AI_PROJECT_ENDPOINT"] =
                "https://example.services.ai.azure.com/api/projects/demo",
            ["MODEL_DEPLOYMENT_NAME"] = "best",
            ["MODEL_DEPLOYMENTS_SNAPSHOT"] = "best,small",
            ["HERMES_STATE"] = _state,
        };
        foreach (var item in overrides ?? new Dictionary<string, string?>())
            values[item.Key] = item.Value;
        var configuration = new ConfigurationBuilder().AddInMemoryCollection(values).Build();
        return new ModelPolicyService(
            configuration,
            new FixedHttpClientFactory(handler),
            NullLogger<ModelPolicyService>.Instance,
            new FixedTokenCredential());
    }

    public void Dispose()
    {
        if (Directory.Exists(_state)) Directory.Delete(_state, recursive: true);
    }
}
