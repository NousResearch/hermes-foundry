#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROVISION_MODE="apply"
FORCE_ARGS=()

case "${1:-}" in
  "") ;;
  --force) FORCE_ARGS=(--force) ;;
  --preview) PROVISION_MODE="preview" ;;
  --validate) PROVISION_MODE="validate" ;;
  --auth-only) PROVISION_MODE="auth-only" ;;
  --continue) PROVISION_MODE="continue" ;;
  --existing) PROVISION_MODE="existing" ;;
  -h|--help)
    printf '%s\n' \
      "Usage: ./setup.sh [--continue|--force|--existing|--preview|--validate|--auth-only]" \
      "  no option    Guided prerequisites, Azure deployment, approval, bot routing, activation and testing." \
      "  --force      Reinstall: reprovision infrastructure, rebuild and activate the app; preserve identity/data." \
      "  --continue   Resume the saved installation at its next required step." \
      "  --existing   Verify/adopt an existing deployment, without provisioning infrastructure." \
      "  --preview    Show the Azure resource plan without creating anything." \
      "  --validate   Authenticate and run read-only Azure prerequisites; create no resources." \
      "  --auth-only  Install tools and authenticate without running azd provisioning."
    exit 0
    ;;
  *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { printf 'Use only one setup option.\n' >&2; exit 2; }

info() {
  printf '\n%s\n' "$*"
}

fail() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

have() {
  command -v "$1" >/dev/null 2>&1
}

download_and_run() {
  local url="$1"
  shift
  local installer_dir installer
  installer_dir="$(mktemp -d)"
  installer="${installer_dir}/installer.sh"
  curl -fsSL "$url" -o "$installer"
  bash "$installer" "$@"
  rm -f "$installer"
  rmdir "$installer_dir"
}

install_homebrew() {
  if have brew; then
    return
  fi

  info "Homebrew is required to install the Azure tools on macOS."
  printf 'The official Homebrew installer will run now and may ask for your macOS password.\n'
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi

  have brew || fail "Homebrew was installed but is not available in PATH. Open a new terminal and rerun this script."
}

install_azure_cli() {
  if have az; then
    printf 'Azure CLI is already installed.\n'
    return
  fi

  info "Azure CLI (az) is not installed. Installing it now."

  case "$(uname -s)" in
    Darwin)
      install_homebrew
      brew install azure-cli
      ;;
    Linux)
      if have apt-get; then
        have curl || fail "curl is required to install Azure CLI."
        printf 'The official Microsoft installer may ask for sudo access.\n'
        download_and_run "https://aka.ms/InstallAzureCLIDeb"
      else
        fail "Automatic Azure CLI installation currently supports macOS and Debian/Ubuntu Linux. Install az from https://learn.microsoft.com/cli/azure/install-azure-cli and rerun this script."
      fi
      ;;
    *)
      fail "This Bash installer supports macOS and Debian/Ubuntu Linux. On Windows, install Azure CLI with 'winget install Microsoft.AzureCLI', then rerun this script in WSL."
      ;;
  esac

  have az || fail "Azure CLI installation did not make 'az' available in PATH. Open a new terminal and rerun this script."
}

install_azd() {
  if have azd; then
    printf 'Azure Developer CLI is already installed.\n'
    return
  fi

  info "Azure Developer CLI (azd) is not installed. Installing it now."

  case "$(uname -s)" in
    Darwin)
      install_homebrew
      brew install azure/azd/azd
      ;;
    Linux)
      have curl || fail "curl is required to install Azure Developer CLI."
      download_and_run "https://aka.ms/install-azd.sh"
      ;;
    *)
      fail "This Bash installer supports macOS and Linux. On Windows, install azd with 'winget install Microsoft.Azd', then rerun this script in WSL."
      ;;
  esac

  have azd || fail "Azure Developer CLI installation did not make 'azd' available in PATH. Open a new terminal and rerun this script."
}

