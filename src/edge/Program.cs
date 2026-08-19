using Azure.AI.AgentServer.Responses;
using Azure.Identity;
using HelloWorldA365.AgentLogic.ResponsesProtocol;
using HelloWorldA365.AgentLogic;
using HelloWorldA365.AgentLogic.ResponsesApi;
using HelloWorldA365.Services;
using Microsoft.Agents.Builder;
using Microsoft.Agents.Hosting.AspNetCore;
using Microsoft.Agents.Storage;

using Microsoft.ApplicationInsights.Extensibility;
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

    Console.WriteLine($"Azure Key Vault configured: {keyVaultUri}");
}
else
{
    Console.WriteLine("KeyVaultName not configured. Key Vault integration skipped.");
}

// Add controllers support
builder.Services.AddControllers();

// ===================================
// These are needed for Agent SDK
// ===================================
builder.Services.AddHttpClient();
builder.Services.AddSingleton<IStorage, MemoryStorage>();
builder.AddAgentApplicationOptions();

builder.AddAgent<A365AgentApplication>();
// Uncomment this so you can get logs of activities.
// builder.Services.AddSingleton<Microsoft.Agents.Builder.IMiddleware[]>([new TranscriptLoggerMiddleware(new FileTranscriptLogger())]);

builder.Services.AddSingleton<ResponsesApiAgentLogicServiceFactory>();

// Register auth helper
builder.Services.AddSingleton<AgentTokenHelper>();

// Register OpenAPI for external agents
builder.Services.AddOpenApi();
builder.Services.AddEndpointsApiExplorer();
builder.Services.AddSwaggerGen();
builder.Services.AddLogging();

#region Setup A365


AppContext.SetSwitch("Azure.Experimental.TraceGenAIMessageContent", true);
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

var telemetryConfig = app.Services.GetRequiredService<TelemetryConfiguration>();
Console.WriteLine($"AI ConnectionString: {telemetryConfig.ConnectionString}");

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
        catch (Exception pe) { peek = $"(unparsed {pe.GetType().Name}) bytes={body.Length} head={body[..Math.Min(160, body.Length)]}"; }

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
