namespace HelloWorldA365.Services;

using Azure.Core;
using Azure.Identity;
using Azure.Security.KeyVault.Secrets;
using Microsoft.Identity.Client;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json;

public class AgentTokenHelper(ILogger<AgentTokenHelper> logger, IConfiguration config)
{
    /// <summary>
    /// Performs the three-step agentic user identity token acquisition process using managed identity.
    /// </summary>
    public async Task<string> GetAgenticUserTokenAsync(string agentAppId, string agentAppInstanceId, string userUpn, string tenantId, string[] scopes)
    {
        try
        {
            // FIRST: Get blueprint token via managed identity
            var blueprintToken = await GetBlueprintToken(agentAppId);

            // SECOND: Get AAD token for AgentAppInstanceId
            var instanceApp = ConfidentialClientApplicationBuilder
                .Create(agentAppInstanceId)
                .WithClientAssertion((AssertionRequestOptions _) => Task.FromResult(blueprintToken.Token))
                .WithAuthority(new Uri($"https://login.microsoftonline.com/{tenantId}"))
                .Build();

            var instanceTokenResult = await instanceApp
                .AcquireTokenForClient(["api://AzureAdTokenExchange/.default"])
                .ExecuteAsync();

            // THIRD: Get combined user token
            var userToken = await GetUserFederatedIdentityTokenAsync(
                agentAppInstanceId,
                tenantId,
                blueprintToken.Token,
                instanceTokenResult.AccessToken,
                userUpn,
                scopes);

            return userToken;
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "Error acquiring agentic user token");
            throw;
        }
    }

    /// <summary>
    /// Performs the documented autonomous Agent User flow with a client certificate held by the
    /// Agent Identity Blueprint. This is for turn-less executions such as Foundry routines, where
    /// Identity Proxy Manager cannot select the separately published Agent Identity.
    /// </summary>
    public async Task<string> GetAgenticUserTokenWithBlueprintCertificateAsync(
        string agentAppId,
        string agentAppInstanceId,
        string userId,
        string tenantId,
        string[] scopes)
    {
        var vaultUri = config["RoutineCredentialVaultUri"];
        var certificateName = config["RoutineCredentialCertificateName"];
        if (string.IsNullOrWhiteSpace(vaultUri) || string.IsNullOrWhiteSpace(certificateName))
            throw new InvalidOperationException(
                "Routine blueprint certificate is not configured. Set RoutineCredentialVaultUri and RoutineCredentialCertificateName.");

        try
        {
            using var certificate = await LoadCertificateAsync(vaultUri, certificateName);
            var blueprintApp = ConfidentialClientApplicationBuilder
                .Create(agentAppId)
                .WithCertificate(certificate)
                .WithAuthority(new Uri($"https://login.microsoftonline.com/{tenantId}"))
                .Build();

            // Microsoft Entra Agent ID's autonomous flow requires the published Agent Identity's
            // client ID in fmi_path. This makes the resulting T1 usable only for that identity.
            var blueprintToken = await blueprintApp
                .AcquireTokenForClient(["api://AzureADTokenExchange/.default"])
                .WithFmiPath(agentAppInstanceId)
                .ExecuteAsync();

            return await GetAgenticUserTokenFromBlueprintTokenAsync(
                agentAppInstanceId,
                userId,
                tenantId,
                scopes,
                blueprintToken.AccessToken);
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "Error acquiring certificate-backed agentic user token");
            throw;
        }
    }

    private async Task<AccessToken> GetBlueprintToken(string clientId)
    {
        // Pin managed identity selection to the blueprint client id. An ambient credential can have
        // a subject that Entra does not accept as an agent identity credential.
        var credential = new DefaultAzureCredential(new DefaultAzureCredentialOptions
        {
            ManagedIdentityClientId = clientId,
        });
        return await credential.GetTokenAsync(new TokenRequestContext(["api://AzureADTokenExchange/.default"]));
    }

    private async Task<string> GetAgenticUserTokenFromBlueprintTokenAsync(
        string agentAppInstanceId,
        string userId,
        string tenantId,
        string[] scopes,
        string blueprintToken)
    {
        var instanceApp = ConfidentialClientApplicationBuilder
            .Create(agentAppInstanceId)
            .WithClientAssertion((AssertionRequestOptions _) => Task.FromResult(blueprintToken))
            .WithAuthority(new Uri($"https://login.microsoftonline.com/{tenantId}"))
            .Build();

        var instanceTokenResult = await instanceApp
            .AcquireTokenForClient(["api://AzureADTokenExchange/.default"])
            .ExecuteAsync();

        return await GetUserFederatedIdentityTokenAsync(
            agentAppInstanceId,
            tenantId,
            blueprintToken,
            instanceTokenResult.AccessToken,
            userId,
            scopes);
    }

    private static async Task<X509Certificate2> LoadCertificateAsync(string vaultUri, string certificateName)
    {
        var secretClient = new SecretClient(new Uri(vaultUri), new DefaultAzureCredential());
        var secret = await secretClient.GetSecretAsync(certificateName);
        var pfxBytes = Convert.FromBase64String(secret.Value.Value);
        try
        {
            return X509CertificateLoader.LoadPkcs12(
                pfxBytes,
                password: null,
                X509KeyStorageFlags.EphemeralKeySet);
        }
        finally
        {
            CryptographicOperations.ZeroMemory(pfxBytes);
        }
    }

    private async Task<string> GetUserFederatedIdentityTokenAsync(
        string clientId,
        string tenantId,
        string clientAssertion,
        string userFederatedIdentityCredential,
        string username,
        string[] scopes)
    {
        using var httpClient = new HttpClient();

        var tokenEndpoint = $"https://login.microsoftonline.com/{tenantId}/oauth2/v2.0/token";

        var parameters = new Dictionary<string, string>
        {
            { "client_id", clientId },
            { "scope", string.Join(" ", scopes) },
            { "client_assertion_type", "urn:ietf:params:oauth:client-assertion-type:jwt-bearer" },
            { "client_assertion", clientAssertion },
            { "user_federated_identity_credential", userFederatedIdentityCredential },
            { "grant_type", "user_fic" }
        };

        if (username.Contains('@'))
        {
            parameters["username"] = username;
        }
        else
        {
            parameters["user_id"] = username;
        }

        var content = new FormUrlEncodedContent(parameters);
        var response = await httpClient.PostAsync(tokenEndpoint, content);

        if (!response.IsSuccessStatusCode)
        {
            var errorContent = await response.Content.ReadAsStringAsync();
            throw new InvalidOperationException($"Failed to acquire user federated identity token: {errorContent}");
        }

        var responseContent = await response.Content.ReadAsStringAsync();
        var tokenResponse = JsonSerializer.Deserialize<Dictionary<string, object>>(responseContent);

        if (tokenResponse != null && tokenResponse.TryGetValue("access_token", out var accessToken))
        {
            var token = accessToken?.ToString();
            return token ?? throw new InvalidOperationException("Access token is null");
        }

        throw new InvalidOperationException("Failed to parse access token from response");
    }
}
