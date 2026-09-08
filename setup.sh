#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

login_with_azure_cli() {
  info "Microsoft sign-in is required."
  printf '%s\n' \
    "A Microsoft sign-in window will open now." \
    "Choose an administrator account in the customer tenant and complete any MFA prompt."
  az login --allow-no-subscriptions --output none
}

select_customer_tenant() {
  local rows=()
  local row existing_row tenant_id tenant_name account_name choice choice_number other_choice i duplicate

  while true; do
    if ! az account show >/dev/null 2>&1; then
      printf 'Azure CLI is not signed in.\n'
      login_with_azure_cli
    fi

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
    done < <(az account list --all \
      --query "[?state=='Enabled'].join('|', [tenantId, tenantDisplayName, user.name])" \
      --output tsv)

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

main() {
  have curl || fail "curl is required. Install curl and rerun this script."

  install_azure_cli
  install_azd
  select_customer_tenant
  ensure_azure_login
  ensure_azd_login

  export AZURE_TENANT_ID="$CUSTOMER_TENANT_ID"

  info "Authentication is ready for tenant ${CUSTOMER_TENANT_ID}."

  if [[ -f "${SCRIPT_DIR}/azure.yaml" ]]; then
    printf 'Starting the Hermes private-tenant provisioning flow with azd.\n'
    cd "$SCRIPT_DIR"
    azd provision
  else
    printf '%s\n' \
      "The login setup is complete." \
      "No azure.yaml was found beside this script, so no Azure resources were changed."
  fi
}

main "$@"
