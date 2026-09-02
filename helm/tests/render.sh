#!/usr/bin/env bash
set -euo pipefail

# render.sh — helm-template regression matrix for the hermes-agent chart.
#
# Nothing else in CI executes the Helm template engine: run-linter checks
# black/pyright/NOTICE, run-test-docker runs pytest, and the helm-chart
# publish job runs `helm package`, which validates Chart.yaml metadata but
# never renders a single template. A Go-template syntax error, or a values
# combination that silently renders a Secret/ConfigMap mount that nothing
# creates, passes PR CI untouched and ends up published to the OCI registry.
#
# The chart's auth gating (helm/templates/_helpers.tpl: hermes.auth.*) compares
# three credential sources — ExternalSecret remoteRef, AWS Secrets Manager,
# and a manually created Kubernetes Secret — at eight call sites across four
# templates (deployment, token/oauth ExternalSecret, metrics/logs collector
# DaemonSets). The failure mode is silent: `helm install` succeeds while a
# DaemonSet or Deployment mounts a Secret that will never exist, and the pod
# just sits in CrashLoopBackOff/ContainerCreating. This matrix was previously
# re-run by hand on every chart change; this script makes it repeatable and
# makes CI fail before a bad chart is published.
#
# Usage: ./helm/tests/render.sh (run from anywhere; paths are resolved
# relative to this script).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${CHART_DIR}/.." && pwd)"
RELEASE_NAME="hermes-render-test"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

PASS_COUNT=0
FAIL_COUNT=0

# --- output helpers --------------------------------------------------------

pass() {
  local name="$1"
  PASS_COUNT=$((PASS_COUNT + 1))
  echo "[PASS] ${name}"
}

fail() {
  local name="$1" expectation="$2" output="$3"
  FAIL_COUNT=$((FAIL_COUNT + 1))
  echo "[FAIL] ${name}"
  echo "  expected: ${expectation}"
  echo "  --- relevant output ---"
  echo "${output}" | sed 's/^/    /'
  echo "  -----------------------"
}

# --- rendering --------------------------------------------------------------

# render <values-file>
# Renders the chart with the shared baseline values plus the case-specific
# values file, and sets RENDER_OUT / RENDER_RC. Never aborts the script on a
# non-zero helm exit (negative cases expect that).
render() {
  local values_file="$1"
  set +e
  RENDER_OUT="$(helm template "${RELEASE_NAME}" "${CHART_DIR}" -f "${BASE_VALUES}" -f "${values_file}" 2>&1)"
  RENDER_RC=$?
  set -e
}

