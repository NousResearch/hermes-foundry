namespace HelloWorldA365.Services;

using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Azure.Core;
using Azure.Identity;

/// <summary>
/// Discovers the chat-capable model deployments that the Foundry project exposes, applies the
/// administrator's priority/allow-list policy, and stores a caller-scoped model preference.
///
/// The service deliberately does not manage or deploy models. Azure/Foundry administrators remain
/// the authority: a model is selectable only when it is returned by the project Deployments API (or
/// appears in the deployment-time snapshot of that same API). A Teams user can therefore choose
/// among models already made available to this Hermes deployment, but cannot bypass Azure policy.
/// </summary>
public sealed class ModelPolicyService
{
    private const string FoundryScope = "https://ai.azure.com/.default";
    private static readonly Regex SafeDeploymentName = new(
        "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
        RegexOptions.CultureInvariant | RegexOptions.Compiled);
    private static readonly Regex LeadingTeamsMention = new(
        "^\\s*(?:<at>[^<]{0,160}</at>\\s*)+",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant | RegexOptions.Compiled);

    private readonly IHttpClientFactory _httpFactory;
    private readonly ILogger<ModelPolicyService> _logger;
    private readonly TokenCredential _credential;
    private readonly SemaphoreSlim _inventoryLock = new(1, 1);
    private readonly SemaphoreSlim _preferencesLock = new(1, 1);
    private readonly string _projectEndpoint;
    private readonly string _configuredPrimary;
    private readonly IReadOnlyList<string> _priority;
    private readonly HashSet<string>? _allowList;
    private readonly IReadOnlyList<string> _snapshot;
    private readonly TimeSpan _cacheLifetime;
    private readonly string _preferencesPath;

    private ModelInventory? _cachedInventory;
    private DateTimeOffset _inventoryExpiresAt;
    private Dictionary<string, string>? _preferences;

    public ModelPolicyService(
        IConfiguration configuration,
        IHttpClientFactory httpFactory,
        ILogger<ModelPolicyService> logger)
        : this(configuration, httpFactory, logger, new DefaultAzureCredential())
    {
    }

    internal ModelPolicyService(
        IConfiguration configuration,
        IHttpClientFactory httpFactory,
        ILogger<ModelPolicyService> logger,
        TokenCredential credential)
    {
        _httpFactory = httpFactory;
        _logger = logger;
        _credential = credential;

        _configuredPrimary = CleanName(configuration["MODEL_DEPLOYMENT_NAME"])
            ?? "gpt-5.6-terra";
        var configuredFallback = CleanName(configuration["FALLBACK_MODEL_DEPLOYMENT_NAME"]);

        _priority = DistinctNames(ParseList(configuration["MODEL_PRIORITY"])
            .Concat([_configuredPrimary])
            .Concat(configuredFallback is null ? [] : [configuredFallback]));

        var allow = DistinctNames(ParseList(configuration["MODEL_ALLOWED_DEPLOYMENTS"]));
        _allowList = allow.Count == 0
            ? null
            : new HashSet<string>(allow, StringComparer.OrdinalIgnoreCase);

        var snapshot = DistinctNames(ParseList(configuration["MODEL_DEPLOYMENTS_SNAPSHOT"]));
        // Older deployed versions have no snapshot. Preserve their exact primary/fallback behavior,
        // but never mix configured names into a real API snapshot: if an administrator removed a
        // deployment, it must disappear from /model after the next version is created.
        if (snapshot.Count == 0)
        {
            var legacySnapshot = new List<string> { _configuredPrimary };
            if (configuredFallback is not null) legacySnapshot.Add(configuredFallback);
            snapshot = DistinctNames(legacySnapshot);
        }
        _snapshot = ApplyAllowList(snapshot);

        _projectEndpoint = ResolveProjectEndpoint(configuration);
        var cacheSeconds = int.TryParse(configuration["MODEL_DISCOVERY_CACHE_SECONDS"], out var parsed)
            ? Math.Clamp(parsed, 30, 3600)
            : 300;
        _cacheLifetime = TimeSpan.FromSeconds(cacheSeconds);

        var stateRoot = configuration["HERMES_STATE"];
        if (string.IsNullOrWhiteSpace(stateRoot)) stateRoot = "/files/hermes";
        _preferencesPath = Path.Combine(stateRoot, ".model-preferences.json");
    }

