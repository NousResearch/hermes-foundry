namespace HelloWorldA365Agent.Tests;

using System.Net;
using System.Text;
using Azure.Core;

internal sealed class FixedTokenCredential(string token = "test-token") : TokenCredential
{
    public override AccessToken GetToken(
        TokenRequestContext requestContext,
        CancellationToken cancellationToken) => new(token, DateTimeOffset.UtcNow.AddMinutes(10));

    public override ValueTask<AccessToken> GetTokenAsync(
        TokenRequestContext requestContext,
        CancellationToken cancellationToken) => ValueTask.FromResult(GetToken(requestContext, cancellationToken));
}

internal sealed class RecordingHandler : HttpMessageHandler
{
    private readonly Queue<HttpResponseMessage> _responses;

    public RecordingHandler(params HttpResponseMessage[] responses) => _responses = new(responses);

    public List<RecordedRequest> Requests { get; } = [];

    protected override async Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        var body = request.Content is null
            ? null
            : await request.Content.ReadAsStringAsync(cancellationToken);
        Requests.Add(new RecordedRequest(
            request.Method,
            request.RequestUri!,
            request.Headers.ToDictionary(x => x.Key, x => string.Join(",", x.Value),
                StringComparer.OrdinalIgnoreCase),
            body));
        if (_responses.Count == 0)
            throw new InvalidOperationException("No response was configured for this request.");
        return _responses.Dequeue();
    }

    public static HttpResponseMessage Json(string json, HttpStatusCode status = HttpStatusCode.OK) =>
        new(status) { Content = new StringContent(json, Encoding.UTF8, "application/json") };
}

internal sealed record RecordedRequest(
    HttpMethod Method,
    Uri Uri,
    IReadOnlyDictionary<string, string> Headers,
    string? Body);

internal sealed class FixedHttpClientFactory(HttpMessageHandler handler) : IHttpClientFactory
{
    public HttpClient CreateClient(string name) => new(handler, disposeHandler: false);
}
