namespace HelloWorldA365.Services;

using System.Text.Json;
using System.Text.Json.Serialization;

/// <summary>
/// Aggregates the edge and its three loopback dependencies without exposing topology or errors.
///
/// Foundry's <c>/readiness</c> probe must stay shallow: Hermes can take several minutes to start,
/// while the edge must bind within the platform's readiness window. This separate endpoint is for
/// operators and smoke tests that need to know whether the complete Hermes runtime is usable.
/// </summary>
public sealed class RuntimeHealthService(
    IConfiguration configuration,
    IHttpClientFactory httpClientFactory,
    ILogger<RuntimeHealthService> logger)
{
    private readonly string _routerUrl = (configuration["RouterUrl"] ?? "").TrimEnd('/');
    private static readonly TimeSpan CheckTimeout = TimeSpan.FromSeconds(2);
    private const int MaxPayloadBytes = 4 * 1024;

    public async Task<RuntimeHealthStatus> CheckAsync(CancellationToken cancellationToken)
    {
        var components = UnhealthyComponents();
        if (string.IsNullOrWhiteSpace(_routerUrl))
            return new(false, components);

        try
        {
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            timeout.CancelAfter(CheckTimeout);
            using var client = httpClientFactory.CreateClient();
            using var request = new HttpRequestMessage(HttpMethod.Get, $"{_routerUrl}/healthz");
            using var response = await client.SendAsync(
                request,
                HttpCompletionOption.ResponseHeadersRead,
                timeout.Token);

            // A compromised or misrouted loopback dependency must not make the edge allocate an
            // unbounded response. LoadIntoBufferAsync throws before JSON parsing above this limit.
            await response.Content.LoadIntoBufferAsync(MaxPayloadBytes);
            using var document = JsonDocument.Parse(
                await response.Content.ReadAsStringAsync(timeout.Token));
            if (!document.RootElement.TryGetProperty("components", out var source)
                || source.ValueKind != JsonValueKind.Object)
                return new(false, components);

            components["gateway"] = ReadBoolean(source, "gateway");
            components["hermes"] = ReadBoolean(source, "hermes");
            components["m365_sidecar"] = ReadBoolean(source, "m365_sidecar");
            var ok = response.IsSuccessStatusCode && components.Values.All(value => value);
            return new(ok, components);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex)
        {
            // Health logs are intentionally structural. HTTP bodies and exception messages can
            // contain internal endpoints or framework diagnostics and provide no operator value.
            logger.LogWarning(
                "Runtime dependency health check failed; type={ExceptionType}",
                ex.GetType().Name);
            return new(false, components);
        }
    }

    private static Dictionary<string, bool> UnhealthyComponents() => new(StringComparer.Ordinal)
    {
        ["edge"] = true,
        ["gateway"] = false,
        ["hermes"] = false,
        ["m365_sidecar"] = false,
    };

    private static bool ReadBoolean(JsonElement source, string name) =>
        source.TryGetProperty(name, out var value)
        && value.ValueKind is JsonValueKind.True;
}

public sealed record RuntimeHealthStatus(
    [property: JsonPropertyName("ok")] bool Ok,
    [property: JsonPropertyName("components")] IReadOnlyDictionary<string, bool> Components);
