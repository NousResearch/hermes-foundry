using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using Azure.AI.AgentServer.Responses;
using Azure.AI.AgentServer.Responses.Models;
using HelloWorldA365.Services;

namespace HelloWorldA365.AgentLogic.ResponsesProtocol;

/// <summary>
/// Serves the <b>Responses</b> protocol, alongside the Activity protocol on /api/messages.
///
/// <para><b>This is not <c>AgentLogic=ResponsesApi</c>.</b> "Responses protocol" is only the wire
/// protocol Foundry routines use to wake the agent. This handler remains on the normal HermesRouter
/// path and forwards routine input to the same in-container Hermes gateway as interactive turns.</para>
///
/// Foundry <b>routines</b> (timer / recurring / event triggers) can only act via
/// <c>invoke_agent_responses_api</c> or <c>invoke_agent_invocations_api</c>. Neither can target an
/// agent that declares only the Activity protocol. Hosted agents can combine Responses and Activity
/// protocols, so this class adds the Responses path needed by routines.
///
/// The two protocols share one brain on purpose: this handler calls exactly the same gateway
/// endpoint that <see cref="HermesRouter.HermesRouterAgentLogicService"/> calls for Teams turns, so
/// a scheduled wake-up and a human message reach Hermes through the same path with the same auth.
///
/// Wiring is the SDK's "Tier 3" self-hosting pattern: AddResponsesServer() + MapResponsesServer()
/// into our existing WebApplication, rather than ResponsesServer.Run&lt;T&gt;() which would take over
/// the host and displace /api/messages.
/// </summary>
public sealed class HermesResponseHandler(
    IConfiguration config,
    IHttpClientFactory httpFactory,
    AgentTokenHelper agentTokenHelper,
    ILogger<HermesResponseHandler> logger) : ResponseHandler
{
    private readonly string _routerUrl = (config["RouterUrl"] ?? "").TrimEnd('/');
    private readonly string _routerKey = config["RouterKey"] ?? "";

    public override IAsyncEnumerable<ResponseStreamEvent> CreateAsync(
        CreateResponse request,
        ResponseContext context,
        CancellationToken cancellationToken)
        => new TextResponse(context, request, createText: ct => RunAsync(context, ct));

    private async Task<string> RunAsync(ResponseContext context, CancellationToken ct)
    {
        var prompt = await context.GetInputTextAsync(cancellationToken: ct) ?? "";
        if (string.IsNullOrWhiteSpace(prompt))
            return "(no input)";

        if (string.IsNullOrEmpty(_routerUrl))
        {
            logger.LogError("RouterUrl is not configured; the Responses protocol has nowhere to forward to.");
            return "The gateway isn't configured.";
        }

        // A routine has no Teams conversation, so there is no channel-supplied session key. Key on
        // the agent itself, which keeps every scheduled wake-up on one stable Hermes session rather
        // than spawning a fresh one per run.
        var sessionKey = $"routine:{Environment.GetEnvironmentVariable("AZURE_AI_AGENT_NAME") ?? "hermes"}";

        EdgeLog("responses.in", $"chars={prompt.Length} session={sessionKey}");

        var payload = JsonSerializer.Serialize(new
        {
            model = "hermes-agent",
            messages = new[] { new { role = "user", content = prompt } }
        });

        var http = httpFactory.CreateClient();
        http.Timeout = TimeSpan.FromMinutes(15);

        using var req = new HttpRequestMessage(HttpMethod.Post, $"{_routerUrl}/v1/chat/completions")
        {
            Content = new StringContent(payload, Encoding.UTF8, "application/json")
        };
        if (!string.IsNullOrEmpty(_routerKey))
            req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _routerKey);
        req.Headers.TryAddWithoutValidation("X-Hermes-Session-Key", sessionKey);

        // Mint the agent's own Graph token, exactly as the Activity path does — but from CONFIG,
        // because a routine-driven turn has no ITurnContext and therefore no AgentMetadata. Without
        // this a scheduled wake-up reaches Hermes with no credential and the m365 tools report
        // "no agent credential present", which is indistinguishable from the token being broken.
        var bpId = config["AgentBlueprintId"];
        var instId = config["AgentInstanceId"];
        var upn = config["AgentUpn"];
        // The agent user is the final token subject. Prefer its object id and retain UPN as fallback.
        var subject = (string.IsNullOrEmpty(config["AgentUserObjectId"]) ? upn : config["AgentUserObjectId"])!;
        var tenant = config["AgentTenantId"];
        if (!string.IsNullOrEmpty(bpId) && !string.IsNullOrEmpty(instId)
            && !string.IsNullOrEmpty(upn) && !string.IsNullOrEmpty(tenant))
        {
            try
            {
                // Turn-less routine calls have no channel token and Identity Proxy Manager's T1 is
                // bound to the hosted runtime identity, not the separately published identity that
                // owns this Agent User. Use the documented autonomous flow when the isolated
                // blueprint certificate is configured: certificate -> T1 with fmi_path -> Agent
                // Identity T2 -> Agent User token. Existing channel behavior remains unchanged.
                var useRoutineCertificate =
                    !string.IsNullOrWhiteSpace(config["RoutineCredentialVaultUri"]) &&
                    !string.IsNullOrWhiteSpace(config["RoutineCredentialCertificateName"]);
                var graphToken = useRoutineCertificate
                    ? await agentTokenHelper.GetAgenticUserTokenWithBlueprintCertificateAsync(
                        bpId,
                        instId,
                        subject,
                        tenant,
                        ["https://graph.microsoft.com/.default"])
                    : await agentTokenHelper.GetAgenticUserTokenAsync(
                        bpId,
                        instId,
                        subject,
                        tenant,
                        ["https://graph.microsoft.com/.default"]);
                if (!string.IsNullOrEmpty(graphToken))
                {
                    req.Headers.TryAddWithoutValidation("X-Agent-Graph-Token", graphToken);
                    req.Headers.TryAddWithoutValidation("X-Agent-Upn", upn);
                    EdgeLog("token.minted", $"responses provider={(useRoutineCertificate ? "blueprint-certificate" : "identity-proxy")} len={graphToken.Length} subject={subject}");
                }
                else EdgeLog("token.missing", "responses: helper returned null");
            }
            catch (Exception ex)
            {
                // Log the AADSTS code, not MSAL's paraphrase. "Service is unavailable to process the
                // request" told us nothing for a whole debugging round; the ErrorCode and response
                // body are what identify the cause.
                var msal = ex as Microsoft.Identity.Client.MsalServiceException;
                EdgeLog("token.error",
                    $"responses subject={subject} {ex.GetType().Name}: {ex.Message} " +
                    $"| errorCode={msal?.ErrorCode ?? "(n/a)"} status={msal?.StatusCode.ToString() ?? "(n/a)"} " +
                    $"| body={(msal?.ResponseBody ?? "").Replace("\n", " ")} " +
                    $"| inner={ex.InnerException?.Message}");
            }
        }
        else EdgeLog("token.unconfigured", $"bp={bpId is null} inst={instId is null} upn={upn is null} tid={tenant is null}");

        try
        {
            using var res = await http.SendAsync(req, ct);
            var text = await res.Content.ReadAsStringAsync(ct);
            if (!res.IsSuccessStatusCode)
            {
                EdgeLog("responses.err", $"gateway {(int)res.StatusCode}");
                return $"The gateway returned {(int)res.StatusCode}.";
            }

            using var doc = JsonDocument.Parse(text);
            var root = doc.RootElement;
            // Success is a chat-completions shape. Control responses (needs_auth, quota, busy) are
            // plain JSON — reaching blindly for "choices" turns those into an exception and hides
            // the actual reason, which cost us a debugging cycle on the Activity path.
            if (root.TryGetProperty("choices", out var choices) && choices.GetArrayLength() > 0)
            {
                var reply = choices[0].GetProperty("message").GetProperty("content").GetString() ?? "";
                EdgeLog("responses.out", $"chars={reply.Length}");
                return reply;
            }
            if (root.TryGetProperty("message", out var m) && m.ValueKind == JsonValueKind.String)
                return m.GetString() ?? "";
            if (root.TryGetProperty("error", out var e))
                return $"The gateway rejected that: {e}";
            return $"Unexpected gateway response: {text[..Math.Min(200, text.Length)]}";
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "Responses turn failed");
            EdgeLog("responses.err", ex.GetType().Name);
            return "The gateway was unreachable.";
        }
    }

    /// <summary>
    /// Mirror of the Activity path's EdgeLog. Foundry's own :logstream returns session_not_found for
    /// us, so this out-of-band sink is the only way to see whether a routine actually reached the
    /// container. Fire-and-forget and fully swallowed — diagnostics must never break a turn.
    /// </summary>
    private void EdgeLog(string stage, string detail)
    {
        if (string.IsNullOrEmpty(_routerUrl)) return;
        _ = Task.Run(async () =>
        {
            try
            {
                var http = httpFactory.CreateClient();
                var session = Environment.GetEnvironmentVariable("FOUNDRY_AGENT_SESSION_ID") ?? "(unset)";
                using var req = new HttpRequestMessage(HttpMethod.Post, $"{_routerUrl}/v1/edge-log")
                {
                    Content = new StringContent(
                        JsonSerializer.Serialize(new { stage, detail, session }),
                        Encoding.UTF8, "application/json")
                };
                if (!string.IsNullOrEmpty(_routerKey))
                    req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _routerKey);
                using var _ = await http.SendAsync(req);
            }
            catch { /* best effort by design */ }
        });
    }
}
