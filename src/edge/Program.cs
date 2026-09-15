using Azure.AI.AgentServer.Responses;
using Azure.Identity;
using HelloWorldA365.AgentLogic.ResponsesProtocol;
using HelloWorldA365.AgentLogic;
using HelloWorldA365.AgentLogic.ResponsesApi;
using HelloWorldA365.Services;
using Microsoft.Agents.Builder;
using Microsoft.Agents.Builder.App;
using Microsoft.Agents.Hosting.AspNetCore;
using Microsoft.Agents.Storage;

using Microsoft.ApplicationInsights.Extensibility;
using Microsoft.Identity.Abstractions;
using Microsoft.Identity.Web;
using Microsoft.Identity.Web.TokenCacheProviders.InMemory;
using System.Text;
using Microsoft.Agents.A365.Observability.Runtime;

var builder = WebApplication.CreateBuilder(args);

// Add Azure Key Vault as configuration provider when running in production (not locally)
var keyVaultName = builder.Configuration["KeyVaultName"];
if (!string.IsNullOrEmpty(keyVaultName))
{
    var keyVaultUri = $"https://{keyVaultName}.vault.azure.net/";

    // Use DefaultAzureCredential which will use Managed Service Identity in production
    builder.Configuration.AddAzureKeyVault(
        new Uri(keyVaultUri),
        new DefaultAzureCredential());

    Console.WriteLine("Azure Key Vault configuration enabled.");
}
else
{
    Console.WriteLine("KeyVaultName not configured. Key Vault integration skipped.");
}

// Add controllers support
builder.Services.AddControllers();
builder.Services.AddHttpContextAccessor();

// ===================================
// These are needed for Agent SDK
// ===================================
builder.Services.AddHttpClient();
builder.Services.AddSingleton<IStorage, MemoryStorage>();

// Teams puts uploaded files on ordinary message activities as M365 attachment URLs.
// AgentApplication only turns the ordinary Teams payload into Temp.InputFiles when a downloader is
// registered, so keep Microsoft's downloader as the first path. Agent 365 Autopilot can send a
// different payload (bare SharePoint contentUrl, no content.downloadUrl); the router
// handles that shape separately through Graph as the Agent User after the SDK returns no bytes.
builder.Services.AddSingleton<IList<IInputFileDownloader>>(services =>
[
    new M365AttachmentDownloader(
        services.GetRequiredService<Microsoft.Agents.Authentication.IConnections>(),
        services.GetRequiredService<IHttpClientFactory>())
]);
builder.AddAgentApplicationOptions();

builder.AddAgent<A365AgentApplication>();
// Uncomment this so you can get logs of activities.
// builder.Services.AddSingleton<Microsoft.Agents.Builder.IMiddleware[]>([new TranscriptLoggerMiddleware(new FileTranscriptLogger())]);

builder.Services.AddSingleton<ResponsesApiAgentLogicServiceFactory>();

// Register auth helper
builder.Services.AddSingleton<AgentTokenHelper>();

// Entra Agent OBO for turn-less routine work. This process is the instance-aware SaaS application:
// the blueprint is its confidential client, while AgentInstanceId and AgentUserObjectId select the
// child instance on each invocation. Microsoft.Identity.Web performs the supported blueprint ->
// Agent Identity -> Agent User flow and owns token caching/refresh. There is no human sign-in and
// no delegated token stored by Hermes.
builder.Services.Configure<MicrosoftIdentityApplicationOptions>("AzureAd", options =>
{
    options.Instance = "https://login.microsoftonline.com/";
    options.TenantId = builder.Configuration["AgentTenantId"];
    options.ClientId = builder.Configuration["AgentBlueprintId"];

    var vaultUri = builder.Configuration["RoutineCredentialVaultUri"];
    var certificateName = builder.Configuration["RoutineCredentialCertificateName"];
    if (!string.IsNullOrWhiteSpace(vaultUri) && !string.IsNullOrWhiteSpace(certificateName))
    {
        options.ClientCredentials =
        [
            new CredentialDescription
            {
                SourceType = CredentialSource.KeyVault,
                KeyVaultUrl = vaultUri,
                KeyVaultCertificateName = certificateName
            }
        ];
    }
});
builder.Services.AddTokenAcquisition(isTokenAcquisitionSingleton: true);
builder.Services.AddInMemoryTokenCaches();
builder.Services.AddAgentIdentities();
builder.Services.AddSingleton<AgentUserAuthorizationService>();
// Project-scoped model inventory and per-caller Teams model selection. The service only enumerates
// existing Foundry deployments; it cannot deploy a model or expand anybody's Azure permissions.
builder.Services.AddSingleton<ModelPolicyService>();
// Creates instance-bound routines during verified owner turns and verifies their immutable action
// when Foundry later invokes the Responses protocol without an Activity turn.
builder.Services.AddSingleton<RoutineBindingService>();