install_support_tools() {
  if have python3 && have openssl && python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
    return
  fi

  info "Installing the local tools required to create the hosted-agent configuration."
  case "$(uname -s)" in
    Darwin)
      install_homebrew
      have python3 || brew install python
      have openssl || brew install openssl
      ;;
    Linux)
      if have apt-get; then
        sudo apt-get update
        sudo apt-get install -y python3 python3-venv openssl
      else
        fail "Install Python 3 and OpenSSL, then rerun this script."
      fi
      ;;
  esac

  have python3 || fail "Python 3 is required but was not installed successfully."
  have openssl || fail "OpenSSL is required but was not installed successfully."
  python3 -c 'import venv, ensurepip' >/dev/null 2>&1 || \
    fail "Python venv/ensurepip support is required for Microsoft sign-in. Install python3-venv (Debian/Ubuntu) or a complete Python installation and retry."
}

login_with_azure_cli() {
  info "Microsoft sign-in is required."
  printf '%s\n' \
    "A Microsoft sign-in window will open now." \
    "Choose an administrator account in the customer tenant and complete any MFA prompt."
  az login --allow-no-subscriptions --output none
}

select_customer_tenant() {
  local rows=()
  local row existing_row tenant_id tenant_name account_name choice choice_number other_choice i duplicate account_rows

  while true; do
    if ! az account show >/dev/null 2>&1; then
      printf 'Azure CLI is not signed in.\n'
      login_with_azure_cli
    fi

    account_rows=$(az account list --all \
      --query "[?state=='Enabled'].join('|', [tenantId, tenantDisplayName || 'Unnamed tenant', user.name || 'Unknown account'])" \
      --output tsv) || fail "Could not list Azure tenants. Check the Azure CLI error above and retry."
    rows=()
    while IFS= read -r row; do
      [[ -n "$row" ]] || continue

      tenant_id="${row%%|*}"
      account_name="${row##*|}"
      duplicate=false
      for existing_row in "${rows[@]:-}"; do
        if [[ "${existing_row%%|*}" == "$tenant_id" && "${existing_row##*|}" == "$account_name" ]]; then
          duplicate=true
          break
        fi
      done
      [[ "$duplicate" == true ]] || rows+=("$row")
    done <<< "$account_rows"

    info "Choose the customer tenant and administrator identity:"
    i=1
    for row in "${rows[@]:-}"; do
      [[ -n "$row" ]] || continue
      tenant_id="${row%%|*}"
      account_name="${row##*|}"
      tenant_name="${row#*|}"
      tenant_name="${tenant_name%|*}"
      [[ -n "$tenant_name" ]] || tenant_name="Unnamed tenant"
      printf '  %d) %s — %s\n' "$i" "$tenant_name" "$account_name"
      i=$((i + 1))
    done

    other_choice="$i"
    printf '  %d) Login into other one\n' "$other_choice"
    read -r -p "Selection: " choice

    if [[ "$choice" =~ ^[0-9]+$ ]]; then
      choice_number=$((10#$choice))
      if (( choice_number == other_choice )); then
        login_with_azure_cli
        continue
      fi

      if (( choice_number >= 1 && choice_number < other_choice )); then
        row="${rows[$((choice_number - 1))]}"
        CUSTOMER_TENANT_ID="${row%%|*}"
        SELECTED_ACCOUNT_NAME="${row##*|}"
        return
      fi
    fi

    printf 'Please choose one of the numbered options.\n' >&2
  done
}

ensure_azure_login() {
  if az account get-access-token \
    --tenant "$CUSTOMER_TENANT_ID" \
    --resource "https://management.azure.com/" \
    --output none >/dev/null 2>&1; then
    printf 'Azure CLI is already signed in to this tenant.\n'
    return
  fi

  info "Azure CLI needs authorization for the selected customer tenant."
  printf 'A Microsoft sign-in window will open now for %s.\n' "$SELECTED_ACCOUNT_NAME"
  az login --tenant "$CUSTOMER_TENANT_ID"

  az account get-access-token \
    --tenant "$CUSTOMER_TENANT_ID" \
    --resource "https://management.azure.com/" \
    --output none >/dev/null 2>&1 || fail "Azure CLI login did not produce a usable token for this tenant."
}

select_customer_subscription() {
  local rows=()
  local row subscription_id subscription_name choice choice_number i account_rows

  account_rows=$(az account list --all \
    --query "[?tenantId=='${CUSTOMER_TENANT_ID}' && state=='Enabled'].join('|', [id, name || 'Unnamed subscription'])" \
    --output tsv) || fail "Could not list Azure subscriptions. Check the Azure CLI error above and retry."
  while IFS= read -r row; do
    [[ -n "$row" ]] && rows+=("$row")
  done <<< "$account_rows"

  ((${#rows[@]} > 0)) || fail "The selected identity has no enabled Azure subscription in this tenant."

  if ((${#rows[@]} == 1)); then
    row="${rows[0]}"
  else
    info "Choose the Azure subscription for Hermes:"
    i=1
    for row in "${rows[@]}"; do
      subscription_id="${row%%|*}"
      subscription_name="${row#*|}"
      printf '  %d) %s — %s\n' "$i" "$subscription_name" "$subscription_id"
      i=$((i + 1))
    done

    while true; do
      read -r -p "Selection: " choice
      if [[ "$choice" =~ ^[0-9]+$ ]]; then
        choice_number=$((10#$choice))
        if ((choice_number >= 1 && choice_number <= ${#rows[@]})); then
          row="${rows[$((choice_number - 1))]}"
          break
        fi
      fi
      printf 'Please choose one of the numbered subscriptions.\n' >&2
    done
  fi

  AZURE_SUBSCRIPTION_ID="${row%%|*}"
  az account set --subscription "$AZURE_SUBSCRIPTION_ID"
  printf 'Azure subscription: %s\n' "${row#*|}"
}

ensure_azd_login() {
  if azd auth token \
    --tenant-id "$CUSTOMER_TENANT_ID" \
    --scope "https://management.azure.com/.default" >/dev/null 2>&1; then
    printf 'Azure Developer CLI is already signed in to this tenant.\n'
    return
  fi

  info "Azure Developer CLI is not signed in to this customer tenant."
  printf '%s\n' \
    "A Microsoft sign-in window will open now." \
    "Sign in with the same customer-tenant administrator account."
  azd auth login --tenant-id "$CUSTOMER_TENANT_ID"

  azd auth token \
    --tenant-id "$CUSTOMER_TENANT_ID" \
    --scope "https://management.azure.com/.default" >/dev/null 2>&1 || \
    fail "Azure Developer CLI login did not produce a usable token for this tenant."
}

prepare_azd_environment() {
  local environment_name="hermes-${CUSTOMER_TENANT_ID%%-*}"
  local principal_id

  principal_id=$(az ad signed-in-user show --query id --output tsv)
  [[ "$principal_id" =~ ^[0-9A-Fa-f-]{36}$ ]] || \
    fail "Could not resolve the selected administrator's Entra object ID."

  if ! azd env select "$environment_name" >/dev/null 2>&1; then
    azd env new "$environment_name" >/dev/null 2>&1
  fi
  azd env set AZURE_SUBSCRIPTION_ID "$AZURE_SUBSCRIPTION_ID" >/dev/null 2>&1
  azd env set AZURE_PRINCIPAL_ID "$principal_id" >/dev/null 2>&1
  if [[ "$PROVISION_MODE" != "auth-only" ]]; then
    python3 scripts/inference_settings.py --save-azd --interactive
  fi
}

main() {
  cd "$SCRIPT_DIR"
  have curl || fail "curl is required. Install curl and rerun this script."

  install_azure_cli
  install_azd
  install_support_tools
  if [[ "$PROVISION_MODE" == "continue" ]]; then
    CUSTOMER_TENANT_ID=$(python3 scripts/tenant_setup.py --field tenant)
    AZURE_SUBSCRIPTION_ID=$(python3 scripts/tenant_setup.py --field subscription)
    SELECTED_ACCOUNT_NAME="an administrator in the saved customer tenant"
    ensure_azure_login
    az account set --subscription "$AZURE_SUBSCRIPTION_ID"
  else
    if [[ "$PROVISION_MODE" =~ ^(apply|existing)$ && -f deployment-output/setup-state.json && ${#FORCE_ARGS[@]} -eq 0 ]]; then
      fail "A saved installation exists. Run ./deploy.sh --continue to resume, or ./deploy.sh --force to archive its checkpoint and restart setup. --force rebuilds and activates the app in the selected environment while preserving identity/data."
    fi
    select_customer_tenant
    ensure_azure_login
    select_customer_subscription
  fi
  ensure_azd_login

  export AZURE_TENANT_ID="$CUSTOMER_TENANT_ID"
  export AZURE_SUBSCRIPTION_ID
  export AZD_SKIP_FIRST_RUN=true
  # azd executes preprovision hooks even while preparing some previews. Pass the requested mode to
  # every hook so --preview cannot quietly register providers or perform another tenant mutation.
  export HERMES_PROVISION_MODE="$PROVISION_MODE"

  info "Authentication is ready for tenant ${CUSTOMER_TENANT_ID}."

  if [[ "$PROVISION_MODE" == "validate" ]]; then
    validate_args=(
      --tenant "$CUSTOMER_TENANT_ID"
      --subscription "$AZURE_SUBSCRIPTION_ID"
    )
    if [[ -n "${AZURE_LOCATION:-}" ]]; then
      validate_args+=(--location "$AZURE_LOCATION")
    fi
    "${SCRIPT_DIR}/scripts/validate-installation.sh" "${validate_args[@]}"
    return
  fi

  if [[ "$PROVISION_MODE" == "continue" ]]; then
    environment_name=$(python3 scripts/tenant_setup.py --field environment)
    azd env select "$environment_name"
    export HERMES_PROVISION_MODE="apply"
  elif [[ "$PROVISION_MODE" == "existing" ]]; then
    read -r -p "Existing azd environment name (see 'azd env list'): " environment_name
    [[ "$environment_name" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || fail "Invalid environment name."
    azd env select "$environment_name"
  else
    prepare_azd_environment
    environment_name="hermes-${CUSTOMER_TENANT_ID%%-*}"
  fi

  if [[ "$PROVISION_MODE" == "auth-only" ]]; then
    printf 'Authentication-only setup is complete. No Azure resources were changed.\n'
  elif [[ -f "${SCRIPT_DIR}/azure.yaml" ]]; then
    cd "$SCRIPT_DIR"
    if [[ "$PROVISION_MODE" == "preview" ]]; then
      printf 'Generating a read-only preview of the Hermes Azure resources.\n'
      azd provision --preview
      return
    fi
    printf 'Starting the Hermes guided installation workflow.\n'
    if [[ "$PROVISION_MODE" == "continue" ]]; then
      python3 scripts/tenant_setup.py --continue
    elif [[ "$PROVISION_MODE" == "existing" ]]; then
      python3 scripts/tenant_setup.py --existing --tenant "$CUSTOMER_TENANT_ID" \
        --subscription "$AZURE_SUBSCRIPTION_ID" --environment "$environment_name"
    else
      python3 scripts/tenant_setup.py "${FORCE_ARGS[@]}" --tenant "$CUSTOMER_TENANT_ID" \
        --subscription "$AZURE_SUBSCRIPTION_ID" --environment "$environment_name"
    fi
  else
    printf '%s\n' \
      "The login setup is complete." \
      "No azure.yaml was found beside this script, so no Azure resources were changed."
  fi
}

main "$@"
