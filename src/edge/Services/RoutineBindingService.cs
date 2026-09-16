namespace HelloWorldA365.Services;

using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Azure.Core;
using Azure.Identity;
using HelloWorldA365.Models;

/// <summary>
/// Creates and verifies the authority carried by a Foundry Routine.
///
/// Foundry's Routine action identifies the shared Hosted Agent, not the Agent 365 instance that
/// asked for the routine. We therefore create the action only during a trusted owner Activity turn
/// and sign the COMPLETE immutable instruction together with that turn's Agent Identity/User pair.
/// The signature is produced by this service; neither the administrator nor the model handles key
/// material or supplies identity IDs.
/// </summary>
public sealed class RoutineBindingService(
    IConfiguration configuration,
    IHttpClientFactory httpClientFactory,
    IHttpContextAccessor httpContextAccessor,
    ILogger<RoutineBindingService> logger,
    TokenCredential? credential = null)
{
    public const string InvocationPrefix = "hermes-routine:v1:";
    public const string CapabilityPrefix = "hermes-routine-capability:v1:";
    public const string AgentUserAuthorizationMode = "entra-agent-user-obo";
    private const string PreviewHeader = "Routines=V1Preview";
    private const string ApiVersion = "v1";
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);

    private readonly byte[] _signingKey = LoadSigningKey(configuration);
    private readonly TokenCredential _credential = credential ?? new DefaultAzureCredential();
    private readonly string _projectEndpoint = (
        Environment.GetEnvironmentVariable("FOUNDRY_PROJECT_ENDPOINT")
        ?? Environment.GetEnvironmentVariable("AZURE_AI_PROJECT_ENDPOINT")
        ?? configuration["FoundryProjectEndpoint"]
        ?? "").TrimEnd('/');
    private readonly string _agentName =
        Environment.GetEnvironmentVariable("FOUNDRY_AGENT_NAME")
        ?? Environment.GetEnvironmentVariable("AZURE_AI_AGENT_NAME")
        ?? configuration["FoundryAgentName"]
        ?? "";
    private readonly string _configuredBlueprint = configuration["AgentBlueprintId"] ?? "";
    private readonly string _configuredTenant = configuration["AgentTenantId"] ?? "";

    public string? MintOwnerCapability(
        AgentMetadata agent,
        string callerTier,
        string? ownerObjectId,
        string? ownerUpn)
    {
        if (!string.Equals(callerTier, "owner", StringComparison.OrdinalIgnoreCase))
            return null;

        var now = DateTimeOffset.UtcNow;
        var capability = new RoutineCreationCapability
        {
            Type = "routine-create",
            TenantId = agent.TenantId.ToString(),
            BlueprintId = agent.AgentApplicationId.ToString(),
            AgentIdentityId = agent.AgentId.ToString(),
            AgentUserId = agent.UserId.ToString(),
            AgentUpn = agent.EmailId,
            OwnerObjectId = ownerObjectId ?? "",
            OwnerUpn = ownerUpn ?? "",
            // Container protocol 2.0 requires the opaque per-request call ID to be forwarded on
            // outbound calls to Foundry services. Keep it inside the signed, short-lived owner
            // capability: the model and sidecar never see or choose the value.
            FoundryCallId = ReadFoundryCallId(httpContextAccessor.HttpContext?.Request),
            IssuedAt = now.ToUnixTimeSeconds(),
            ExpiresAt = now.AddMinutes(15).ToUnixTimeSeconds(),
            Nonce = Guid.NewGuid().ToString("N")
        };
        return CapabilityPrefix + Sign(capability);
    }

    public bool TryReadInvocation(
        string input,
        out RoutineInvocationBinding binding,
        out string error)
    {
        binding = new RoutineInvocationBinding();
        if (string.IsNullOrWhiteSpace(input) || !input.StartsWith(InvocationPrefix, StringComparison.Ordinal))
        {
            error = "The invocation is not an automatically bound Hermes Routine.";
            return false;
        }

        if (!TryVerify(input[InvocationPrefix.Length..], out binding, out error))
            return false;
        if (!string.Equals(binding.Type, "routine-invocation", StringComparison.Ordinal))
        {
            error = "The signed object is not a routine invocation.";
            return false;
        }
        if (!ValidateConfiguredParent(binding.TenantId, binding.BlueprintId, out error))
            return false;
        if (!Guid.TryParse(binding.AgentIdentityId, out _)
            || !Guid.TryParse(binding.AgentUserId, out _)
            || string.IsNullOrWhiteSpace(binding.Task)
            || string.IsNullOrWhiteSpace(binding.RoutineName))
        {
            error = "The signed routine binding is incomplete.";
            return false;
        }
        if (!string.Equals(binding.AuthorizationMode, AgentUserAuthorizationMode,
                StringComparison.Ordinal))
        {
            error = "The signed routine does not require the supported Agent User OBO path.";
            return false;
        }

        error = "";
        return true;
    }

    public async Task<RoutineControlResult> CreateAsync(
        string capabilityToken,
        CreateRoutineRequest request,
        CancellationToken cancellationToken)
    {
        if (!TryReadCapability(capabilityToken, out var capability, out var capabilityError))
            return RoutineControlResult.Failure(capabilityError);
        if (!ValidateCreateRequest(request, out var validationError))
            return RoutineControlResult.Failure(validationError);
        if (!EnsureFoundryConfigured(out var configurationError))
            return RoutineControlResult.Failure(configurationError);

        var internalName = InternalName(capability.AgentIdentityId, request.Name);
        var binding = new RoutineInvocationBinding
        {
            Type = "routine-invocation",
            TenantId = capability.TenantId,
            BlueprintId = capability.BlueprintId,
            AgentIdentityId = capability.AgentIdentityId,
            AgentUserId = capability.AgentUserId,
            AgentUpn = capability.AgentUpn,
            OwnerObjectId = capability.OwnerObjectId,
            OwnerUpn = capability.OwnerUpn,
            AuthorizationMode = AgentUserAuthorizationMode,
            RoutineName = internalName,
            DisplayName = request.Name.Trim(),
            Task = request.Task.Trim(),
            CronExpression = request.CronExpression.Trim(),
            TimeZone = request.TimeZone.Trim(),
            CreatedAt = DateTimeOffset.UtcNow.ToUnixTimeSeconds()
        };
        var signedInput = InvocationPrefix + Sign(binding);

        var body = new
        {
            description = $"Hermes routine: {binding.DisplayName}",
            enabled = true,
            triggers = new Dictionary<string, object>
            {
                ["schedule"] = new
                {
                    type = "schedule",
                    cron_expression = binding.CronExpression,
                    time_zone = binding.TimeZone
                }
            },
            action = new
            {
                type = "invoke_agent_responses_api",
                agent_name = _agentName,
                input = signedInput
            }
        };

        using var response = await SendFoundryAsync(
            HttpMethod.Put,
            $"routines/{Uri.EscapeDataString(internalName)}",
            JsonSerializer.Serialize(body, JsonOptions),
            capability.FoundryCallId,
            cancellationToken);
        var responseBody = await response.Content.ReadAsStringAsync(cancellationToken);
        if (!response.IsSuccessStatusCode)
        {
            logger.LogError("Foundry routine creation failed with HTTP {Status}",
                (int)response.StatusCode);
            return RoutineControlResult.Failure(
                $"Foundry rejected the routine with HTTP {(int)response.StatusCode}: {SafeServiceError(responseBody)}");
        }

        logger.LogInformation("Created an automatically instance-bound routine");
        return RoutineControlResult.Success(
            $"Routine '{binding.DisplayName}' was created and enabled. "
            + $"Schedule: {binding.CronExpression} ({binding.TimeZone}).",
            internalName);
    }

    public async Task<RoutineControlResult> ListAsync(
        string capabilityToken,
        CancellationToken cancellationToken)
    {
        if (!TryReadCapability(capabilityToken, out var capability, out var capabilityError))
            return RoutineControlResult.Failure(capabilityError);
        if (!EnsureFoundryConfigured(out var configurationError))
            return RoutineControlResult.Failure(configurationError);

        using var response = await SendFoundryAsync(
            HttpMethod.Get, "routines", null, capability.FoundryCallId, cancellationToken);
        var responseBody = await response.Content.ReadAsStringAsync(cancellationToken);
        if (!response.IsSuccessStatusCode)
            return RoutineControlResult.Failure(
                $"Foundry could not list routines (HTTP {(int)response.StatusCode}): {SafeServiceError(responseBody)}");

        var owned = new List<object>();
        try
        {
            using var doc = JsonDocument.Parse(responseBody);
            var root = doc.RootElement;
            var values = root.ValueKind == JsonValueKind.Array
                ? root.EnumerateArray()
                : root.TryGetProperty("data", out var data) && data.ValueKind == JsonValueKind.Array
                    ? data.EnumerateArray()
                    : root.TryGetProperty("value", out var value) && value.ValueKind == JsonValueKind.Array
                    ? value.EnumerateArray()
                    : Enumerable.Empty<JsonElement>();
            foreach (var routine in values)
            {
                if (!TryGetActionInput(routine, out var actionInput)
                    || !TryReadInvocation(actionInput, out var binding, out _)
                    || !string.Equals(binding.AgentIdentityId, capability.AgentIdentityId,
                        StringComparison.OrdinalIgnoreCase))
                    continue;

                var enabled = routine.TryGetProperty("enabled", out var enabledElement)
                    && enabledElement.ValueKind == JsonValueKind.True;
                owned.Add(new
                {
                    name = binding.DisplayName,
                    enabled,
                    cron_expression = binding.CronExpression,
                    time_zone = binding.TimeZone,
                    task = binding.Task
                });
            }
        }
        catch (JsonException ex)
        {
            logger.LogError("Foundry returned an unreadable routine list; type={ExceptionType}",
                ex.GetType().Name);
            return RoutineControlResult.Failure("Foundry returned an unreadable routine list.");
        }

        return RoutineControlResult.Success(
            owned.Count == 0
                ? "This Hermes instance has no routines."
                : JsonSerializer.Serialize(owned, JsonOptions));
    }

    public async Task<RoutineControlResult> DeleteAsync(
        string capabilityToken,
        string displayName,
        CancellationToken cancellationToken)
    {
        if (!TryReadCapability(capabilityToken, out var capability, out var capabilityError))
            return RoutineControlResult.Failure(capabilityError);
        if (string.IsNullOrWhiteSpace(displayName))
            return RoutineControlResult.Failure("Routine name is required.");
        if (!EnsureFoundryConfigured(out var configurationError))
            return RoutineControlResult.Failure(configurationError);

        var internalName = InternalName(capability.AgentIdentityId, displayName);
        using var get = await SendFoundryAsync(
            HttpMethod.Get, $"routines/{Uri.EscapeDataString(internalName)}", null,
            capability.FoundryCallId, cancellationToken);
        var body = await get.Content.ReadAsStringAsync(cancellationToken);
        if (get.StatusCode == System.Net.HttpStatusCode.NotFound)
            return RoutineControlResult.Failure($"No routine named '{displayName.Trim()}' belongs to this Hermes instance.");
        if (!get.IsSuccessStatusCode)
            return RoutineControlResult.Failure(
                $"Foundry could not inspect the routine (HTTP {(int)get.StatusCode}): {SafeServiceError(body)}");

        try
        {
            using var doc = JsonDocument.Parse(body);
            if (!TryGetActionInput(doc.RootElement, out var actionInput)
                || !TryReadInvocation(actionInput, out var binding, out _)
                || !string.Equals(binding.AgentIdentityId, capability.AgentIdentityId,
                    StringComparison.OrdinalIgnoreCase))
                return RoutineControlResult.Failure("That routine is not owned by this Hermes instance.");
        }
        catch (JsonException)
        {
            return RoutineControlResult.Failure("Foundry returned an unreadable routine definition.");
        }

        using var delete = await SendFoundryAsync(
            HttpMethod.Delete, $"routines/{Uri.EscapeDataString(internalName)}", null,
            capability.FoundryCallId, cancellationToken);
        var deleteBody = await delete.Content.ReadAsStringAsync(cancellationToken);
        if (!delete.IsSuccessStatusCode)
            return RoutineControlResult.Failure(
                $"Foundry could not remove the routine (HTTP {(int)delete.StatusCode}): {SafeServiceError(deleteBody)}");

        logger.LogInformation("Removed an instance-bound routine");
        return RoutineControlResult.Success($"Routine '{displayName.Trim()}' was removed.");
    }

    private bool TryReadCapability(
        string token,
        out RoutineCreationCapability capability,
        out string error)
    {
        capability = new RoutineCreationCapability();
        if (string.IsNullOrWhiteSpace(token)
            || !token.StartsWith(CapabilityPrefix, StringComparison.Ordinal))
        {
            error = "Routine management is available only during a verified owner conversation.";
            return false;
        }
        if (!TryVerify(token[CapabilityPrefix.Length..], out capability, out error))
            return false;
        if (!string.Equals(capability.Type, "routine-create", StringComparison.Ordinal))
        {
            error = "The supplied authority is not a routine-management capability.";
            return false;
        }
        if (capability.ExpiresAt < DateTimeOffset.UtcNow.ToUnixTimeSeconds())
        {
            error = "The owner authorization for routine management expired. Ask again in the owner conversation.";
            return false;
        }
        if (!ValidateConfiguredParent(capability.TenantId, capability.BlueprintId, out error))
            return false;
        if (!Guid.TryParse(capability.AgentIdentityId, out _)
            || !Guid.TryParse(capability.AgentUserId, out _))
        {
            error = "The owner authorization does not identify a valid Agent Identity/User pair.";
            return false;
        }
        error = "";
        return true;
    }

    private bool ValidateConfiguredParent(string tenantId, string blueprintId, out string error)
    {
        if (!string.IsNullOrWhiteSpace(_configuredTenant)
            && !string.Equals(_configuredTenant, tenantId, StringComparison.OrdinalIgnoreCase))
        {
            error = "The routine belongs to a different tenant.";
            return false;
        }
        if (!string.IsNullOrWhiteSpace(_configuredBlueprint)
            && !string.Equals(_configuredBlueprint, blueprintId, StringComparison.OrdinalIgnoreCase))
        {
            error = "The routine belongs to a different Agent Identity Blueprint.";
            return false;
        }
        error = "";
        return true;
    }

    private bool EnsureFoundryConfigured(out string error)
    {
        if (string.IsNullOrWhiteSpace(_projectEndpoint))
        {
            error = "The Foundry project endpoint is not available in this hosted runtime.";
            return false;
        }
        if (string.IsNullOrWhiteSpace(_agentName))
        {
            error = "The Foundry agent name is not available in this hosted runtime.";
            return false;
        }
        error = "";
        return true;
    }

    private static bool ValidateCreateRequest(CreateRoutineRequest request, out string error)
    {
        if (string.IsNullOrWhiteSpace(request.Name) || request.Name.Trim().Length > 80)
            error = "Routine name is required and must be at most 80 characters.";
        else if (string.IsNullOrWhiteSpace(request.Task) || request.Task.Trim().Length > 8_000)
            error = "Routine task is required and must be at most 8,000 characters.";
        else if (string.IsNullOrWhiteSpace(request.CronExpression) || request.CronExpression.Trim().Length > 100)
            error = "A cron expression is required.";
        else if (string.IsNullOrWhiteSpace(request.TimeZone) || request.TimeZone.Trim().Length > 100)
            error = "An IANA time zone is required.";
        else
        {
            error = "";
            return true;
        }
        return false;
    }

    private async Task<HttpResponseMessage> SendFoundryAsync(
        HttpMethod method,
        string relativePath,
        string? json,
        string? foundryCallId,
        CancellationToken cancellationToken)
    {
        var token = await _credential.GetTokenAsync(
            new TokenRequestContext(["https://ai.azure.com/.default"]), cancellationToken);
        LogFoundryCaller(token.Token, foundryCallId);
        var client = httpClientFactory.CreateClient();
        client.Timeout = TimeSpan.FromSeconds(60);
        var querySeparator = relativePath.Contains('?') ? '&' : '?';
        using var request = new HttpRequestMessage(
            method,
            $"{_projectEndpoint}/{relativePath}{querySeparator}api-version={Uri.EscapeDataString(ApiVersion)}");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token.Token);
        request.Headers.TryAddWithoutValidation("Foundry-Features", PreviewHeader);
        if (!string.IsNullOrWhiteSpace(foundryCallId))
            request.Headers.TryAddWithoutValidation("x-agent-foundry-call-id", foundryCallId);
        if (json is not null)
            request.Content = new StringContent(json, Encoding.UTF8, "application/json");
        return await client.SendAsync(request, cancellationToken);
    }

    private void LogFoundryCaller(string accessToken, string? foundryCallId)
    {
        // Never log the bearer token or raw directory claims. Stable one-way tags are enough to
        // distinguish the ambient hosted identity, Agent Identity and blueprint during diagnosis.
        try
        {
            var parts = accessToken.Split('.');
            if (parts.Length < 2 || !TryB64Url(parts[1], out var payload))
            {
                logger.LogWarning(
                    "Foundry routine caller token is not a readable JWT; call-id present={FoundryCallIdPresent}",
                    !string.IsNullOrWhiteSpace(foundryCallId));
                return;
            }

            using var document = JsonDocument.Parse(payload);
            var claims = document.RootElement;
            logger.LogWarning(
                "Foundry routine caller: oid_tag={ObjectIdTag}, appid_tag={ApplicationIdTag}, azp_tag={AuthorizedPartyTag}, "
                + "idtyp={IdentityType}, call-id present={FoundryCallIdPresent}",
                ClaimTag(claims, "oid"),
                ClaimTag(claims, "appid"),
                ClaimTag(claims, "azp"),
                Claim(claims, "idtyp"),
                !string.IsNullOrWhiteSpace(foundryCallId));
        }
        catch (Exception ex) when (ex is JsonException or FormatException)
        {
            logger.LogWarning(
                "Could not decode Foundry routine caller claims ({ErrorType}); call-id present={FoundryCallIdPresent}",
                ex.GetType().Name,
                !string.IsNullOrWhiteSpace(foundryCallId));
        }
    }

    private static string Claim(JsonElement claims, string name) =>
        claims.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? ""
            : "";

    private static string ClaimTag(JsonElement claims, string name)
    {
        var value = Claim(claims, name);
        if (string.IsNullOrWhiteSpace(value)) return "none";
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value)))
            .ToLowerInvariant()[..12];
    }

    private static string ReadFoundryCallId(HttpRequest? request)
    {
        var value = request?.Headers["x-agent-foundry-call-id"].ToString().Trim() ?? "";
        // This is an opaque platform handle, not user/model input. Still reject malformed or
        // unreasonable header content before copying it to another request.
        return value.Length is > 0 and <= 4096 && !value.Contains('\r') && !value.Contains('\n')
            ? value
            : "";
    }

    private string Sign<T>(T value)
    {
        var payload = JsonSerializer.SerializeToUtf8Bytes(value, JsonOptions);
        using var hmac = new HMACSHA256(_signingKey);
        return B64Url(payload) + "." + B64Url(hmac.ComputeHash(payload));
    }

    private bool TryVerify<T>(string token, out T value, out string error) where T : new()
    {
        value = new T();
        var parts = token.Split('.');
        if (parts.Length != 2 || !TryB64Url(parts[0], out var payload) || !TryB64Url(parts[1], out var signature))
        {
            error = "The routine authority has an invalid format.";
            return false;
        }
        // Convert.FromBase64String accepts non-zero unused trailing bits, so multiple textual
        // spellings can decode to the same signed bytes. Require our canonical unpadded base64url
        // encoding before comparing the MAC; capabilities then have exactly one representation.
        if (!string.Equals(parts[0], B64Url(payload), StringComparison.Ordinal)
            || !string.Equals(parts[1], B64Url(signature), StringComparison.Ordinal))
        {
            error = "The routine authority has a non-canonical format.";
            return false;
        }
        using var hmac = new HMACSHA256(_signingKey);
        var expected = hmac.ComputeHash(payload);
        if (signature.Length != expected.Length
            || !CryptographicOperations.FixedTimeEquals(signature, expected))
        {
            error = "The routine authority signature is invalid.";
            return false;
        }
        try
        {
            value = JsonSerializer.Deserialize<T>(payload, JsonOptions) ?? new T();
            error = "";
            return true;
        }
        catch (JsonException)
        {
            error = "The signed routine authority is unreadable.";
            return false;
        }
    }

    private static bool TryGetActionInput(JsonElement routine, out string input)
    {
        input = "";
        if (!routine.TryGetProperty("action", out var action)
            || !action.TryGetProperty("input", out var inputElement))
            return false;
        input = inputElement.ValueKind == JsonValueKind.String
            ? inputElement.GetString() ?? ""
            : inputElement.GetRawText().Trim('"');
        return input.Length > 0;
    }

    private static string InternalName(string agentIdentityId, string displayName)
    {
        var identityBytes = SHA256.HashData(Encoding.UTF8.GetBytes(agentIdentityId.ToLowerInvariant()));
        var identityPrefix = Convert.ToHexString(identityBytes)[..12].ToLowerInvariant();
        var slug = new string(displayName.Trim().ToLowerInvariant()
            .Select(c => char.IsAsciiLetterOrDigit(c) ? c : '-')
            .ToArray());
        while (slug.Contains("--", StringComparison.Ordinal)) slug = slug.Replace("--", "-", StringComparison.Ordinal);
        slug = slug.Trim('-');
        if (slug.Length == 0) slug = "routine";
        if (slug.Length > 60) slug = slug[..60].TrimEnd('-');
        return $"hr-{identityPrefix}-{slug}";
    }

    private static byte[] LoadSigningKey(IConfiguration configuration)
    {
        var configured = configuration["RoutineBindingSignKey"] ?? configuration["IdentitySignKey"] ?? "";
        if (configured.Length < 32)
            throw new InvalidOperationException(
                "RoutineBindingSignKey (or IdentitySignKey fallback) must contain at least 32 characters.");
        return Encoding.UTF8.GetBytes(configured);
    }

    private static string B64Url(byte[] value) =>
        Convert.ToBase64String(value).TrimEnd('=').Replace('+', '-').Replace('/', '_');

    private static bool TryB64Url(string value, out byte[] bytes)
    {
        try
        {
            var padded = value.Replace('-', '+').Replace('_', '/');
            padded += new string('=', (4 - padded.Length % 4) % 4);
            bytes = Convert.FromBase64String(padded);
            return true;
        }
        catch (FormatException)
        {
            bytes = [];
            return false;
        }
    }

    private static string SafeServiceError(string body)
    {
        if (string.IsNullOrWhiteSpace(body)) return "no response body";
        try
        {
            using var doc = JsonDocument.Parse(body);
            if (doc.RootElement.TryGetProperty("error", out var error))
            {
                if (error.ValueKind == JsonValueKind.String) return Truncate(error.GetString() ?? "error");
                if (error.TryGetProperty("message", out var message)) return Truncate(message.GetString() ?? "error");
            }
        }
        catch (JsonException) { }
        return Truncate(body);
    }

    private static string Truncate(string value) =>
        value.Length <= 500 ? value : value[..500];
}

