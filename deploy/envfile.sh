# Shared writers for /srv/qos/deploy.env and /srv/qos/terminal.env.
# Sourced by deploy/install.sh and deploy/terminal-setup.sh. No secrets are printed.
upsert_env() {
  local file="$1" key="$2" val="$3" tmp
  [[ -n "$key" ]] || return 0
  mkdir -p "$(dirname "$file")"
  [[ -f "$file" ]] || : > "$file"
  tmp=$(mktemp)
  awk -v key="$key" -v val="$val" '
    BEGIN { prefix = key "="; found = 0 }
    index($0, prefix) == 1 { if (!found) print prefix val; found = 1; next }
    { print }
    END { if (!found) print prefix val }
  ' "$file" > "$tmp"
  mv "$tmp" "$file"
}

# Public web Terminal. DOMAIN is the hostname Caddy serves (terminal.example.com).
# The API reads QOS_TRUSTED_ORIGINS from deploy.env. The Terminal reads the same origin
# and QOS_TERMINAL_HOST from terminal.env (Caddy forwards that Host on GET /).
write_terminal_host_env() {
  local deploy="$1" terminal="$2" domain="$3"
  [[ -n "$domain" ]] || return 0
  upsert_env "$deploy" QOS_PUBLIC_TERMINAL 1
  upsert_env "$deploy" QOS_TRUSTED_ORIGINS "https://$domain"
  upsert_env "$terminal" QOS_TRUSTED_ORIGINS "https://$domain"
  upsert_env "$terminal" QOS_TERMINAL_HOST "$domain"
}

# Optional override from the installer. Empty means "leave the file unchanged".
persist_local_token_file() {
  local deploy="$1" path="$2"
  [[ -n "$path" ]] || return 0
  upsert_env "$deploy" QOS_LOCAL_TOKEN_FILE "$path"
}
