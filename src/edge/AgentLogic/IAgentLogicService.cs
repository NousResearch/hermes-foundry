namespace HelloWorldA365.AgentLogic;

using Microsoft.Agents.A365.Notifications.Models;
using Microsoft.Agents.Builder;
using Microsoft.Agents.Builder.State;

public interface IAgentLogicService
{
    /// <summary>
    /// Processes a new message received by the agent.
    /// Returns a simple string response.
    /// This is invoked by background service and is alternate way to process emails that don't leverage SDK activities.
    /// </summary>
    Task<string> NewEmailReceived(string fromEmail, string subject, string messageBody);

    /// <summary>
    /// Processes a new chat message received by the agent.
    /// Returns a simple string response.
    /// This is invoked by background service and is alternate way to process chat messages that don't leverage SDK activities.
    /// </summary>
    Task<string> NewChatReceived(string chatId, string fromUser, string messageBody);

    /// <summary>
    /// Handles email notification events
    /// </summary>
    Task HandleEmailNotificationAsync(ITurnContext turnContext, ITurnState turnState, AgentNotificationActivity emailEvent);

    /// <summary>
    /// Handles document comment notification events (Word, Excel, PowerPoint)
    /// </summary>
    Task HandleCommentNotificationAsync(ITurnContext turnContext, ITurnState turnState, AgentNotificationActivity commentEvent);

    /// <summary>
    /// Handles installation update events
    /// </summary>
    Task HandleInstallationUpdateAsync(ITurnContext turnContext, ITurnState turnState, AgentNotificationActivity installationEvent);

    /// <summary>
    /// Handles the conversation update emitted when a person is added to the agent conversation.
    /// Teams and Agent 365 use this event for the initial owner conversation, so it is the primary
    /// welcome-message trigger. InstallationUpdate remains registered as a channel-dependent fallback.
    /// </summary>
    Task HandleMembersAddedAsync(ITurnContext turnContext, ITurnState turnState, CancellationToken cancellationToken);

    /// <summary>
    /// Handles a standard activity protocol message
    /// </summary>
    /// <returns></returns>
    Task NewActivityReceived(ITurnContext turnContext, ITurnState turnState, CancellationToken cancellationToken);
}