# assert_success <case-name> <values-file> [--present PATTERN]... [--absent PATTERN]...
# Renders successfully, then asserts each --present pattern is a substring of
# the output and each --absent pattern is not.
assert_success() {
  local name="$1" values_file="$2"
  shift 2
  local -a present=()
  local -a absent=()
  local mode="present"
  local arg
  for arg in "$@"; do
    case "${arg}" in
      --present) mode="present" ;;
      --absent) mode="absent" ;;
      *)
        if [[ "${mode}" == "present" ]]; then
          present+=("${arg}")
        else
          absent+=("${arg}")
        fi
        ;;
    esac
  done

  render "${values_file}"
  if [[ ${RENDER_RC} -ne 0 ]]; then
    fail "${name}" "helm template to succeed" "${RENDER_OUT}"
    return
  fi

  local -a problems=()
  local pattern
  for pattern in "${present[@]}"; do
    if ! grep -qF -- "${pattern}" <<<"${RENDER_OUT}"; then
      problems+=("missing expected substring: ${pattern}")
    fi
  done
  for pattern in "${absent[@]}"; do
    if grep -qF -- "${pattern}" <<<"${RENDER_OUT}"; then
      problems+=("found forbidden substring: ${pattern}")
    fi
  done

  if [[ ${#problems[@]} -eq 0 ]]; then
    pass "${name}"
  else
    fail "${name}" "$(printf '%s; ' "${problems[@]}")" "${RENDER_OUT}"
  fi
}

# assert_failure <case-name> <values-file> <expected-substring>
# Renders expecting a non-zero exit, and asserts the error output contains
# the expected substring.
assert_failure() {
  local name="$1" values_file="$2" expected="$3"
  render "${values_file}"
  if [[ ${RENDER_RC} -eq 0 ]]; then
    fail "${name}" "helm template to fail, containing: ${expected}" "${RENDER_OUT}"
    return
  fi
  if grep -qF -- "${expected}" <<<"${RENDER_OUT}"; then
    pass "${name}"
  else
    fail "${name}" "error output to contain: ${expected}" "${RENDER_OUT}"
  fi
}

# lint_and_template <path-to-values-file>
# Runs `helm lint` and `helm template` against a real environment values
# file, expecting both to succeed.
lint_and_template() {
  local values_file="$1"
  local rel_path="${values_file#"${REPO_ROOT}"/}"

  set +e
  local lint_out
  lint_out="$(helm lint "${CHART_DIR}" -f "${values_file}" 2>&1)"
  local lint_rc=$?
  set -e
  if [[ ${lint_rc} -ne 0 ]]; then
    fail "helm lint: ${rel_path}" "helm lint to succeed" "${lint_out}"
  else
    pass "helm lint: ${rel_path}"
  fi

  set +e
  local template_out
  template_out="$(helm template "${RELEASE_NAME}" "${CHART_DIR}" -f "${values_file}" 2>&1)"
  local template_rc=$?
  set -e
  if [[ ${template_rc} -ne 0 ]]; then
    fail "helm template: ${rel_path}" "helm template to succeed" "${template_out}"
  else
    pass "helm template: ${rel_path}"
  fi
}

# --- baseline values ---------------------------------------------------------
# Every case needs the chart's required values. logShipping and the metrics
# collector default off so most cases render the smallest possible manifest;
# the collector cases below turn metricsCollector back on explicitly.

BASE_VALUES="${TMP_DIR}/base.yaml"
cat >"${BASE_VALUES}" <<'EOF'
container:
  backendServiceUrl: https://artemis.example.com
  storageBucketName: test-bucket
  storageType: S3
logShipping: none
metricsCollector:
  enabled: false
EOF

# --- positive cases -----------------------------------------------------------

KEY_TOKEN_ESO="${TMP_DIR}/key_token_eso.yaml"
cat >"${KEY_TOKEN_ESO}" <<'EOF'
secretStore:
  provider:
    aws:
      role: arn:aws:iam::123456789012:role/example
      region: us-east-1
      service: SecretsManager
tokenSecret:
  remoteRef:
    key: example-secret
EOF
assert_success "key/token via ExternalSecret" "${KEY_TOKEN_ESO}" \
  --present "MCD_TOKEN_FILE_PATH" "kind: ExternalSecret" "name: mcd-agent-token-secret" \
  --absent "MCD_OAUTH_FILE_PATH" "mcd-oauth-secret"

OAUTH_ESO="${TMP_DIR}/oauth_eso.yaml"
cat >"${OAUTH_ESO}" <<'EOF'
secretStore:
  provider:
    aws:
      role: arn:aws:iam::123456789012:role/example
      region: us-east-1
      service: SecretsManager
oauthSecret:
  enabled: true
  remoteRef:
    key: example-oauth-secret
EOF
assert_success "OAuth via ExternalSecret" "${OAUTH_ESO}" \
  --present "MCD_OAUTH_FILE_PATH" "mcd-oauth-secret" \
  --absent "MCD_TOKEN_FILE_PATH"

KEY_TOKEN_ASM="${TMP_DIR}/key_token_asm.yaml"
cat >"${KEY_TOKEN_ASM}" <<'EOF'
skipExternalSecrets: true
tokenSecret:
  awsSecretsManager:
    secretId: example-secret-id
EOF
assert_success "key/token via AWS Secrets Manager" "${KEY_TOKEN_ASM}" \
  --present "MCD_TOKEN_AWS_SECRET_ID" \
  --absent "MCD_TOKEN_FILE_PATH" "secretName: mcd-agent-token-secret" "kind: ExternalSecret"

OAUTH_ASM="${TMP_DIR}/oauth_asm.yaml"
cat >"${OAUTH_ASM}" <<'EOF'
skipExternalSecrets: true
oauthSecret:
  enabled: true
  awsSecretsManager:
    secretId: example-oauth-secret-id
EOF
assert_success "OAuth via AWS Secrets Manager" "${OAUTH_ASM}" \
  --present "MCD_OAUTH_AWS_SECRET_ID" \
  --absent "MCD_OAUTH_FILE_PATH" "secretName: mcd-oauth-secret"

ASM_WITH_REGION="${TMP_DIR}/asm_with_region.yaml"
cat >"${ASM_WITH_REGION}" <<'EOF'
skipExternalSecrets: true
tokenSecret:
  awsSecretsManager:
    secretId: example-secret-id
    region: us-east-1
EOF
assert_success "ASM with a region" "${ASM_WITH_REGION}" \
  --present "MCD_AWS_SECRETS_MANAGER_REGION"

ASM_WITHOUT_REGION="${TMP_DIR}/asm_without_region.yaml"
cat >"${ASM_WITHOUT_REGION}" <<'EOF'
skipExternalSecrets: true
tokenSecret:
  awsSecretsManager:
    secretId: example-secret-id
EOF
assert_success "ASM without a region" "${ASM_WITHOUT_REGION}" \
  --absent "MCD_AWS_SECRETS_MANAGER_REGION"

MANUAL_KEY_TOKEN="${TMP_DIR}/manual_key_token.yaml"
cat >"${MANUAL_KEY_TOKEN}" <<'EOF'
skipExternalSecrets: true
EOF
assert_success "manual key/token" "${MANUAL_KEY_TOKEN}" \
  --present "MCD_TOKEN_FILE_PATH" "secretName: mcd-agent-token-secret" \
  --absent "kind: ExternalSecret"

MANUAL_OAUTH="${TMP_DIR}/manual_oauth.yaml"
cat >"${MANUAL_OAUTH}" <<'EOF'
skipExternalSecrets: true
oauthSecret:
  enabled: true
EOF
assert_success "manual OAuth" "${MANUAL_OAUTH}" \
  --present "MCD_OAUTH_FILE_PATH" "secretName: mcd-oauth-secret" \
  --absent "kind: ExternalSecret"

EMPTY_OAUTH_BLOCK="${TMP_DIR}/empty_oauth_block.yaml"
cat >"${EMPTY_OAUTH_BLOCK}" <<'EOF'
skipExternalSecrets: true
oauthSecret: {}
EOF
assert_success "empty oauthSecret block falls back to key/token" "${EMPTY_OAUTH_BLOCK}" \
  --present "MCD_TOKEN_FILE_PATH" \
  --absent "MCD_OAUTH_FILE_PATH"

NAMESPACE_DEFAULT="${TMP_DIR}/namespace_default.yaml"
cat >"${NAMESPACE_DEFAULT}" <<'EOF'
skipExternalSecrets: true
EOF
assert_success "namespaceCreate default" "${NAMESPACE_DEFAULT}" \
  --present "kind: Namespace"

NAMESPACE_FALSE="${TMP_DIR}/namespace_false.yaml"
cat >"${NAMESPACE_FALSE}" <<'EOF'
skipExternalSecrets: true
namespaceCreate: false
EOF
assert_success "namespaceCreate: false" "${NAMESPACE_FALSE}" \
  --absent "kind: Namespace"

COLLECTORS_ASM="${TMP_DIR}/collectors_asm.yaml"
cat >"${COLLECTORS_ASM}" <<'EOF'
skipExternalSecrets: true
metricsCollector:
  enabled: true
tokenSecret:
  awsSecretsManager:
    secretId: example-secret-id
EOF
assert_failure "collectors + ASM: metrics collector rejected" "${COLLECTORS_ASM}" \
  "metricsCollector.enabled is true but the agent credential comes from AWS Secrets Manager"

COLLECTORS_OAUTH="${TMP_DIR}/collectors_oauth.yaml"
cat >"${COLLECTORS_OAUTH}" <<'EOF'
skipExternalSecrets: true
metricsCollector:
  enabled: true
oauthSecret:
  enabled: true
EOF
# Pins the pre-existing OAuth behaviour rather than endorsing it: the collectors
# mount mcd-agent-token-secret, which OAuth never creates, so this release
# renders a DaemonSet whose pods cannot start. Failing it here would break
# existing OAuth releases on upgrade, so it is tracked separately — this case
# exists so that whoever fixes it sees this assertion flip and updates it
# deliberately.
assert_success "collectors + OAuth: DaemonSet still renders (known, tracked)" "${COLLECTORS_OAUTH}" \
  --present "kind: DaemonSet" "secretName: mcd-agent-token-secret"

COLLECTORS_KEY_TOKEN="${TMP_DIR}/collectors_key_token.yaml"
cat >"${COLLECTORS_KEY_TOKEN}" <<'EOF'
skipExternalSecrets: true
metricsCollector:
  enabled: true
EOF
assert_success "collectors + key/token file: DaemonSet renders" "${COLLECTORS_KEY_TOKEN}" \
  --present "kind: DaemonSet" "secretName: mcd-agent-token-secret"

# --- negative cases -----------------------------------------------------------

BOTH_METHODS="${TMP_DIR}/both_methods.yaml"
cat >"${BOTH_METHODS}" <<'EOF'
oauthSecret:
  enabled: true
  remoteRef:
    key: example-oauth-secret
tokenSecret:
  remoteRef:
    key: example-secret
EOF
assert_failure "oauthSecret and tokenSecret both configured" "${BOTH_METHODS}" \
  "one authentication method at a time"

DISABLED_WITH_SOURCE="${TMP_DIR}/disabled_with_source.yaml"
cat >"${DISABLED_WITH_SOURCE}" <<'EOF'
oauthSecret:
  enabled: false
  remoteRef:
    key: example-oauth-secret
EOF
assert_failure "oauthSecret.enabled: false with a source configured" "${DISABLED_WITH_SOURCE}" \
  "enabled is false"

TOKEN_ENDPOINT_NOT_HTTPS="${TMP_DIR}/token_endpoint_not_https.yaml"
cat >"${TOKEN_ENDPOINT_NOT_HTTPS}" <<'EOF'
oauthSecret:
  enabled: true
  tokenEndpoint: "http://example.com/token"
EOF
assert_failure "oauthSecret.tokenEndpoint not HTTPS" "${TOKEN_ENDPOINT_NOT_HTTPS}" \
  "must use HTTPS"

NO_CREDENTIAL_SOURCE="${TMP_DIR}/no_credential_source.yaml"
cat >"${NO_CREDENTIAL_SOURCE}" <<'EOF'
EOF
assert_failure "ESO path with no auth configured at all" "${NO_CREDENTIAL_SOURCE}" \
  "no credential source is configured"

TOKEN_TWO_SOURCES="${TMP_DIR}/token_two_sources.yaml"
cat >"${TOKEN_TWO_SOURCES}" <<'EOF'
tokenSecret:
  remoteRef:
    key: example-secret
  awsSecretsManager:
    secretId: example-secret-id
EOF
assert_failure "tokenSecret with both remoteRef and awsSecretsManager" "${TOKEN_TWO_SOURCES}" \
  "one source"

TOKEN_REMOTEREF_SKIP="${TMP_DIR}/token_remoteref_skip.yaml"
cat >"${TOKEN_REMOTEREF_SKIP}" <<'EOF'
skipExternalSecrets: true
tokenSecret:
  remoteRef:
    key: example-secret
EOF
assert_failure "tokenSecret.remoteRef with skipExternalSecrets: true" "${TOKEN_REMOTEREF_SKIP}" \
  "skipExternalSecrets"

ASM_REGION_NO_SECRET_ID="${TMP_DIR}/asm_region_no_secret_id.yaml"
cat >"${ASM_REGION_NO_SECRET_ID}" <<'EOF'
tokenSecret:
  awsSecretsManager:
    region: us-east-1
EOF
assert_failure "tokenSecret.awsSecretsManager with region but no secretId" "${ASM_REGION_NO_SECRET_ID}" \
  "secretId"

# --- environment values files -------------------------------------------------

ENV_VALUES_FILES=(
  "${REPO_ROOT}/environments/examples/aws/values.yaml"
  "${REPO_ROOT}/environments/examples/azure/values.yaml"
  "${REPO_ROOT}/environments/examples/gcp/values.yaml"
  "${REPO_ROOT}/environments/local/values.yaml"
)
for env_file in "${ENV_VALUES_FILES[@]}"; do
  if [[ -f "${env_file}" ]]; then
    lint_and_template "${env_file}"
  fi
done

# --- summary -------------------------------------------------------------------

TOTAL=$((PASS_COUNT + FAIL_COUNT))
echo
echo "==============================================="
echo "render.sh: ${PASS_COUNT}/${TOTAL} passed, ${FAIL_COUNT} failed"
echo "==============================================="

if [[ ${FAIL_COUNT} -ne 0 ]]; then
  exit 1
fi
