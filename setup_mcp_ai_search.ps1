<#
.SYNOPSIS
    Sets up Azure MCP Server to connect to Azure AI Search indexes.

.DESCRIPTION
    This script configures the environment for Azure MCP Server to authenticate
    to Azure AI Search data plane operations. It handles:
    - Azure CLI login to the correct tenant
    - Setting AZURE_TOKEN_CREDENTIALS so the MCP server uses AzureCliCredential
    - Setting @azure.argTenant in VS Code settings for correct token issuer
    - Enabling RBAC (aadOrApiKey) on the AI Search service
    - Assigning required RBAC roles (Search Index Data Reader, Search Service Contributor)
    - Verifying connectivity by listing indexes via MCP CLI

.PARAMETER TenantId
    The Microsoft Entra ID tenant ID. Required.

.PARAMETER SubscriptionId
    The Azure subscription ID. Required.

.PARAMETER SearchServiceName
    The name of the Azure AI Search service (e.g., "slot-mapping-ai-search"). Required.

.PARAMETER ResourceGroupName
    The resource group containing the AI Search service. Required.

.PARAMETER IndexNames
    Array of index names to verify access to. Optional - if omitted, lists all indexes.

.PARAMETER UserObjectId
    The Entra ID object ID of the user to assign RBAC roles to.
    If omitted, uses the currently signed-in user.

.PARAMETER SkipRbacSetup
    Skip enabling RBAC on the search service and assigning roles.

.PARAMETER SkipVSCodeSettings
    Skip updating VS Code user settings.

.EXAMPLE
    # Full setup for slot-mapping-ai-search
    .\setup_mcp_ai_search.ps1 `
        -TenantId "16b3c013-d300-468d-ac64-7eda0820b6d3" `
        -SubscriptionId "3ee7aaf1-0b4c-423c-9ed7-48beadbcdc85" `
        -SearchServiceName "slot-mapping-ai-search" `
        -ResourceGroupName "rg-genaiops-dev" `
        -IndexNames @("group-slot-mapping-index", "user-slot-mapping-index")

.EXAMPLE
    # Quick setup (skip RBAC, just configure credentials)
    .\setup_mcp_ai_search.ps1 `
        -TenantId "16b3c013-d300-468d-ac64-7eda0820b6d3" `
        -SubscriptionId "3ee7aaf1-0b4c-423c-9ed7-48beadbcdc85" `
        -SearchServiceName "slot-mapping-ai-search" `
        -ResourceGroupName "rg-genaiops-dev" `
        -SkipRbacSetup

.NOTES
    Prerequisites:
    - Azure CLI installed and available in PATH
    - VS Code with Azure MCP Server extension installed
    - User must have permissions to assign RBAC roles (unless -SkipRbacSetup)

    Background:
    The Azure MCP Server uses a credential chain (DefaultAzureCredential-like) to
    authenticate. For AI Search data plane operations, it needs a token with audience
    "https://search.azure.com/user_impersonation" (API ID: 880da380-985e-4198-81b9-e05b1cc53158).

    In cross-tenant or guest-user scenarios, the default credential chain may pick a
    credential (e.g., VisualStudioCodeCredential or broker) that produces a token with
    the wrong issuer/audience, resulting in a 401 "invalid_token" error.

    The fix is to set AZURE_TOKEN_CREDENTIALS=AzureCliCredential to force the MCP server
    to use Azure CLI credentials, which correctly acquire tokens scoped to the AI Search
    data plane.

    Reference:
    - API Permissions: https://github.com/microsoft/mcp/blob/main/servers/Azure.Mcp.Server/azd-templates/api-permissions.md
    - Troubleshooting: https://github.com/microsoft/mcp/blob/main/servers/Azure.Mcp.Server/TROUBLESHOOTING.md
    - Authentication: https://github.com/microsoft/mcp/blob/main/docs/Authentication.md
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TenantId,

    [Parameter(Mandatory = $true)]
    [string]$SubscriptionId,

    [Parameter(Mandatory = $true)]
    [string]$SearchServiceName,

    [Parameter(Mandatory = $true)]
    [string]$ResourceGroupName,

    [Parameter(Mandatory = $false)]
    [string[]]$IndexNames,

    [Parameter(Mandatory = $false)]
    [string]$UserObjectId,

    [switch]$SkipRbacSetup,

    [switch]$SkipVSCodeSettings
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "`n========================================" -ForegroundColor Cyan
    Write-Host " $Message" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
}

