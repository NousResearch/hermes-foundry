namespace HelloWorldA365.Services;

using Microsoft.Identity.Abstractions;
using Microsoft.Identity.Web;

/// <summary>
/// Acquires downstream tokens as an instance's own Agent User through Entra Agent OBO.
///
/// No human or turn token participates in this flow. The configured Agent Identity Blueprint is
/// the SaaS application's confidential client; <paramref name="agentIdentityId"/> selects one of
/// its child instances, and <paramref name="agentUserId"/> selects that instance's one-to-one Agent
/// User. Microsoft.Identity.Web performs and caches the native User FIC flow.
/// </summary>
public sealed class AgentUserAuthorizationService(
    IAuthorizationHeaderProvider authorizationHeaderProvider,
    ILogger<AgentUserAuthorizationService> logger)
{
    public async Task<string> GetAccessTokenAsync(
        string agentIdentityId,
        string agentUserId,
        string tenantId,
        string[] scopes,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(agentIdentityId))
            throw new ArgumentException("Agent Identity client ID is required.", nameof(agentIdentityId));
        if (string.IsNullOrWhiteSpace(agentUserId))
            throw new ArgumentException("Agent User object ID or UPN is required.", nameof(agentUserId));
        if (string.IsNullOrWhiteSpace(tenantId))
            throw new ArgumentException("Tenant ID is required.", nameof(tenantId));
        if (scopes.Length == 0)
            throw new ArgumentException("At least one downstream scope is required.", nameof(scopes));

        var options = new AuthorizationHeaderProviderOptions
        {
            AcquireTokenOptions = new AcquireTokenOptions
            {
                AuthenticationOptionsName = "AzureAd",
                Tenant = tenantId
            }
        };

        if (Guid.TryParse(agentUserId, out var objectId))
            options.WithAgentUserIdentity(agentIdentityId, objectId);
        else
            options.WithAgentUserIdentity(agentIdentityId, agentUserId);

        var authorization = await authorizationHeaderProvider.CreateAuthorizationHeaderForUserAsync(
            scopes,
            options,
            claimsPrincipal: null,
            cancellationToken);

        const string bearerPrefix = "Bearer ";
        if (!authorization.StartsWith(bearerPrefix, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Entra Agent OBO returned an unsupported authorization scheme.");

        var token = authorization[bearerPrefix.Length..].Trim();
        if (token.Length == 0)
            throw new InvalidOperationException("Entra Agent OBO returned an empty access token.");

        logger.LogInformation("Acquired downstream authorization through Entra Agent OBO");
        return token;
    }
}