    /// <summary>Resolve the model that should execute a turn.</summary>
    public async Task<ModelSelection> ResolveAsync(
        string sessionKey,
        CancellationToken cancellationToken,
        bool forceRefresh = false)
    {
        var inventory = await GetInventoryAsync(cancellationToken, forceRefresh);
        var preference = await GetPreferenceAsync(sessionKey, cancellationToken);
        var explicitlySelected = preference is not null
            && inventory.Models.Contains(preference, StringComparer.OrdinalIgnoreCase);
        var selected = explicitlySelected
            ? inventory.Models.First(m => m.Equals(preference, StringComparison.OrdinalIgnoreCase))
            : ChooseAutomatic(inventory.Models);

        return new ModelSelection(
            selected,
            inventory.Models,
            _priority,
            explicitlySelected,
            inventory.Source,
            inventory.Warning);
    }

    /// <summary>
    /// Handle the Teams-facing model command. Unknown text is returned as <c>Handled=false</c> and
    /// continues through the ordinary Hermes turn.
    /// </summary>
    public async Task<ModelCommandResult> HandleCommandAsync(
        string? activityText,
        string sessionKey,
        CancellationToken cancellationToken)
    {
        var text = LeadingTeamsMention.Replace(activityText ?? "", "").Trim();
        if (!text.Equals("/model", StringComparison.OrdinalIgnoreCase)
            && !text.StartsWith("/model ", StringComparison.OrdinalIgnoreCase))
            return new ModelCommandResult(false, "");

        var argument = text.Length == "/model".Length
            ? ""
            : text["/model".Length..].Trim();
        var forceRefresh = argument.Equals("refresh", StringComparison.OrdinalIgnoreCase)
            || argument.Equals("--refresh", StringComparison.OrdinalIgnoreCase);

        if (argument.Equals("auto", StringComparison.OrdinalIgnoreCase)
            || argument.Equals("default", StringComparison.OrdinalIgnoreCase)
            || argument.Equals("reset", StringComparison.OrdinalIgnoreCase))
        {
            await SetPreferenceAsync(sessionKey, null, cancellationToken);
            var automatic = await ResolveAsync(sessionKey, cancellationToken, forceRefresh: true);
            if (!automatic.IsAvailable)
            {
                return new ModelCommandResult(true,
                    "Model selection is now **automatic**, but no chat-capable Foundry model " +
                    "deployment is currently available under the administrator's policy.");
            }
            return new ModelCommandResult(true,
                $"Model selection is now **automatic**. Current model: `{automatic.Model}`.\n\n" +
                $"Priority: {FormatInlineList(automatic.Priority)}");
        }

        var inventory = await GetInventoryAsync(cancellationToken, forceRefresh);
        if (!string.IsNullOrWhiteSpace(argument) && !forceRefresh
            && !argument.Equals("list", StringComparison.OrdinalIgnoreCase)
            && !argument.Equals("status", StringComparison.OrdinalIgnoreCase))
        {
            var requested = inventory.Models.FirstOrDefault(
                m => m.Equals(argument, StringComparison.OrdinalIgnoreCase));
            if (requested is null)
            {
                var available = inventory.Models.Count == 0
                    ? "No chat-capable deployments could be discovered."
                    : $"Available models: {FormatInlineList(inventory.Models)}";
                return new ModelCommandResult(true,
                    $"`{argument}` is not an available model for this Hermes agent.\n\n{available}");
            }

            await SetPreferenceAsync(sessionKey, requested, cancellationToken);
            return new ModelCommandResult(true,
                $"Model changed to `{requested}` for your Hermes conversation. " +
                "Use `/model auto` to return to the administrator's priority order.");
        }

        var selection = await ResolveAsync(sessionKey, cancellationToken, forceRefresh);
        if (!selection.IsAvailable)
        {
            var detail = string.IsNullOrWhiteSpace(selection.Warning)
                ? ""
                : $"\n\nDiscovery warning: {selection.Warning}";
            return new ModelCommandResult(true,
                "No chat-capable Foundry model deployment is currently available under the " +
                $"administrator's policy.{detail}");
        }
        var mode = selection.IsExplicit ? "your selection" : "automatic priority";
        var warning = selection.Available.Count == 0 && !string.IsNullOrWhiteSpace(selection.Warning)
            ? $"\n\nDiscovery warning: {selection.Warning}"
            : "";
        return new ModelCommandResult(true,
            $"Current model: `{selection.Model}` ({mode}).\n\n" +
            $"Available: {FormatInlineList(selection.Available)}\n\n" +
            $"Automatic priority: {FormatInlineList(selection.Priority)}\n\n" +
            "Use `/model <name>` to select one, or `/model auto` to use the best available model " +
            $"from the priority list.{warning}");
    }