function Write-Success {
    param([string]$Message)
    Write-Host "[OK] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Write-Fail {
    param([string]$Message)
    Write-Host "[FAIL] $Message" -ForegroundColor Red
}

# ============================================================
# Step 1: Verify prerequisites
# ============================================================
Write-Step "Step 1: Verifying prerequisites"

# Check Azure CLI
try {
    $azVersion = az version 2>&1 | ConvertFrom-Json
    Write-Success "Azure CLI found (version $($azVersion.'azure-cli'))"
} catch {
    Write-Fail "Azure CLI not found. Install from https://aka.ms/installazurecliwindows"
    exit 1
}

# Check MCP Server extension
$mcpExtPath = Get-ChildItem "$env:USERPROFILE\.vscode\extensions\" -Filter "ms-azuretools.vscode-azure-mcp-server-*" -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
if ($mcpExtPath) {
    $azmcpExe = Join-Path $mcpExtPath.FullName "server\azmcp.exe"
    if (Test-Path $azmcpExe) {
        $mcpVersion = & $azmcpExe --version 2>&1
        Write-Success "Azure MCP Server found: $mcpVersion"
    } else {
        Write-Warn "MCP extension found but binary missing at: $azmcpExe"
        $azmcpExe = $null
    }
} else {
    Write-Warn "Azure MCP Server VS Code extension not found. MCP verification will be skipped."
    $azmcpExe = $null
}

# ============================================================
# Step 2: Azure CLI login
# ============================================================
Write-Step "Step 2: Ensuring Azure CLI login to tenant $TenantId"

$currentAccount = az account show --query "{tenantId:tenantId, subscriptionId:id}" 2>&1 | ConvertFrom-Json -ErrorAction SilentlyContinue
if ($currentAccount -and $currentAccount.tenantId -eq $TenantId -and $currentAccount.subscriptionId -eq $SubscriptionId) {
    Write-Success "Already logged in to correct tenant and subscription"
} else {
    Write-Host "Logging in to tenant $TenantId..."
    az login --tenant $TenantId 2>&1 | Out-Null
    az account set --subscription $SubscriptionId 2>&1
    Write-Success "Logged in and subscription set to $SubscriptionId"
}

# ============================================================
# Step 3: Set AZURE_TOKEN_CREDENTIALS environment variable
# ============================================================
Write-Step "Step 3: Setting AZURE_TOKEN_CREDENTIALS=AzureCliCredential"

$currentVal = [System.Environment]::GetEnvironmentVariable("AZURE_TOKEN_CREDENTIALS", "User")
if ($currentVal -eq "AzureCliCredential") {
    Write-Success "AZURE_TOKEN_CREDENTIALS already set to AzureCliCredential (user-level)"
} else {
    [System.Environment]::SetEnvironmentVariable("AZURE_TOKEN_CREDENTIALS", "AzureCliCredential", "User")
    Write-Success "Set AZURE_TOKEN_CREDENTIALS=AzureCliCredential (user-level, persistent)"
}
$env:AZURE_TOKEN_CREDENTIALS = "AzureCliCredential"
$env:AZURE_TENANT_ID = $TenantId

