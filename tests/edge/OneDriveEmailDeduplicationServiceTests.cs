namespace HelloWorldA365Agent.Tests;

using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using HelloWorldA365.Services;
using Microsoft.Extensions.Logging.Abstractions;
using Xunit;

public sealed class OneDriveEmailDeduplicationServiceTests
{
    private static readonly DateTimeOffset Now =
        new(2026, 9, 16, 12, 0, 0, TimeSpan.Zero);

    [Fact]
    public async Task FirstDeliveryCreatesOpaqueAtomicClaimInAgentAppRoot()
    {
        var handler = new RecordingHandler(
            Item("app-root", "app"),
            Item("claim-id", MarkerName("mail-message-id"), "\"claim-tag\"", Now));
        var service = CreateService(handler);

        var claim = await service.TryAcquireAsync(
            "agent-token", "mail-message-id", Now, CancellationToken.None);

        Assert.Equal(EmailClaimDisposition.Acquired, claim.Disposition);
        Assert.Equal("claim-id", claim.MarkerId);
        Assert.Equal(2, handler.Requests.Count);
        Assert.All(handler.Requests, request =>
            Assert.Equal("Bearer agent-token", request.Headers["Authorization"]));
        var create = handler.Requests[1];
        Assert.Equal(HttpMethod.Post, create.Method);
        Assert.DoesNotContain("mail-message-id", create.Uri.AbsoluteUri);
        Assert.DoesNotContain("mail-message-id", create.Body);
        using var body = JsonDocument.Parse(create.Body!);
        Assert.Equal("fail", body.RootElement
            .GetProperty("@microsoft.graph.conflictBehavior").GetString());
        Assert.Equal(MarkerName("mail-message-id"),
            body.RootElement.GetProperty("name").GetString());
    }

    [Fact]
    public async Task CompletedMarkerSuppressesRedeliveryWithoutCreatingAnotherClaim()
    {
        var marker = MarkerName("same-message");
        var handler = new RecordingHandler(
            Item("app-root", "app"),
            RecordingHandler.Json("{}", HttpStatusCode.Conflict),
            Item("claim-id", marker, "\"tag\"", Now.AddMinutes(-2)),
            Item("completed-id", "completed"));
        var service = CreateService(handler);

        var claim = await service.TryAcquireAsync(
            "agent-token", "same-message", Now, CancellationToken.None);

        Assert.Equal(EmailClaimDisposition.Duplicate, claim.Disposition);
        Assert.Equal(4, handler.Requests.Count);
        Assert.Single(handler.Requests, request => request.Method == HttpMethod.Post);
    }

    [Fact]
    public async Task StalePendingClaimIsConditionallyRemovedAndReacquired()
    {
        var marker = MarkerName("stale-message");
        var handler = new RecordingHandler(
            Item("app-root", "app"),
            RecordingHandler.Json("{}", HttpStatusCode.Conflict),
            Item("old-claim", marker, "\"old-tag\"", Now.AddHours(-1)),
            RecordingHandler.Json("{}", HttpStatusCode.NotFound),
            new HttpResponseMessage(HttpStatusCode.NoContent),
            Item("new-claim", marker, "\"new-tag\"", Now));
        var service = CreateService(handler);

        var claim = await service.TryAcquireAsync(
            "agent-token", "stale-message", Now, CancellationToken.None);

        Assert.Equal(EmailClaimDisposition.Acquired, claim.Disposition);
        Assert.Equal("new-claim", claim.MarkerId);
        var deletion = Assert.Single(handler.Requests,
            request => request.Method == HttpMethod.Delete);
        Assert.Equal("\"old-tag\"", deletion.Headers["If-Match"]);
    }

    [Fact]
    public async Task LosingStaleClaimRecoveryIsSuppressedAcrossContainers()
    {
        var marker = MarkerName("stale-race");
        var handler = new RecordingHandler(
            Item("app-root", "app"),
            RecordingHandler.Json("{}", HttpStatusCode.Conflict),
            Item("old-claim", marker, "\"old-tag\"", Now.AddHours(-1)),
            RecordingHandler.Json("{}", HttpStatusCode.NotFound),
            new HttpResponseMessage(HttpStatusCode.NoContent),
            RecordingHandler.Json("{}", HttpStatusCode.Conflict));
        var service = CreateService(handler);

        var claim = await service.TryAcquireAsync(
            "agent-token", "stale-race", Now, CancellationToken.None);

        Assert.Equal(EmailClaimDisposition.Duplicate, claim.Disposition);
    }

    [Fact]
    public async Task ConflictFollowedByReadFailureFailsClosedAsDuplicate()
    {
        var handler = new RecordingHandler(
            Item("app-root", "app"),
            RecordingHandler.Json("{}", HttpStatusCode.Conflict),
            RecordingHandler.Json("{}", HttpStatusCode.ServiceUnavailable));
        var service = CreateService(handler);

        var claim = await service.TryAcquireAsync(
            "agent-token", "conflicted-message", Now, CancellationToken.None);

        Assert.Equal(EmailClaimDisposition.Duplicate, claim.Disposition);
    }