public sealed class CreateRoutineRequest
{
    public string Name { get; set; } = "";
    public string Task { get; set; } = "";
    public string CronExpression { get; set; } = "";
    public string TimeZone { get; set; } = "UTC";
}

public sealed class RoutineCreationCapability
{
    [JsonPropertyName("typ")] public string Type { get; set; } = "";
    [JsonPropertyName("tid")] public string TenantId { get; set; } = "";
    [JsonPropertyName("blueprint")] public string BlueprintId { get; set; } = "";
    [JsonPropertyName("agent_identity")] public string AgentIdentityId { get; set; } = "";
    [JsonPropertyName("agent_user")] public string AgentUserId { get; set; } = "";
    [JsonPropertyName("agent_upn")] public string AgentUpn { get; set; } = "";
    [JsonPropertyName("owner_oid")] public string OwnerObjectId { get; set; } = "";
    [JsonPropertyName("owner_upn")] public string OwnerUpn { get; set; } = "";
    [JsonPropertyName("foundry_call_id")] public string FoundryCallId { get; set; } = "";
    [JsonPropertyName("iat")] public long IssuedAt { get; set; }
    [JsonPropertyName("exp")] public long ExpiresAt { get; set; }
    [JsonPropertyName("nonce")] public string Nonce { get; set; } = "";
}