# ============================================================
# Step 4: Set @azure.argTenant in VS Code settings
# ============================================================
if (-not $SkipVSCodeSettings) {
    Write-Step "Step 4: Configuring VS Code settings (@azure.argTenant)"

    $vsCodeSettingsPath = "$env:APPDATA\Code\User\settings.json"
    if (Test-Path $vsCodeSettingsPath) {
        $settings = Get-Content $vsCodeSettingsPath -Raw | ConvertFrom-Json
        if ($settings.'@azure.argTenant' -eq $TenantId) {
            Write-Success "@azure.argTenant already set to $TenantId"
        } else {
            $settings | Add-Member -NotePropertyName "@azure.argTenant" -NotePropertyValue $TenantId -Force
            $settings | ConvertTo-Json -Depth 10 | Set-Content $vsCodeSettingsPath -Encoding UTF8
            Write-Success "Set @azure.argTenant=$TenantId in VS Code settings"
        }
    } else {
        Write-Warn "VS Code settings file not found at $vsCodeSettingsPath"
    }
} else {
    Write-Step "Step 4: Skipping VS Code settings (--SkipVSCodeSettings)"
}

# ============================================================
# Step 5: Enable RBAC on AI Search service
# ============================================================
if (-not $SkipRbacSetup) {
    Write-Step "Step 5: Enabling RBAC on AI Search service '$SearchServiceName'"

    # Enable aadOrApiKey auth
    Write-Host "Setting auth options to aadOrApiKey with bearer challenge..."
    az search service update `
        --name $SearchServiceName `
        --resource-group $ResourceGroupName `
        --subscription $SubscriptionId `
        --auth-options aadOrApiKey `
        --aad-auth-failure-mode http401WithBearerChallenge 2>&1 | Out-Null
    Write-Success "RBAC enabled (aadOrApiKey mode with http401WithBearerChallenge)"

    # Determine user object ID
    if (-not $UserObjectId) {
        Write-Host "Detecting current user object ID..."
        $UserObjectId = az ad signed-in-user show --query id -o tsv 2>&1
        if ($LASTEXITCODE -ne 0) {
            # Guest user fallback — try getting from account
            $userUpn = az account show --query user.name -o tsv 2>&1
            $UserObjectId = az ad user show --id $userUpn --query id -o tsv 2>&1
        }
        Write-Success "User object ID: $UserObjectId"
    }

    # Assign RBAC roles
    $searchScope = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName/providers/Microsoft.Search/searchServices/$SearchServiceName"

    $roles = @(
        @{ Name = "Search Index Data Reader"; Id = "1407120a-92aa-4202-b7e9-c0e197c71c8f" }
        @{ Name = "Search Service Contributor"; Id = "7ca78c08-252a-4471-8644-bb5ff32d4ba0" }
    )

    foreach ($role in $roles) {
        Write-Host "Assigning '$($role.Name)' role..."
        $existing = az role assignment list `
            --assignee $UserObjectId `
            --role $role.Id `
            --scope $searchScope `
            --query "length(@)" -o tsv 2>&1
        if ($existing -gt 0) {
            Write-Success "'$($role.Name)' already assigned"
        } else {
            az role assignment create `
                --assignee $UserObjectId `
                --role $role.Id `
                --scope $searchScope 2>&1 | Out-Null
            Write-Success "'$($role.Name)' assigned to $UserObjectId"
        }
    }
} else {
    Write-Step "Step 5: Skipping RBAC setup (--SkipRbacSetup)"
}

# ============================================================
# Step 6: Verify MCP AI Search connectivity
# ============================================================
Write-Step "Step 6: Verifying MCP AI Search connectivity"

