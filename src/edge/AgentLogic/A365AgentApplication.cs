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
                // Activity bodies can contain chat text, attachment URLs, identity claims and
                // channel credentials. Keep operational logs useful without turning telemetry into
                // a transcript store.
                _logger.LogInformation(
                    "Received {ActivityType} activity on {ChannelId}; attachments={AttachmentCount}",
                    turnContext.Activity.Type,
                    turnContext.Activity.ChannelId,
                    turnContext.Activity.Attachments?.Count ?? 0);
                // Based on the recipient, determine which agent to use
                var agent = await GetAgentFromRecipient(turnContext.Activity);

                // Get agent logic service from factory
                var agentService = await _factory.CreateAsync(agent, turnContext, UserAuthorization);

                // Ignoring all other channel Ids to prevent duplicate notifications.
                if (agent.IsMessagingEnabled && turnContext.Activity.ChannelId != "msteams")
                {
                    return;
                }

                // Agent 365 documentation is explicit: Teams does not support streaming responses
                // for agentic identities. The SDK buffers that "stream" into one message and the
                // Teams stream handle expires while Hermes performs longer research/tool runs. We
                // measured the failure at 122 seconds: Hermes completed, but EndStreamAsync's update
                // of the original activity returned 403 and Teams rendered "This response was
                // stopped". Use Microsoft's documented discrete-message pattern for Agent Users:
                // one immediate acknowledgement and one independent final message. Keep streaming
                // only for ordinary, non-agentic message traffic where the channel supports it.
                var isAgenticTeams =
                    string.Equals(turnContext.Activity.ChannelId, Channels.Msteams,
                        StringComparison.OrdinalIgnoreCase)
                    && AgenticAuthorization.IsAgenticRequest(turnContext);
                if (isAgenticTeams)
                {
                    var progressActivity = await turnContext.SendActivityAsync(
                        MessageFactory.Text("Working on your request..."),
                        cancellationToken);
                    // Keep only the channel-assigned ID for this turn. HermesRouter uses it to edit
                    // this bot-owned activity as Hermes emits tool lifecycle events. This is NOT a
                    // streaming handle and is never used for the final reply.
                    if (!string.IsNullOrWhiteSpace(progressActivity?.Id))
                    {
                        turnContext.Services.Set(
                            HermesRouter.HermesRouterAgentLogicService.ProgressActivityIdStateKey,
                            progressActivity.Id);
                    }
                }
                else
                {
                    await turnContext.StreamingResponse.QueueInformativeUpdateAsync(
                        "Working on your request...",
                        cancellationToken);
                    streamingStarted = true;
                }

                // Execute logic
                await agentService.NewActivityReceived(turnContext, turnState, cancellationToken);
            }
            catch (Exception ex)
            {
                var correlationId = System.Diagnostics.Activity.Current?.TraceId.ToString();
                if (string.IsNullOrWhiteSpace(correlationId))
                    correlationId = Guid.NewGuid().ToString("N");
                _logger.LogError(
                    "Error processing message activity; type={ExceptionType} correlation={CorrelationId}",
                    ex.GetType().Name,
                    correlationId);

                // Never send stack traces, environment values or hosted-session identifiers to
                // the caller. They can contain tenant topology and credentials. The correlation
                // value is safe to give support and joins the user-visible error to server logs.
                var errorText =
                    $"Sorry, something went wrong while processing your message. " +
                    $"Reference: {correlationId}.";

                if (streamingStarted)
                {
                    try
                    {
                        turnContext.StreamingResponse.QueueTextChunk(errorText);
                    }
                    catch (Exception streamEx)
                    {
                        _logger.LogWarning(
                            "Failed to queue error text on streaming response; type={ExceptionType}",
                            streamEx.GetType().Name);
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
                        _logger.LogWarning(
                            "Failed to end streaming response; type={ExceptionType}",
                            streamEx.GetType().Name);
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

        // Microsoft Teams starts the owner conversation with a conversationUpdate/membersAdded
        // activity. This is also the welcome route used by Microsoft's current Agents SDK sample
        // and by our earlier working Teams adapter. Foundry accepted this activity with HTTP 202
        // even when no route was registered, which made the missing welcome look like a delivery
        // failure. Keep installationUpdate above as a fallback for channels that emit it instead.
        OnConversationUpdate(ConversationUpdateEvents.MembersAdded,
            async (turnContext, turnState, cancellationToken) =>
            {
                _logger.LogInformation(
                    "Received membersAdded welcome trigger with {MemberCount} member(s)",
                    turnContext.Activity.MembersAdded?.Count ?? 0);

                var agent = await GetAgentFromRecipient(turnContext.Activity);
                var agentService = await _factory.CreateAsync(agent, turnContext, UserAuthorization);
                await agentService.HandleMembersAddedAsync(turnContext, turnState, cancellationToken);
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