    [Fact]
    public async Task AmbiguousInitialCreateFailsClosedAsDuplicate()
    {
        var handler = new RecordingHandler(
            Item("app-root", "app"),
            RecordingHandler.Json("{}", HttpStatusCode.InternalServerError),
            RecordingHandler.Json("{}", HttpStatusCode.NotFound));
        var service = CreateService(handler);

        var claim = await service.TryAcquireAsync(
            "agent-token", "ambiguous-message", Now, CancellationToken.None);

        Assert.Equal(EmailClaimDisposition.Duplicate, claim.Disposition);
    }

    [Fact]
    public async Task CompletionIsIdempotentAndReleaseUsesClaimEtag()
    {
        var completionHandler = new RecordingHandler(
            RecordingHandler.Json("{}", HttpStatusCode.Conflict));
        var completionService = CreateService(completionHandler);
        var claim = new EmailDeduplicationClaim(
            EmailClaimDisposition.Acquired, "claim-id", "\"claim-tag\"", "app-root");

        var completed = await completionService.CompleteAsync(
            "agent-token", claim, CancellationToken.None);

        Assert.True(completed);
        Assert.Equal(HttpMethod.Post, completionHandler.Requests[0].Method);
        Assert.Contains("/claim-id/children", completionHandler.Requests[0].Uri.AbsoluteUri);

        var releaseHandler = new RecordingHandler(
            new HttpResponseMessage(HttpStatusCode.NoContent));
        var releaseService = CreateService(releaseHandler);
        await releaseService.ReleaseAsync("agent-token", claim, CancellationToken.None);

        var deletion = Assert.Single(releaseHandler.Requests);
        Assert.Equal(HttpMethod.Delete, deletion.Method);
        Assert.Equal("\"claim-tag\"", deletion.Headers["If-Match"]);
    }

    [Fact]
    public async Task CompletionRetriesAServiceFailureBecauseTheMarkerNameIsIdempotent()
    {
        var handler = new RecordingHandler(
            RecordingHandler.Json("{}", HttpStatusCode.InternalServerError),
            RecordingHandler.Json("{}", HttpStatusCode.NotFound),
            Item("completed-id", "completed"));
        var service = CreateService(handler);
        var claim = new EmailDeduplicationClaim(
            EmailClaimDisposition.Acquired, "claim-id", "\"claim-tag\"", "app-root");

        var completed = await service.CompleteAsync(
            "agent-token", claim, CancellationToken.None);

        Assert.True(completed);
        Assert.Equal(2, handler.Requests.Count(request => request.Method == HttpMethod.Post));
    }

    [Fact]
    public async Task CleanupDeletesOnlyExpiredOpaqueMarkersAndIsBounded()
    {
        var oldMarker = MarkerName("old-message");
        var recentMarker = MarkerName("recent-message");
        var listing = RecordingHandler.Json(JsonSerializer.Serialize(new
        {
            value = new object[]
            {
                new { id = "old-id", name = oldMarker, eTag = "\"old\"",
                    createdDateTime = Now.AddDays(-9).ToString("O") },
                new { id = "recent-id", name = recentMarker, eTag = "\"recent\"",
                    createdDateTime = Now.AddDays(-1).ToString("O") },
                new { id = "other-id", name = "unrelated", eTag = "\"other\"",
                    createdDateTime = Now.AddDays(-30).ToString("O") }
            }
        }));
        var handler = new RecordingHandler(
            listing,
            new HttpResponseMessage(HttpStatusCode.NoContent));
        var service = CreateService(handler);

        await service.CleanupAsync("agent-token", "app-root", Now, CancellationToken.None);

        Assert.Equal(2, handler.Requests.Count);
        Assert.Equal(HttpMethod.Get, handler.Requests[0].Method);
        Assert.Contains("$orderby=createdDateTime", handler.Requests[0].Uri.Query);
        var deletion = handler.Requests[1];
        Assert.Equal(HttpMethod.Delete, deletion.Method);
        Assert.Contains("/old-id", deletion.Uri.AbsolutePath);
        Assert.Equal("\"old\"", deletion.Headers["If-Match"]);
    }

    [Fact]
    public async Task GraphFailureDegradesToUnavailableWithoutLeakingResponseBody()
    {
        var handler = new RecordingHandler(RecordingHandler.Json(
            """{"error":{"message":"tenant secret detail"}}""",
            HttpStatusCode.Forbidden));
        var service = CreateService(handler);

        var claim = await service.TryAcquireAsync(
            "agent-token", "message", Now, CancellationToken.None);

        Assert.Equal(EmailClaimDisposition.Unavailable, claim.Disposition);
    }

    private static OneDriveEmailDeduplicationService CreateService(HttpMessageHandler handler) =>
        new(
            new FixedHttpClientFactory(handler),
            NullLogger<OneDriveEmailDeduplicationService>.Instance);

    private static HttpResponseMessage Item(
        string id,
        string name,
        string eTag = "\"tag\"",
        DateTimeOffset? created = null) => RecordingHandler.Json(JsonSerializer.Serialize(new
        {
            id,
            name,
            eTag,
            createdDateTime = (created ?? Now).ToString("O")
        }));

    private static string MarkerName(string messageId)
    {
        var hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(messageId)))
            .ToLowerInvariant();
        return $"email-dedupe-{hash}";
    }
}