if ($azmcpExe) {
    # Test listing all indexes
    Write-Host "Listing indexes via MCP CLI..."
    $result = & $azmcpExe search index get `
        --service $SearchServiceName `
        --tenant $TenantId `
        --auth-method Credential 2>&1 | Out-String

    if ($result -match '"status": 200') {
        Write-Success "MCP data plane authentication successful!"

        # Extract and display index names
        $indexNames_found = [regex]::Matches($result, '"name":\s*"([^"]+)"') |
            Where-Object { $_.Groups[1].Value -notmatch '(Edm\.|_whole|_edge|_normalized|Vector|Embedding|All)' -and
                           $_.Groups[1].Value -match '-index$' } |
            ForEach-Object { $_.Groups[1].Value } |
            Select-Object -Unique
        Write-Host "  Indexes found:" -ForegroundColor White
        foreach ($idx in $indexNames_found) {
            $marker = if ($IndexNames -and $idx -in $IndexNames) { " [TARGET]" } else { "" }
            Write-Host "    - $idx$marker" -ForegroundColor White
        }

        # Verify specific target indexes
        if ($IndexNames) {
            Write-Host ""
            foreach ($targetIdx in $IndexNames) {
                Write-Host "Verifying index: $targetIdx..."
                $idxResult = & $azmcpExe search index get `
                    --service $SearchServiceName `
                    --tenant $TenantId `
                    --auth-method Credential `
                    --index $targetIdx 2>&1 | Out-String
                if ($idxResult -match '"status": 200') {
                    Write-Success "Index '$targetIdx' accessible"
                } else {
                    Write-Fail "Index '$targetIdx' not accessible"
                }
            }
        }
    } else {
        Write-Fail "MCP data plane authentication failed!"
        Write-Host $result
        Write-Host "`nTroubleshooting:" -ForegroundColor Yellow
        Write-Host "  1. Ensure 'az login --tenant $TenantId' is current" -ForegroundColor Yellow
        Write-Host "  2. Verify RBAC roles are assigned (may take a few minutes to propagate)" -ForegroundColor Yellow
        Write-Host "  3. Check: https://github.com/microsoft/mcp/blob/main/servers/Azure.Mcp.Server/TROUBLESHOOTING.md" -ForegroundColor Yellow
        exit 1
    }
} else {
    # Fallback: verify via Azure CLI REST call
    Write-Host "MCP binary not found, verifying via Azure CLI REST call..."
    $token = az account get-access-token --resource "https://search.azure.com" --tenant $TenantId --query accessToken -o tsv 2>&1
    $headers = @{ "Authorization" = "Bearer $token"; "Content-Type" = "application/json" }
    try {
        $response = Invoke-RestMethod -Uri "https://$SearchServiceName.search.windows.net/indexes?api-version=2024-07-01&`$select=name" -Headers $headers
        Write-Success "AI Search data plane accessible via Entra ID"
        foreach ($idx in $response.value) {
            $marker = if ($IndexNames -and $idx.name -in $IndexNames) { " [TARGET]" } else { "" }
            Write-Host "    - $($idx.name)$marker" -ForegroundColor White
        }
    } catch {
        Write-Fail "AI Search data plane access failed: $_"
        exit 1
    }
}

# ============================================================
# Summary
# ============================================================
Write-Step "Setup Complete!"
Write-Host @"

  Configuration Summary:
  ----------------------
  Tenant:              $TenantId
  Subscription:        $SubscriptionId
  Search Service:      $SearchServiceName
  Resource Group:      $ResourceGroupName
  Credential Mode:     AzureCliCredential
  VS Code argTenant:   $TenantId

  Environment Variable (persistent, user-level):
    AZURE_TOKEN_CREDENTIALS = AzureCliCredential

  IMPORTANT: Restart VS Code for the MCP extension to pick up the
  environment variable change.

  After restart, you can use the Azure MCP AI Search tools in
  GitHub Copilot Agent mode to query your indexes:
    "What indexes do I have in my AI Search service '$SearchServiceName'?"
    "Search the '$($IndexNames[0])' index for 'test query'"

  References:
  - API Permissions:  https://github.com/microsoft/mcp/blob/main/servers/Azure.Mcp.Server/azd-templates/api-permissions.md
  - Troubleshooting:  https://github.com/microsoft/mcp/blob/main/servers/Azure.Mcp.Server/TROUBLESHOOTING.md
  - Authentication:   https://github.com/microsoft/mcp/blob/main/docs/Authentication.md
  - Python quickstart: https://learn.microsoft.com/en-us/azure/developer/azure-mcp-server/get-started/languages/python
"@