public sealed class RoutineInvocationBinding
{
    [JsonPropertyName("typ")] public string Type { get; set; } = "";
    [JsonPropertyName("tid")] public string TenantId { get; set; } = "";
    [JsonPropertyName("blueprint")] public string BlueprintId { get; set; } = "";
    [JsonPropertyName("agent_identity")] public string AgentIdentityId { get; set; } = "";
    [JsonPropertyName("agent_user")] public string AgentUserId { get; set; } = "";
    [JsonPropertyName("agent_upn")] public string AgentUpn { get; set; } = "";
    [JsonPropertyName("owner_oid")] public string OwnerObjectId { get; set; } = "";
    [JsonPropertyName("owner_upn")] public string OwnerUpn { get; set; } = "";
    // This is a signed policy selector, never an OAuth token or credential. The Responses handler
    // must mint a fresh downstream token through Microsoft.Identity.Web for every execution.
    [JsonPropertyName("auth")] public string AuthorizationMode { get; set; } = "";
    [JsonPropertyName("routine")] public string RoutineName { get; set; } = "";
    [JsonPropertyName("name")] public string DisplayName { get; set; } = "";
    [JsonPropertyName("task")] public string Task { get; set; } = "";
    [JsonPropertyName("cron")] public string CronExpression { get; set; } = "";
    [JsonPropertyName("time_zone")] public string TimeZone { get; set; } = "UTC";
    [JsonPropertyName("created_at")] public long CreatedAt { get; set; }
}

public sealed class RoutineControlResult
{
    public bool Ok { get; init; }
    public string Message { get; init; } = "";
    public string? InternalName { get; init; }

    public static RoutineControlResult Success(string message, string? internalName = null) =>
        new() { Ok = true, Message = message, InternalName = internalName };
    public static RoutineControlResult Failure(string message) =>
        new() { Ok = false, Message = message };
}