// Register OpenAPI for external agents
builder.Services.AddOpenApi();
builder.Services.AddEndpointsApiExplorer();
builder.Services.AddSwaggerGen();
builder.Services.AddLogging();

#region Setup A365


// Prompts and completions may contain email, document and chat content. Keep distributed tracing
// enabled without exporting message bodies to telemetry.
AppContext.SetSwitch("Azure.Experimental.TraceGenAIMessageContent", false);
AppContext.SetSwitch("System.Net.Http.SocketsHttpHandler.Http2UnencryptedSupport", true);

if (Environment.GetEnvironmentVariable("EnableKairoTracing") == "true")
{
    builder.AddA365Tracing(config => { });
}

#endregion


builder.Services.AddApplicationInsightsTelemetry(options =>
{
    Console.WriteLine("Setting Application Insights connection string...");
    options.ConnectionString = builder.Configuration["ApplicationInsights:ConnectionString"];
    options.EnableAdaptiveSampling = false; // Disable adaptive sampling to capture all traces
});

builder.Logging.AddApplicationInsights();


// ─── Responses protocol ───────────────────────────────────────────────────────────────────────
// Added alongside the Activity protocol, not instead of it. Microsoft's hosted-agents guidance
// names "Responses + Activity" as the shape for an agent published to Teams/M365, and only the
// Responses (or Invocations) protocol can be targeted by a Foundry ROUTINE — which is how the agent
// gets woken on a schedule to check its own mailbox.
//
// This is the SDK's "Tier 3" self-hosting pattern (AddResponsesServer + MapResponsesServer) rather
// than ResponsesServer.Run<T>(), because Run<T> owns the whole host and would displace
// /api/messages, which is where every Teams and agentic notification arrives.
builder.Services.AddAgentServerCore();
builder.Services.AddResponsesServer();
builder.Services.AddScoped<ResponseHandler, HermesResponseHandler>();

var app = builder.Build();

_ = app.Services.GetRequiredService<TelemetryConfiguration>();

var logger = app.Services.GetRequiredService<ILogger<Program>>();
logger.LogWarning("Application starting...");

// ===================================
// These are needed for Agent SDK
// ===================================
app.UseRouting();

// Responses protocol middleware + endpoints (/responses). Mapped before /api/messages so both
// protocols are served by the one container, which is what "protocols can be combined" requires.
app.UseAgentServerCore();
app.MapResponsesServer();

// Loopback routine-control API for our co-located MCP sidecar. RouterKey authenticates the local
// process; RoutineBindingService still requires a short-lived capability minted from a verified
// owner Activity turn, so knowing this endpoint or choosing identity IDs in model text grants
// nothing.
static bool IsInternalRoutineCaller(HttpRequest request, IConfiguration configuration)
{
    var expected = configuration["RouterKey"] ?? "";
    var supplied = request.Headers.Authorization.ToString();
    if (string.IsNullOrEmpty(expected) || supplied.Length != "Bearer ".Length + expected.Length)
        return false;
    return System.Security.Cryptography.CryptographicOperations.FixedTimeEquals(
        Encoding.UTF8.GetBytes(supplied), Encoding.UTF8.GetBytes($"Bearer {expected}"));
}

app.MapPost("/internal/routines/create", async (
    HttpRequest request,
    RoutineCreateControlRequest body,
    RoutineBindingService routines,
    IConfiguration configuration,
    CancellationToken cancellationToken) =>
{
    if (!IsInternalRoutineCaller(request, configuration)) return Results.Unauthorized();
    var result = await routines.CreateAsync(
        body.Capability,
        new CreateRoutineRequest
        {
            Name = body.Name,
            Task = body.Task,
            CronExpression = body.CronExpression,
            TimeZone = body.TimeZone
        },
        cancellationToken);
    return result.Ok ? Results.Ok(result) : Results.BadRequest(result);
});

app.MapPost("/internal/routines/list", async (
    HttpRequest request,
    RoutineCapabilityControlRequest body,
    RoutineBindingService routines,
    IConfiguration configuration,
    CancellationToken cancellationToken) =>
{
    if (!IsInternalRoutineCaller(request, configuration)) return Results.Unauthorized();
    var result = await routines.ListAsync(body.Capability, cancellationToken);
    return result.Ok ? Results.Ok(result) : Results.BadRequest(result);
});

app.MapPost("/internal/routines/delete", async (
    HttpRequest request,
    RoutineDeleteControlRequest body,
    RoutineBindingService routines,
    IConfiguration configuration,
    CancellationToken cancellationToken) =>
{
    if (!IsInternalRoutineCaller(request, configuration)) return Results.Unauthorized();
    var result = await routines.DeleteAsync(body.Capability, body.Name, cancellationToken);
    return result.Ok ? Results.Ok(result) : Results.BadRequest(result);
});
// Enable buffering globally - this allows request body to be read multiple times
app.Use(next => context =>
{
    context.Request.EnableBuffering();
    return next(context);
});


