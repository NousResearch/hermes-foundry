namespace HelloWorldA365.Services;

using System.Globalization;
using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

public enum EmailClaimDisposition
{
    Acquired,
    Duplicate,
    Unavailable
}

public sealed record EmailDeduplicationClaim(
    EmailClaimDisposition Disposition,
    string MarkerId = "",
    string ETag = "",
    string AppRootId = "");

/// <summary>
/// Claims an Agent User email notification in that Agent User's OneDrive app folder.
///
/// Foundry can redeliver a notification in another hosted session, so process memory and the
/// session-scoped /files filesystem cannot provide agent-wide deduplication. Folder creation with
/// conflictBehavior=fail is the atomic compare-and-create primitive: exactly one session creates
/// the marker; every other session observes the name conflict. A child named <c>completed</c>
/// distinguishes a sent/sealed notification from an abandoned claim. Pending claims may be taken
/// over after a bounded lease, allowing recovery after a container dies mid-turn.
/// </summary>
public sealed class OneDriveEmailDeduplicationService(
    IHttpClientFactory httpClientFactory,
    ILogger<OneDriveEmailDeduplicationService> logger)
{
    private const string Graph = "https://graph.microsoft.com/v1.0";
    private const string MarkerPrefix = "email-dedupe-";
    private static readonly TimeSpan PendingLease = TimeSpan.FromMinutes(30);
    private static readonly TimeSpan GraphRequestTimeout = TimeSpan.FromSeconds(8);
    private const int RetentionDays = 8;
    private readonly SemaphoreSlim _cleanupLock = new(1, 1);
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, DateOnly>
        _lastCleanupDates = new(StringComparer.Ordinal);

    public async Task<EmailDeduplicationClaim> TryAcquireAsync(
        string accessToken,
        string messageId,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(accessToken) || string.IsNullOrWhiteSpace(messageId))
            return new(EmailClaimDisposition.Unavailable);

        var observedConflict = false;
        try
        {
            var appRoot = await GetItemAsync(
                accessToken,
                "me/drive/special/approot?$select=id,eTag,createdDateTime,name",
                missingIsNormal: false,
                cancellationToken) ?? throw new InvalidOperationException("OneDrive app root is absent.");

            var hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(messageId)))
                .ToLowerInvariant();

            // The marker name is stable for this message throughout its retention window. This
            // covers delayed redelivery as well as retries that cross a UTC date boundary.
            var markerName = MarkerName(hash);
            var created = await CreateFolderAsync(
                accessToken, appRoot.Id, markerName, cancellationToken);
            if (created.Item is not null)
                return new(
                    EmailClaimDisposition.Acquired,
                    created.Item.Id,
                    created.Item.ETag,
                    appRoot.Id);

            // An ambiguous atomic create may have committed. Never process the notification on a
            // second worker merely because its read-back also failed.
            if (created.Ambiguous)
                return new(EmailClaimDisposition.Duplicate);

            if (!created.Conflict)
                return new(EmailClaimDisposition.Unavailable);
            observedConflict = true;

            var existing = await GetChildAsync(
                accessToken, appRoot.Id, markerName, cancellationToken);
            if (existing is null)
                return new(EmailClaimDisposition.Duplicate);

            var state = await ClassifyExistingAsync(
                accessToken, existing, now, cancellationToken);
            if (state == ExistingClaim.Completed || state == ExistingClaim.Pending)
                return new(EmailClaimDisposition.Duplicate);

            // A worker died without sealing or releasing the claim. Delete only the exact version
            // inspected above, then make one bounded attempt to acquire it again.
            if (!await DeleteItemAsync(accessToken, existing, cancellationToken))
                return new(EmailClaimDisposition.Duplicate);
            var replacement = await CreateFolderAsync(
                accessToken, appRoot.Id, markerName, cancellationToken);
            if (replacement.Item is not null)
                return new(
                    EmailClaimDisposition.Acquired,
                    replacement.Item.Id,
                    replacement.Item.ETag,
                    appRoot.Id);
            return replacement.Conflict || replacement.Ambiguous
                ? new(EmailClaimDisposition.Duplicate)
                : new(EmailClaimDisposition.Unavailable);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex)
        {
            logger.LogWarning(
                "Durable email claim resolution failed; failClosed={FailClosed} type={ExceptionType}",
                observedConflict,
                ex.GetType().Name);
            return new(observedConflict
                ? EmailClaimDisposition.Duplicate
                : EmailClaimDisposition.Unavailable);
        }
    }

    /// <summary>Seal the claim before sending the reply, preserving the existing at-most-once policy.</summary>
    public async Task<bool> CompleteAsync(
        string accessToken,
        EmailDeduplicationClaim claim,
        CancellationToken cancellationToken)
    {
        if (claim.Disposition != EmailClaimDisposition.Acquired
            || string.IsNullOrWhiteSpace(claim.MarkerId))
            return false;
        try
        {
            // This operation is safely repeatable: a conflict means another attempt already sealed
            // the same immutable marker and is therefore success, not a second side effect.
            for (var attempt = 0; attempt < 3; attempt++)
            {
                try
                {
                    var completion = await CreateFolderAsync(
                        accessToken, claim.MarkerId, "completed", cancellationToken);
                    if (completion.Item is not null || completion.Conflict)
                        return true;
                }
                catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
                {
                    throw;
                }
                catch (Exception ex)
                {
                    logger.LogWarning(
                        "Durable email completion attempt failed; attempt={Attempt} type={ExceptionType}",
                        attempt + 1,
                        ex.GetType().Name);
                }
                if (attempt < 2)
                    await Task.Delay(TimeSpan.FromSeconds(1 << attempt), cancellationToken);
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex)
        {
            logger.LogWarning(
                "Durable email claim could not be completed; type={ExceptionType}",
                ex.GetType().Name);
        }
        return false;
    }

    /// <summary>
    /// Remove a bounded batch of expired markers after the user-visible reply has been sent.
    /// Cleanup is deliberately outside <see cref="CompleteAsync"/> so mailbox maintenance cannot
    /// delay or prevent the reply whose claim was already sealed.
    /// </summary>
    public Task CleanupAsync(
        string accessToken,
        string appRootId,
        DateTimeOffset now,
        CancellationToken cancellationToken) =>
        TryCleanupAsync(accessToken, appRootId, now, cancellationToken);

    /// <summary>Release work that failed before any reply was attempted so a redelivery may retry.</summary>
    public async Task ReleaseAsync(
        string accessToken,
        EmailDeduplicationClaim claim,
        CancellationToken cancellationToken)
    {
        if (claim.Disposition != EmailClaimDisposition.Acquired
            || string.IsNullOrWhiteSpace(claim.MarkerId))
            return;
        try
        {
            await DeleteItemAsync(
                accessToken,
                new GraphItem(claim.MarkerId, claim.ETag, default, ""),
                cancellationToken);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex)
        {
            logger.LogWarning(
                "Durable email claim could not be released; type={ExceptionType}",
                ex.GetType().Name);
        }
    }

    private async Task<ExistingClaim> ClassifyExistingAsync(
        string accessToken,
        GraphItem marker,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        var completed = await GetChildAsync(
            accessToken, marker.Id, "completed", cancellationToken);
        if (completed is not null)
            return ExistingClaim.Completed;
        return marker.CreatedAt != default && now - marker.CreatedAt >= PendingLease
            ? ExistingClaim.Stale
            : ExistingClaim.Pending;
    }

    private async Task<CreateFolderResult> CreateFolderAsync(
        string accessToken,
        string parentId,
        string name,
        CancellationToken cancellationToken)
    {
        var body = JsonSerializer.Serialize(new Dictionary<string, object>
        {
            ["name"] = name,
            ["folder"] = new Dictionary<string, object>(),
            ["@microsoft.graph.conflictBehavior"] = "fail"
        });
        using var request = GraphRequest(
            HttpMethod.Post,
            $"me/drive/items/{Uri.EscapeDataString(parentId)}/children",
            accessToken,
            body);
        HttpResponseMessage response;
        try
        {
            response = await SendAsync(request, cancellationToken);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch
        {
            // A lost POST response has an uncertain outcome. Resolve the exact name instead of
            // replaying the create. If it exists, report a conflict so the caller treats it as an
            // existing claim; at-most-once delivery is safer than guessing that this worker won.
            try
            {
                if (await GetChildAsync(accessToken, parentId, name, cancellationToken) is not null)
                    return new(true, false, null);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch
            {
                return new(false, true, null);
            }
            return new(false, true, null);
        }
        using (response)
        {
            if (response.StatusCode == HttpStatusCode.Conflict)
                return new(true, false, null);
            if (!response.IsSuccessStatusCode)
            {
                // Some service failures are returned after the mutation committed. Perform the
                // same read-back used for transport ambiguity before declaring the operation down.
                if ((int)response.StatusCode >= 500)
                {
                    try
                    {
                        if (await GetChildAsync(
                                accessToken, parentId, name, cancellationToken) is not null)
                            return new(true, false, null);
                    }
                    catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
                    {
                        throw;
                    }
                    catch
                    {
                        return new(false, true, null);
                    }
                    return new(false, true, null);
                }
                throw new InvalidOperationException(
                    $"OneDrive folder creation returned HTTP {(int)response.StatusCode}.");
            }
            return new(false, false, await ParseItemAsync(response, cancellationToken));
        }
    }

    private Task<GraphItem?> GetChildAsync(
        string accessToken,
        string parentId,
        string name,
        CancellationToken cancellationToken) => GetItemAsync(
            accessToken,
            $"me/drive/items/{Uri.EscapeDataString(parentId)}:/{Uri.EscapeDataString(name)}"
                + "?$select=id,eTag,createdDateTime,name",
            missingIsNormal: true,
            cancellationToken);

    private async Task<GraphItem?> GetItemAsync(
        string accessToken,
        string relativePath,
        bool missingIsNormal,
        CancellationToken cancellationToken)
    {
        using var request = GraphRequest(HttpMethod.Get, relativePath, accessToken);
        using var response = await SendAsync(request, cancellationToken);
        if (missingIsNormal && response.StatusCode == HttpStatusCode.NotFound)
            return null;
        if (!response.IsSuccessStatusCode)
            throw new InvalidOperationException(
                $"OneDrive item lookup returned HTTP {(int)response.StatusCode}.");
        return await ParseItemAsync(response, cancellationToken);
    }

    private async Task<bool> DeleteItemAsync(
        string accessToken,
        GraphItem item,
        CancellationToken cancellationToken)
    {
        using var request = GraphRequest(
            HttpMethod.Delete,
            $"me/drive/items/{Uri.EscapeDataString(item.Id)}",
            accessToken);
        if (!string.IsNullOrWhiteSpace(item.ETag))
            request.Headers.TryAddWithoutValidation("If-Match", item.ETag);
        using var response = await SendAsync(request, cancellationToken);
        if (response.IsSuccessStatusCode || response.StatusCode == HttpStatusCode.NotFound)
            return true;
        if (response.StatusCode == HttpStatusCode.PreconditionFailed)
            return false;
        throw new InvalidOperationException(
            $"OneDrive marker deletion returned HTTP {(int)response.StatusCode}.");
    }

    private async Task TryCleanupAsync(
        string accessToken,
        string appRootId,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        var today = DateOnly.FromDateTime(now.UtcDateTime);
        if (string.IsNullOrWhiteSpace(appRootId)
            || (_lastCleanupDates.TryGetValue(appRootId, out var cleaned) && cleaned == today))
            return;
        await _cleanupLock.WaitAsync(cancellationToken);
        try
        {
            if (_lastCleanupDates.TryGetValue(appRootId, out cleaned) && cleaned == today)
                return;
            var cutoff = now.AddDays(-RetentionDays);
            // App root is dedicated to this application. Reading the oldest 200 children makes
            // cleanup bounded while ensuring an old backlog is drained before newer markers.
            using var request = GraphRequest(
                HttpMethod.Get,
                $"me/drive/items/{Uri.EscapeDataString(appRootId)}/children"
                    + "?$select=id,name,eTag,createdDateTime&$orderby=createdDateTime&$top=200",
                accessToken);
            using var response = await SendAsync(request, cancellationToken);
            if (!response.IsSuccessStatusCode)
                return;
            using var document = JsonDocument.Parse(
                await response.Content.ReadAsStringAsync(cancellationToken));
            if (document.RootElement.TryGetProperty("value", out var values)
                && values.ValueKind == JsonValueKind.Array)
            {
                foreach (var element in values.EnumerateArray())
                {
                    var item = ParseItem(element);
                    if (item is null
                        || !item.Name.StartsWith(MarkerPrefix, StringComparison.Ordinal)
                        || item.CreatedAt == default
                        || item.CreatedAt >= cutoff)
                        continue;
                    try
                    {
                        await DeleteItemAsync(accessToken, item, cancellationToken);
                    }
                    catch (Exception ex) when (ex is not OperationCanceledException)
                    {
                        logger.LogWarning(
                            "Expired email marker cleanup failed; type={ExceptionType}",
                            ex.GetType().Name);
                    }
                }
            }
            _lastCleanupDates[appRootId] = today;
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex)
        {
            logger.LogWarning(
                "Email marker cleanup is unavailable; type={ExceptionType}",
                ex.GetType().Name);
        }
        finally
        {
            _cleanupLock.Release();
        }
    }

    private async Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        using var client = httpClientFactory.CreateClient();
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(GraphRequestTimeout);
        try
        {
            return await client.SendAsync(request, timeout.Token);
        }
        catch (OperationCanceledException ex) when (!cancellationToken.IsCancellationRequested)
        {
            throw new TimeoutException("Microsoft Graph request timed out.", ex);
        }
    }

    private static HttpRequestMessage GraphRequest(
        HttpMethod method,
        string relativePath,
        string accessToken,
        string? json = null)
    {
        var request = new HttpRequestMessage(method, $"{Graph}/{relativePath}");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", accessToken);
        request.Headers.Accept.ParseAdd("application/json");
        if (json is not null)
            request.Content = new StringContent(json, Encoding.UTF8, "application/json");
        return request;
    }

    private static async Task<GraphItem> ParseItemAsync(
        HttpResponseMessage response,
        CancellationToken cancellationToken)
    {
        using var document = JsonDocument.Parse(
            await response.Content.ReadAsStringAsync(cancellationToken));
        return ParseItem(document.RootElement)
            ?? throw new InvalidOperationException("OneDrive returned an incomplete drive item.");
    }

    private static GraphItem? ParseItem(JsonElement element)
    {
        var id = element.TryGetProperty("id", out var idElement) ? idElement.GetString() : null;
        if (string.IsNullOrWhiteSpace(id))
            return null;
        var eTag = element.TryGetProperty("eTag", out var tagElement)
            ? tagElement.GetString() ?? ""
            : "";
        var name = element.TryGetProperty("name", out var nameElement)
            ? nameElement.GetString() ?? ""
            : "";
        var created = element.TryGetProperty("createdDateTime", out var createdElement)
            && DateTimeOffset.TryParse(
                createdElement.GetString(),
                CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal,
                out var parsed)
            ? parsed
            : default;
        return new(id, eTag, created, name);
    }

    private static string MarkerName(string hash) => $"{MarkerPrefix}{hash}";

    private enum ExistingClaim
    {
        Pending,
        Completed,
        Stale
    }

    private sealed record GraphItem(string Id, string ETag, DateTimeOffset CreatedAt, string Name);
    private sealed record CreateFolderResult(bool Conflict, bool Ambiguous, GraphItem? Item);
}
