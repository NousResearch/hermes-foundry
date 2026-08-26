namespace HelloWorldA365.AgentLogic;

using HelloWorldA365.AgentLogic.ResponsesApi;
using HelloWorldA365.Models;
using Microsoft.Agents.Builder.App;
using Microsoft.Agents.Core.Models;
using AgentNotification;
using Microsoft.Agents.A365.Notifications.Models;

/// <summary>
/// This is main handler for incoming activities, and is linked to Agent SDK infrastructure.
/// This will need to resolve the incoming activity to the correct agent instance.
/// </summary>
public class A365AgentApplication : AgentApplication
{
    private readonly ResponsesApiAgentLogicServiceFactory _factory;
    private readonly IConfiguration _configuration;
    private readonly ILogger<A365AgentApplication> _logger;

    public A365AgentApplication(
        AgentApplicationOptions options,
        ResponsesApiAgentLogicServiceFactory factory,
        ILogger<A365AgentApplication> logger,
        IConfiguration configuration) : base(options)
    {
        _factory = factory ?? throw new ArgumentNullException(nameof(factory));
        _configuration = configuration ?? throw new ArgumentNullException(nameof(configuration));
        // Configure the agent to handle message activities
        ConfigureMessageHandling();
        _logger = logger;
    }

    /// <summary>
    /// Configures message handling for the agent.
    /// </summary>
    /// <summary>Name of the AgenticUserAuthorization handler declared in appsettings.json.
    /// Every A365 notification route must opt into it explicitly: AutoSignin is false, so without
    /// this the agentic sign-in never runs for that route and the turn has no agentic user token.
    /// Matches adner/Agent365_Notification_Sample, which handles email notifications successfully.</summary>
    private const string AgenticIdAuthHandler = "agentic";

    private void ConfigureMessageHandling()
    {
        // ── EMAIL NOTIFICATIONS: RE-REGISTERED (2026-08-10) ───────────────────────────────────
        //
        // This was commented out on 2026-08-07 — deliberately parked, never deleted — so that the
        // Teams/activity path could be tested without inbound email retries muddying the signal.
        // It is back on, because it is the actual email path and the agent is meant to have it.
        //
        // History worth keeping, because it shaped the (wrong) conclusion that followed: with this
        // registered, inbound email failed at Foundry's ingress with a CheckAccess 400 and bounced a
        // stack trace to the sender ~4 times per message. That volume is what Exchange flagged as
        // suspicious outbound sending on 2026-08-05, and it is why the handler came off.
        //
        // What we did NOT know then, and do now: the blueprint had NO Microsoft Graph consent, so
        // the agentic user token could never be minted at all (AADSTS65001 on the user_fic step,
        // RUNBOOK 7z). Graph inheritance now reports OK, 5 of 5. Several conclusions recorded while
        // that was broken deserve re-testing rather than restating.
        //
        // Dedupe is already in place: HermesRouterAgentLogicService.AlreadyReplied() keys on the
        // activity id, so a redelivered notification cannot produce a second reply. That is the
        // control that makes re-registering safe this time.
        this.OnAgenticEmailNotification(async (turnContext, turnState, agentNotificationActivity, cancellationToken) =>
        {
            var agent = await GetAgentFromRecipient(turnContext.Activity);
            var agentService = await _factory.CreateAsync(agent, turnContext, UserAuthorization);
            await agentService.HandleEmailNotificationAsync(turnContext, turnState, agentNotificationActivity);
        }, autoSignInHandlers: new[] { AgenticIdAuthHandler });

        // Handle Word notifications
        this.OnAgenticWordNotification(async (turnContext, turnState, agentNotificationActivity, cancellationToken) =>
        {
            var agent = await GetAgentFromRecipient(turnContext.Activity);
            var agentService = await _factory.CreateAsync(agent, turnContext, UserAuthorization);

            await agentService.HandleCommentNotificationAsync(turnContext, turnState, agentNotificationActivity);
        }, autoSignInHandlers: new[] { AgenticIdAuthHandler });

        // Handle Excel notifications
        this.OnAgenticExcelNotification(async (turnContext, turnState, agentNotificationActivity, cancellationToken) =>
        {
            var agent = await GetAgentFromRecipient(turnContext.Activity);
            var agentService = await _factory.CreateAsync(agent, turnContext, UserAuthorization);
            // Use the specific comment notification handler for Excel documents
            await agentService.HandleCommentNotificationAsync(turnContext, turnState, agentNotificationActivity);
        }, autoSignInHandlers: new[] { AgenticIdAuthHandler });

        // Handle PowerPoint notifications
        this.OnAgenticPowerPointNotification(async (turnContext, turnState, agentNotificationActivity, cancellationToken) =>
        {
            var agent = await GetAgentFromRecipient(turnContext.Activity);
            var agentService = await _factory.CreateAsync(agent, turnContext, UserAuthorization);
            // Use the specific comment notification handler for PowerPoint documents
            await agentService.HandleCommentNotificationAsync(turnContext, turnState, agentNotificationActivity);
        }, autoSignInHandlers: new[] { AgenticIdAuthHandler });

        OnActivity(ActivityTypes.Message, async (turnContext, turnState, cancellationToken) =>
        {
            var streamingStarted = false;
            try
            {
                _logger.LogInformation("Received message activity: {ActivityId} from {UserId} activity {activity}", turnContext.Activity.Id, turnContext.Activity.From?.Id, System.Text.Json.JsonSerializer.Serialize(turnContext.Activity));
                // Based on the recipient, determine which agent to use
                var agent = await GetAgentFromRecipient(turnContext.Activity);

                // Get agent logic service from factory
                var agentService = await _factory.CreateAsync(agent, turnContext, UserAuthorization);

                // Ignoring all other channel Ids to prevent duplicate notifications.
                if (agent.IsMessagingEnabled && turnContext.Activity.ChannelId != "msteams")
                {
                    return;
                }

                // Let the user know the agent is working on the prompt.
                // This is a no-op on channels that don't support streaming/typing updates.
                await turnContext.StreamingResponse.QueueInformativeUpdateAsync(
                    "Working on your request...",
                    cancellationToken);
                streamingStarted = true;

                // Execute logic
                await agentService.NewActivityReceived(turnContext, turnState, cancellationToken);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error processing message activity");

                var sessionId = Environment.GetEnvironmentVariable("FOUNDRY_AGENT_SESSION_ID");
                var sessionLine = string.IsNullOrEmpty(sessionId) ? "(not set)" : sessionId;
                var errorText =
                    $"Sorry, something went wrong while processing your message.{Environment.NewLine}" +
                    $"FOUNDRY_AGENT_SESSION_ID: {sessionLine}{Environment.NewLine}" +
                    $"Exception:{Environment.NewLine}{ex}";

                if (streamingStarted)
                {
                    try
                    {
                        turnContext.StreamingResponse.QueueTextChunk(errorText);
                    }
                    catch (Exception streamEx)
                    {
                        _logger.LogWarning(streamEx, "Failed to queue error text on streaming response");
                    }
                }
                else
                {
                    await turnContext.SendActivitiesAsync(new IActivity[]
                    {
                        new Activity
                        {
                            Type = ActivityTypes.Message,
                            Text = errorText
                        }
                    }, cancellationToken);
                }
            }
            finally
            {
                if (streamingStarted)
                {
                    try
                    {
                        // Always finalize the stream so the channel renders a final message
                        // instead of falling back to "No text was streamed".
                        await turnContext.StreamingResponse.EndStreamAsync(cancellationToken);
                    }
                    catch (Exception streamEx)
                    {
                        _logger.LogWarning(streamEx, "Failed to end streaming response");
                    }
                }
            }
        });

        // Keep existing handlers for backward compatibility
        OnActivity(ActivityTypes.Event, async (turnContext, turnState, cancellationToken) =>
        {
            var agent = await GetAgentFromRecipient(turnContext.Activity);
            var agentService = await _factory.CreateAsync(agent, turnContext, UserAuthorization);

            await agentService.NewActivityReceived(turnContext, turnState, cancellationToken);
        });

        OnActivity(ActivityTypes.InstallationUpdate, async (turnContext, turnState, cancellationToken) =>
        {
            var agent = await GetAgentFromRecipient(turnContext.Activity);
            var agentService = await _factory.CreateAsync(agent, turnContext, UserAuthorization);

            // Installation updates are lifecycle events, not user prompts. Always route them to the
            // dedicated handler. The old IsMessagingEnabled check could never pass because the
            // activity-derived AgentMetadata does not set that optional sample flag; as a result,
            // newly provisioned hfull instances were sent through the ordinary Hermes turn path and
            // the deterministic owner greeting below was unreachable. Each logic implementation
            // decides what an installation event means: HermesRouter sends the welcome message,
            // while the non-production ResponsesApi implementation deliberately does nothing.
            var agentNotificationActivity = new AgentNotificationActivity(turnContext.Activity);
            await agentService.HandleInstallationUpdateAsync(turnContext, turnState, agentNotificationActivity);
		});
    }

