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
/// WHY THIS EXISTS
/// ---------------
/// Foundry <b>routines</b> (timer / recurring / event triggers) can only act via
/// <c>invoke_agent_responses_api</c> or <c>invoke_agent_invocations_api</c>. Neither can target an
/// agent that declares only the Activity protocol, which is what the a365 autopilot sample builds.
/// Every one of our 20 routine runs failed 400 because of that, and we wrongly recorded it as a
/// platform limitation (see AGENT365_RUNBOOK.md §7v, since corrected).
///
/// It is not a limitation. Microsoft's hosted-agents guidance names <b>Responses + Activity</b> as
/// THE shape for an agent published to Teams/M365 — "supported protocols can be combined in a single
/// agent", and the platform bridges Responses to Activity for channel delivery. This class adds the
/// missing half.
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
    AgentUserAuthorizationService agentUserAuthorization,
    RoutineBindingService routineBindings,
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
        var signedInput = await context.GetInputTextAsync(cancellationToken: ct) ?? "";
        if (string.IsNullOrWhiteSpace(signedInput))
            return "(no input)";

        // The Responses endpoint is a non-turn entry point. It has no Agent 365 Activity metadata,
        // so ordinary caller input is never authority to select an Agent User. Only an action that
        // our edge created and signed during a verified owner turn may receive Agent OBO or execute
        // scheduled work. This also rejects Foundry's manual-dispatch input override: testing must
        // dispatch the stored action unchanged.
        if (!routineBindings.TryReadInvocation(signedInput, out var binding, out var bindingError))
        {
            EdgeLog("responses.rejected", bindingError);
            return "This invocation is not bound to this Hermes instance and was not executed.";
        }
        var prompt = BuildRoutineExecutionPrompt(binding.Task);

        if (string.IsNullOrEmpty(_routerUrl))
        {
            logger.LogError("RouterUrl is not configured; the Responses protocol has nowhere to forward to.");
            return "The gateway isn't configured.";
        }

        // A routine has no Teams conversation, so there is no channel-supplied session key. Key on
        // the agent itself, which keeps every scheduled wake-up on one stable Hermes session rather
        // than spawning a fresh one per run.
        var sessionKey = $"routine:{binding.AgentIdentityId}:{binding.RoutineName}";

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
        req.Headers.TryAddWithoutValidation("X-Hermes-Session-Id", sessionKey);
        // The owner authorized this exact immutable task when create_routine was called. Treat the
        // scheduled execution as owner-authorized only after signature verification above; an
        // unsigned Responses caller cannot set this header or receive a Graph token.
        req.Headers.TryAddWithoutValidation("X-Caller-Tier", "owner");

        // The identity pair and required authorization mode come from the verified routine binding,
        // not deployment-wide config and not model input. The binding contains no OAuth token,
        // refresh token, certificate, or secret. Microsoft.Identity.Web uses the blueprint
        // credential from Key Vault, validates the blueprint-child and Agent Identity/User
        // relationships, and mints a fresh short-lived downstream token through native Agent OBO.
        try
        {
            var graphToken = await agentUserAuthorization.GetAccessTokenAsync(
                binding.AgentIdentityId,
                binding.AgentUserId,
                binding.TenantId,
                ["https://graph.microsoft.com/.default"],
                ct);
            if (!string.IsNullOrEmpty(graphToken))
            {
                req.Headers.TryAddWithoutValidation("X-Agent-Graph-Token", graphToken);
                req.Headers.TryAddWithoutValidation("X-Agent-Upn", binding.AgentUpn);
                EdgeLog("token.minted",
                    $"responses provider=entra-agent-obo len={graphToken.Length} agent={binding.AgentIdentityId}");
            }
        }
        catch (Exception ex)
        {
            // Log the AADSTS code, not MSAL's paraphrase. The error code and response body identify
            // whether the blueprint credential, child relationship, or Agent User grant is wrong.
            var msal = ex as Microsoft.Identity.Client.MsalServiceException;
            EdgeLog("token.error",
                $"responses agent={binding.AgentIdentityId} user={binding.AgentUserId} " +
                $"{ex.GetType().Name}: {ex.Message} | errorCode={msal?.ErrorCode ?? "(n/a)"} " +
                $"status={msal?.StatusCode.ToString() ?? "(n/a)"} " +
                $"| body={(msal?.ResponseBody ?? "").Replace("\n", " ")} " +
                $"| inner={ex.InnerException?.Message}");
            return "This routine could not acquire its Agent User identity and was not executed.";
        }

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
    /// Convert the owner-authorized per-run task into an execution turn.
    ///
    /// The stored task can naturally contain words such as "daily" or "every run" because it was
    /// authored while the owner was creating a schedule. Forwarding that text by itself caused
    /// Hermes to apply its scheduling instructions a second time: the fired routine asked for a
    /// time, or claimed it had created another routine, instead of doing the work. The signed
    /// binding already proves that Foundry has scheduled and fired the task, so this envelope is
    /// trusted edge context rather than model- or caller-supplied authority.
    /// </summary>
    private static string BuildRoutineExecutionPrompt(string task) =>
        "TRUSTED FOUNDRY ROUTINE EXECUTION\n\n"
      + "A Microsoft Foundry Routine has already reached its scheduled occurrence. This is NOT a "
      + "request to create, change, inspect, or discuss a schedule. Do not call create_routine, "
      + "list_routines, or remove_routine. Do not ask what time or time zone the work should use. "
      + "Words such as 'daily', 'scheduled', or 'every run' inside the task describe this routine; "
      + "they are not a new scheduling request.\n\n"
      + "Execute exactly one occurrence of the task NOW, using your own Microsoft 365 identity and "
      + "the tools attached to this run. For a requested side effect, call the appropriate tool and "
      + "treat only its returned success as completion. If the tool fails, report the exact failure; "
      + "never replace execution with scheduling advice or an unsupported claim of success.\n\n"
      + "TASK FOR THIS OCCURRENCE:\n"
      + task;

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