app.MapPost("/api/messages", async (HttpRequest request, HttpResponse response, IAgentHttpAdapter adapter, IAgent agent, CancellationToken cancellationToken) =>
{
    // Comment out this line to disable request logging
    // await request.LogRequestAsync();

    request.EnableBuffering();

    using var reader = new StreamReader(request.Body, encoding: Encoding.UTF8, detectEncodingFromByteOrderMarks: false, leaveOpen: true);
    string body = await reader.ReadToEndAsync();

    // Reset stream position so ASP.NET can read it again
    request.Body.Position = 0;

    // HERMES: report every inbound activity BEFORE the SDK sees it.
    //
    // Email notifications never reach our handler. OnAgenticEmailNotification only fires when
    // ChannelId.Channel == "agents" AND SubChannel == "email" — decompiled from
    // Microsoft.Agents.A365.Notifications (ExtensionChannelId = "agents"; IsValidSubChannel =
    // email|excel|word|powerpoint|FederatedKnowledgeService). Logging upstream of routing and of any
    // auth the adapter performs is the ONLY way to distinguish "the email never arrives at this
    // container" from "it arrives and the SDK rejects or misroutes it". Those have completely
    // different fixes, and without this we were guessing between them.
    try
    {
        string peek;
        try
        {
            using var doc = System.Text.Json.JsonDocument.Parse(body);
            var r = doc.RootElement;
            string G(string n) => r.TryGetProperty(n, out var v) ? v.ToString() : "";
            peek = $"type={G("type")} channelId={G("channelId")} name={G("name")} "
                 + $"valueType={G("valueType")} serviceUrl={G("serviceUrl")} bytes={body.Length}";
        }
        catch (Exception pe) { peek = $"unparsed={pe.GetType().Name} bytes={body.Length}"; }

        var cfg = app.Services.GetRequiredService<IConfiguration>();
        var routerUrl = (cfg["RouterUrl"] ?? "").TrimEnd('/');
        if (!string.IsNullOrEmpty(routerUrl))
        {
            var session = Environment.GetEnvironmentVariable("FOUNDRY_AGENT_SESSION_ID") ?? "(unset)";
            var payload = System.Text.Json.JsonSerializer.Serialize(new { stage = "http.in", detail = peek, session });
            using var req = new HttpRequestMessage(HttpMethod.Post, $"{routerUrl}/v1/edge-log")
            { Content = new StringContent(payload, Encoding.UTF8, "application/json") };
            var rk = cfg["RouterKey"] ?? "";
            if (!string.IsNullOrEmpty(rk))
                req.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", rk);
            // AWAIT it, and use a process-wide client. The first version of this used
            // `using var http = new HttpClient(...)` with a fire-and-forget SendAsync — the client was
            // disposed the instant the lambda returned, cancelling the request in flight, so not one
            // http.in line was ever emitted and the diagnostic looked like "email never arrives".
            // A broken instrument reads exactly like the thing it was built to measure.
            await DiagHttpHolder.Client.SendAsync(req, cancellationToken);
        }
    }
    catch { /* diagnostics are best-effort by design */ }

    await adapter.ProcessAsync(request, response, agent, cancellationToken);
});

app.MapGet("/", () => "Hello World from HelloWorldA365Agent!");

app.MapGet("/liveness", () => "Hello World from HelloWorldA365Agent!");

app.MapGet("/readiness", () => "Hello World from HelloWorldA365Agent!");


if (!app.Environment.IsDevelopment())
{
    app.UseHsts();
}

app.Use(next => context =>
{
    context.Request.EnableBuffering();
    return next(context);
});

// Configure the HTTP request pipeline.
if (app.Environment.IsDevelopment())
{
    app.UseSwagger();
    app.UseSwaggerUI();
}

app.UseHttpsRedirection();

// Map controllers
app.MapControllers();

app.Run();

// Process-wide client for the inbound-activity diagnostic in the /api/messages handler. Must NOT be
// per-request and must NOT be disposed while a send is in flight — see the comment at the call site.
static class DiagHttpHolder
{
    public static readonly HttpClient Client = new() { Timeout = TimeSpan.FromSeconds(10) };
}

sealed class RoutineCreateControlRequest
{
    public string Capability { get; set; } = "";
    public string Name { get; set; } = "";
    public string Task { get; set; } = "";
    public string CronExpression { get; set; } = "";
    public string TimeZone { get; set; } = "UTC";
}

class RoutineCapabilityControlRequest
{
    public string Capability { get; set; } = "";
}

sealed class RoutineDeleteControlRequest : RoutineCapabilityControlRequest
{
    public string Name { get; set; } = "";
}