    private async Task<ModelInventory> GetInventoryAsync(
        CancellationToken cancellationToken,
        bool forceRefresh)
    {
        var now = DateTimeOffset.UtcNow;
        if (!forceRefresh && _cachedInventory is not null && now < _inventoryExpiresAt)
            return _cachedInventory;

        await _inventoryLock.WaitAsync(cancellationToken);
        try
        {
            now = DateTimeOffset.UtcNow;
            if (!forceRefresh && _cachedInventory is not null && now < _inventoryExpiresAt)
                return _cachedInventory;

            var discovered = await DiscoverFromFoundryAsync(cancellationToken);
            if (discovered is not null)
            {
                _cachedInventory = new ModelInventory(
                    ApplyAllowList(discovered), "Foundry project", null);
                _inventoryExpiresAt = now.Add(_cacheLifetime);
                return _cachedInventory;
            }

            // The snapshot is created by new-version.sh from this same documented API. It keeps
            // model selection operational if a hosted identity may infer but may not enumerate the
            // project. It grants no access by itself: inference still enforces the live deployment.
            _cachedInventory = new ModelInventory(
                _snapshot,
                "deployment snapshot",
                "Live Foundry model discovery was unavailable; showing the last deployment snapshot.");
            _inventoryExpiresAt = now.Add(TimeSpan.FromSeconds(60));
            return _cachedInventory;
        }
        finally
        {
            _inventoryLock.Release();
        }
    }

    private async Task<IReadOnlyList<string>?> DiscoverFromFoundryAsync(
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(_projectEndpoint)) return null;