    private async Task<AgentMetadata> GetAgentFromRecipient(IActivity activity)
    {
        ChannelAccount recipient = activity.Recipient;
        ConversationAccount conversation = activity.Conversation;

        if (recipient == null)
        {
            throw new ArgumentNullException(nameof(recipient), "Recipient cannot be null.");
        }

        // Recipient will have an ID, but this may not be sufficient to determine the agent.
        // ChannelAccount recipient currently has an AadObjectId, which we can try using to identify the user.
        // If activityProtocol and SDK is changed to pass a new field, we can update this code to use that instead.
        var aadObjectId = Guid.TryParse(recipient.AadObjectId, out var parsedId) ? parsedId : Guid.Empty;
        var id = recipient.Id;
        var tenantId = Guid.TryParse(conversation.TenantId, out var parsedTenantId) ? parsedTenantId : Guid.Empty;
        return ConstructAgentMetadataFromActivity(activity);
    }

    private AgentMetadata ConstructAgentMetadataFromActivity(IActivity activity)
    {
        if (activity == null)
        {
            throw new ArgumentNullException(nameof(activity), "Activity cannot be null.");
        }

        var recipient = activity.Recipient;
        var conversation = activity.Conversation;

        if (recipient == null || conversation == null)
        {
            throw new ArgumentException("Activity must have a recipient and conversation.");
        }

        var tenantId = Guid.TryParse(recipient.TenantId, out var parsedTenantId) ? parsedTenantId : Guid.Empty;

        // AAI
        var agenticAppId = Guid.TryParse(recipient.AgenticAppId, out var parsedAgenticAppId) ? parsedAgenticAppId : Guid.Empty;

        // This could be agentic user email?
        var agenticUserId = recipient.AgenticUserId ?? recipient.AadObjectId;



        return new AgentMetadata
        {
            UserId = Guid.Parse(agenticUserId),
            EmailId = recipient.Id.Contains('@') ? recipient.Id : recipient.Name,
            AgentId = agenticAppId,
            AgentApplicationId = recipient.Properties.TryGetValue("agenticAppBlueprintId", out var agentAppBlueprintId) ? Guid.Parse(agentAppBlueprintId.ToString()) : Guid.TryParse(recipient.Id, out var parsedId) ? parsedId : Guid.Empty,
            TenantId = tenantId,
        };
    }
}