        try
        {
            var token = await _credential.GetTokenAsync(
                new TokenRequestContext([FoundryScope]), cancellationToken);
            var http = _httpFactory.CreateClient();
            var models = new List<string>();
            var next = new Uri($"{_projectEndpoint}/deployments?api-version=v1");
            var expectedHost = next.Host;

            // Follow the documented nextLink contract while ensuring the Foundry bearer token is
            // never sent to another host.
            for (var page = 0; page < 20 && next is not null; page++)
            {
                if (next.Scheme != Uri.UriSchemeHttps
                    || !next.Host.Equals(expectedHost, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("Foundry returned an unsafe deployments nextLink.");

                using var request = new HttpRequestMessage(HttpMethod.Get, next);
                request.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue(
                    "Bearer", token.Token);
                using var response = await http.SendAsync(request, cancellationToken);
                if (!response.IsSuccessStatusCode)
                {
                    _logger.LogWarning(
                        "Foundry model discovery returned HTTP {Status}; using the deployment snapshot.",
                        (int)response.StatusCode);
                    return null;
                }

                await using var stream = await response.Content.ReadAsStreamAsync(cancellationToken);
                using var document = await JsonDocument.ParseAsync(stream, cancellationToken: cancellationToken);
                if (document.RootElement.TryGetProperty("value", out var value)
                    && value.ValueKind == JsonValueKind.Array)
                {
                    foreach (var deployment in value.EnumerateArray())
                    {
                        var type = StringProperty(deployment, "type");
                        var name = CleanName(StringProperty(deployment, "name"));
                        if (name is null || !type.Equals("ModelDeployment", StringComparison.OrdinalIgnoreCase))
                            continue;
                        if (!IsChatCapable(deployment)) continue;
                        models.Add(name);
                    }
                }

                next = null;
                if (document.RootElement.TryGetProperty("nextLink", out var nextLink)
                    && nextLink.ValueKind == JsonValueKind.String
                    && Uri.TryCreate(nextLink.GetString(), UriKind.Absolute, out var parsedNext))
                    next = parsedNext;
            }

            var result = DistinctNames(models);
            _logger.LogInformation(
                "Foundry model discovery found {Count} selectable chat deployments.", result.Count);
            return result;
        }
        catch (Exception ex) when (ex is AuthenticationFailedException
            or HttpRequestException
            or TaskCanceledException
            or JsonException
            or InvalidOperationException)
        {
            _logger.LogWarning(
                "Foundry model discovery failed ({ExceptionType}); using the deployment snapshot.",
                ex.GetType().Name);
            return null;
        }
    }

    private async Task<string?> GetPreferenceAsync(
        string sessionKey,
        CancellationToken cancellationToken)
    {
        await _preferencesLock.WaitAsync(cancellationToken);
        try
        {
            await EnsurePreferencesLoadedAsync(cancellationToken);
            return _preferences!.TryGetValue(PreferenceKey(sessionKey), out var model)
                ? model
                : null;
        }
        finally
        {
            _preferencesLock.Release();
        }
    }

    private async Task SetPreferenceAsync(
        string sessionKey,
        string? model,
        CancellationToken cancellationToken)
    {
        await _preferencesLock.WaitAsync(cancellationToken);
        try
        {
            await EnsurePreferencesLoadedAsync(cancellationToken);
            var key = PreferenceKey(sessionKey);
            if (string.IsNullOrWhiteSpace(model)) _preferences!.Remove(key);
            else _preferences![key] = model;

            var directory = Path.GetDirectoryName(_preferencesPath)!;
            Directory.CreateDirectory(directory);
            var temporary = $"{_preferencesPath}.{Guid.NewGuid():N}.tmp";
            await File.WriteAllTextAsync(
                temporary,
                JsonSerializer.Serialize(_preferences, new JsonSerializerOptions { WriteIndented = true }),
                cancellationToken);
            File.Move(temporary, _preferencesPath, overwrite: true);
        }
        finally
        {
            _preferencesLock.Release();
        }
    }

    private async Task EnsurePreferencesLoadedAsync(CancellationToken cancellationToken)
    {
        if (_preferences is not null) return;
        _preferences = new Dictionary<string, string>(StringComparer.Ordinal);
        try
        {
            if (!File.Exists(_preferencesPath)) return;
            var json = await File.ReadAllTextAsync(_preferencesPath, cancellationToken);
            var stored = JsonSerializer.Deserialize<Dictionary<string, string>>(json);
            if (stored is null) return;
            foreach (var (key, value) in stored)
            {
                var model = CleanName(value);
                if (key.Length == 64 && model is not null) _preferences[key] = model;
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException)
        {
            _logger.LogWarning(
                "Could not read persisted model preferences ({ExceptionType}); starting empty.",
                ex.GetType().Name);
        }
    }

    private string ChooseAutomatic(IReadOnlyList<string> available)
    {
        foreach (var preferred in _priority)
        {
            var match = available.FirstOrDefault(
                m => m.Equals(preferred, StringComparison.OrdinalIgnoreCase));
            if (match is not null) return match;
        }
        // A successfully empty inventory means the admin has removed all chat deployments (or an
        // optional local allow-list excludes all of them). Do not silently bypass that policy by
        // sending the historical primary deployment anyway. The deployment snapshot already covers
        // the separate case where live enumeration itself is unavailable.
        return available.FirstOrDefault() ?? "";
    }

    private IReadOnlyList<string> ApplyAllowList(IEnumerable<string> models)
    {
        var filtered = _allowList is null
            ? models
            : models.Where(_allowList.Contains);
        return DistinctNames(filtered);
    }

    private static bool IsChatCapable(JsonElement deployment)
    {
        if (!deployment.TryGetProperty("capabilities", out var capabilities)
            || capabilities.ValueKind != JsonValueKind.Object)
            return false;
        return IsTruthy(capabilities, "chat_completion")
            || IsTruthy(capabilities, "responses")
            || IsTruthy(capabilities, "chatCompletion");
    }

    private static bool IsTruthy(JsonElement value, string property)
    {
        if (!value.TryGetProperty(property, out var item)) return false;
        return item.ValueKind == JsonValueKind.True
            || (item.ValueKind == JsonValueKind.String
                && item.GetString()?.Equals("true", StringComparison.OrdinalIgnoreCase) == true);
    }

    private static string StringProperty(JsonElement value, string property) =>
        value.TryGetProperty(property, out var item) && item.ValueKind == JsonValueKind.String
            ? item.GetString() ?? ""
            : "";

    private static string ResolveProjectEndpoint(IConfiguration configuration)
    {
        var project = (configuration["AZURE_AI_PROJECT_ENDPOINT"] ?? "").Trim().TrimEnd('/');
        if (!string.IsNullOrWhiteSpace(project)) return project;
        var modelBase = (configuration["AZURE_FOUNDRY_BASE_URL"] ?? "").Trim().TrimEnd('/');
        const string suffix = "/openai/v1";
        return modelBase.EndsWith(suffix, StringComparison.OrdinalIgnoreCase)
            ? modelBase[..^suffix.Length]
            : "";
    }

    private static IEnumerable<string> ParseList(string? value) =>
        (value ?? "").Split([',', ';'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);

    private static IReadOnlyList<string> DistinctNames(IEnumerable<string> values)
    {
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var result = new List<string>();
        foreach (var raw in values)
        {
            var value = CleanName(raw);
            if (value is not null && seen.Add(value)) result.Add(value);
        }
        return result;
    }

    private static string? CleanName(string? value)
    {
        var cleaned = (value ?? "").Trim();
        return SafeDeploymentName.IsMatch(cleaned) ? cleaned : null;
    }

    private static string PreferenceKey(string sessionKey) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(sessionKey ?? "")))
            .ToLowerInvariant();

    private static string FormatInlineList(IReadOnlyList<string> models) =>
        models.Count == 0 ? "(none)" : string.Join(" → ", models.Select(m => $"`{m}`"));

    private sealed record ModelInventory(
        IReadOnlyList<string> Models,
        string Source,
        string? Warning);
}

public sealed record ModelSelection(
    string Model,
    IReadOnlyList<string> Available,
    IReadOnlyList<string> Priority,
    bool IsExplicit,
    string DiscoverySource,
    string? Warning)
{
    public bool IsAvailable => !string.IsNullOrWhiteSpace(Model);

    public string TrustedPromptContext =>
        "TRUSTED FOUNDRY MODEL POLICY: This turn is running on deployment '" + Model + "'. " +
        "The chat-capable deployments currently available to this Hermes runtime are: " +
        (Available.Count == 0 ? "unknown" : string.Join(", ", Available)) + ". " +
        "The user can inspect or change their conversation model in Teams with /model. " +
        "Do not claim that an unlisted model is available.";
}

public sealed record ModelCommandResult(bool Handled, string Reply);
