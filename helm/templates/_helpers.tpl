{{/*
Whether OAuth authentication is enabled. Any signal inside `oauthSecret`
selects it: `enabled: true` (the documented way), or a credential source —
`remoteRef` for the ESO path, `awsSecretsManager` for a credential the agent
reads itself. A source implies the method so that values files predating
`enabled` keep working.

`enabled: false` alongside a source is contradictory rather than an override,
and resolving it by precedence is how a values file silently gets an
authentication method its author did not choose. hermes.auth.validate rejects
that combination instead.
*/}}
{{- define "hermes.oauth.enabled" -}}
{{- with .Values.oauthSecret -}}
{{- if or .enabled .remoteRef (.awsSecretsManager).secretId -}}
true
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
The selected authentication method: `oauth` or `token`.
*/}}
{{- define "hermes.auth.method" -}}
{{- if include "hermes.oauth.enabled" . -}}oauth{{- else -}}token{{- end -}}
{{- end -}}

{{/*
Where the selected method's credential comes from, as one of:

  awsSecretsManager — the agent reads it directly; no Kubernetes Secret exists
  externalSecret    — ESO syncs it into a Kubernetes Secret from a remoteRef
  k8sSecret         — a Kubernetes Secret the operator created themselves

Every consumer branches on this rather than re-deriving from `remoteRef`, so
adding a source means adding a case here instead of another term to each
condition. The three are mutually exclusive by construction: a block with both
`awsSecretsManager` and `remoteRef` is rejected in hermes.auth.validate.
*/}}
{{- define "hermes.auth.source" -}}
{{- $block := ternary (.Values.oauthSecret | default dict) (.Values.tokenSecret | default dict) (eq (include "hermes.auth.method" .) "oauth") -}}
{{- if ($block.awsSecretsManager).secretId -}}awsSecretsManager
{{- else if $block.remoteRef -}}externalSecret
{{- else -}}k8sSecret
{{- end -}}
{{- end -}}

{{/*
The secret id (name or ARN) for the selected method's Secrets Manager source.
*/}}
{{- define "hermes.auth.awsSecretId" -}}
{{- $block := ternary (.Values.oauthSecret | default dict) (.Values.tokenSecret | default dict) (eq (include "hermes.auth.method" .) "oauth") -}}
{{- ($block.awsSecretsManager).secretId -}}
{{- end -}}

{{/*
The AWS region for the selected method's Secrets Manager source, or empty —
empty lets boto resolve it the usual way.
*/}}
{{- define "hermes.auth.awsRegion" -}}
{{- $block := ternary (.Values.oauthSecret | default dict) (.Values.tokenSecret | default dict) (eq (include "hermes.auth.method" .) "oauth") -}}
{{- ($block.awsSecretsManager).region | default "" -}}
{{- end -}}

{{/*
Whether the selected method's Secrets Manager values are base64-encoded.
Applies to every secret in the block. Opt-in because base64 text is itself a
valid secret value; binary secrets need no flag.

Checked with `kindIs "bool"` rather than plain truthiness: `text/template`
treats any non-empty string as true, so a quoted `base64Encoded: "false"`
(e.g. from `--set-string`, a values file round-tripped through `yq`, or a
Terraform `set { type = "string" }` block) would otherwise turn decoding on.
hermes.auth.validate rejects a non-bool value outright rather than silently
ignoring it.
*/}}
{{- define "hermes.auth.awsBase64Encoded" -}}
{{- $block := ternary (.Values.oauthSecret | default dict) (.Values.tokenSecret | default dict) (eq (include "hermes.auth.method" .) "oauth") -}}
{{- $flag := ($block.awsSecretsManager).base64Encoded -}}
{{- if and (kindIs "bool" $flag) $flag -}}true{{- end -}}
{{- end -}}

{{/*
The Kubernetes Secret the selected method's credential is read from, its key,
and its mount path. Defined once because the same three strings otherwise
appear across the deployment, both ExternalSecret templates, the collectors,
and NOTES.txt — and a consumer that keeps its own copy is a consumer the
source model cannot see, which is how the collectors came to mount a Secret
that no longer always exists.
*/}}
{{- define "hermes.auth.secretName" -}}
{{- if eq (include "hermes.auth.method" .) "oauth" -}}mcd-oauth-secret{{- else -}}mcd-agent-token-secret{{- end -}}
{{- end -}}

{{- define "hermes.auth.secretKey" -}}
{{- if eq (include "hermes.auth.method" .) "oauth" -}}credentials.json{{- else -}}contents.json{{- end -}}
{{- end -}}

{{- define "hermes.auth.mountPath" -}}
{{- if eq (include "hermes.auth.method" .) "oauth" -}}/etc/secrets/mcd-oauth{{- else -}}/etc/secrets/mcd-agent-token{{- end -}}
{{- end -}}

{{/*
Included from the deployment so a misconfigured release fails at template time
instead of as a backend authentication failure at runtime.

Checks are keyed on the *selected* method and source rather than on whichever
value happens to be present. The deployment mounts the secret for the selected
method non-optionally, so validating anything else lets a release render with a
mount that no template creates — the pod then waits on it forever while the
install reports success.
*/}}
{{- define "hermes.auth.validate" -}}
{{- $oauth := include "hermes.oauth.enabled" . -}}
{{- $source := include "hermes.auth.source" . -}}
{{- with .Values.oauthSecret -}}
{{- if and (hasKey . "enabled") (not .enabled) (or .remoteRef .awsSecretsManager) -}}
{{- fail "oauthSecret.enabled is false but a credential source is configured under oauthSecret. Remove the oauthSecret block to use key/token authentication, or drop oauthSecret.enabled to use OAuth." -}}
{{- end -}}
{{- end -}}
{{- if and $oauth (or ((.Values.tokenSecret).remoteRef) (((.Values.tokenSecret).awsSecretsManager).secretId)) -}}
{{- fail "oauthSecret and tokenSecret are both configured with a credential source — the agent uses one authentication method at a time. Remove the oauthSecret block to use key/token authentication, or remove tokenSecret to use OAuth." -}}
{{- end -}}
{{/* The `if $block` is load-bearing, not defensive: an unset block arrives from
     the dict as nil, and `hasKey` rejects a non-map outright rather than
     returning false. */}}
{{- range $method, $block := dict "oauthSecret" .Values.oauthSecret "tokenSecret" .Values.tokenSecret -}}
{{- if $block -}}
{{- if and ($block.remoteRef) (($block.awsSecretsManager).secretId) -}}
{{- fail (printf "%s sets both remoteRef and awsSecretsManager — a credential comes from one source. Keep remoteRef to sync it with the External Secrets Operator, or awsSecretsManager to have the agent read it directly." $method) -}}
{{- end -}}
{{- if and (hasKey $block "awsSecretsManager") (not (($block.awsSecretsManager).secretId)) -}}
{{- fail (printf "%s.awsSecretsManager is set but %s.awsSecretsManager.secretId is empty. Set it to the name or ARN of the secret, or remove the awsSecretsManager block." $method $method) -}}
{{- end -}}
{{- if and (hasKey ($block.awsSecretsManager | default dict) "base64Encoded") (not (kindIs "bool" ($block.awsSecretsManager).base64Encoded)) -}}
{{- fail (printf "%s.awsSecretsManager.base64Encoded must be a boolean, got %q. A quoted value (e.g. \"false\") is a non-empty string, which is always true — set it unquoted." $method (toString ($block.awsSecretsManager).base64Encoded)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and .Values.oauthSecret .Values.oauthSecret.tokenEndpoint (not (hasPrefix "https://" .Values.oauthSecret.tokenEndpoint)) -}}
{{- fail "oauthSecret.tokenEndpoint must use HTTPS" -}}
{{- end -}}
{{- if and (eq $source "externalSecret") .Values.skipExternalSecrets -}}
{{- fail "A remoteRef is configured but skipExternalSecrets is true — nothing would sync the credential. Remove skipExternalSecrets to use the External Secrets Operator, or replace remoteRef with awsSecretsManager to have the agent read the credential itself." -}}
{{- end -}}
{{/* The metrics and logs collectors read `mcd_id`/`mcd_token` out of
     `mcd-agent-token-secret` with an init container, so they cannot work when
     the agent reads its own credential from a secret manager — no such Secret
     exists. Rendering them anyway parks their pods in ContainerCreating on
     every node while the install reports success, so this fails instead and
     names the two ways out.

     Scoped to awsSecretsManager deliberately. The collectors have the same
     incompatibility with OAuth, which predates this source and would break
     existing releases on upgrade if failed here — tracked separately. */}}
{{- if eq $source "awsSecretsManager" -}}
{{- if .Values.metricsCollector.enabled -}}
{{- fail "metricsCollector.enabled is true but the agent credential comes from AWS Secrets Manager. The metrics collector reads mcd_id/mcd_token from the mcd-agent-token-secret Secret, which is not created for this source, so its pods would never start. Set metricsCollector.enabled: false, or use a tokenSecret.remoteRef credential source." -}}
{{- end -}}
{{- if eq .Values.logShipping "fluentd" -}}
{{- fail "logShipping is fluentd but the agent credential comes from AWS Secrets Manager. The logs collector reads mcd_id/mcd_token from the mcd-agent-token-secret Secret, which is not created for this source, so its pods would never start. Use logShipping: in-process (the default), or a tokenSecret.remoteRef credential source." -}}
{{- end -}}
{{- end -}}
{{/* A k8sSecret source means no credential source was configured at all. That
     is only valid when the operator creates the Secret by hand, which is what
     skipExternalSecrets declares. awsSecretsManager needs neither ESO nor a
     hand-made Secret. */}}
{{- if and (eq $source "k8sSecret") (not .Values.skipExternalSecrets) -}}
{{- if $oauth -}}
{{- fail "OAuth is selected but no credential source is configured. Set oauthSecret.remoteRef to sync it with the External Secrets Operator, or oauthSecret.awsSecretsManager.secretId to have the agent read it directly, or skipExternalSecrets: true when mcd-oauth-secret is created manually." -}}
{{- else -}}
{{- fail "Key/token authentication is selected but no credential source is configured. Set tokenSecret.remoteRef to sync it with the External Secrets Operator, or tokenSecret.awsSecretsManager.secretId to have the agent read it directly, or skipExternalSecrets: true when mcd-agent-token-secret is created manually." -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Log shipping mode validation. Fails on the legacy two-flag keys (renamed to
the `logShipping` enum) and rejects unknown enum values. Also rejects
in-process log levels outside a curated allowlist — DEBUG would surface
third-party-library content (request bodies, tokens) into shipped logs.
*/}}
{{- define "hermes.logShipping.validate" -}}
{{- if hasKey .Values.logsCollector "enabled" -}}
{{- fail "logsCollector.enabled has been replaced by the top-level `logShipping` setting. Use `logShipping: fluentd` (was `logsCollector.enabled: true`) or `logShipping: none` (was `logsCollector.enabled: false`, which left the agent with no log shipping). To opt into the new in-process shipper instead, set `logShipping: in-process`. See helm/README.md." -}}
{{- end -}}
{{- if not (has .Values.logShipping (list "in-process" "fluentd" "none")) -}}
{{- fail (printf "logShipping must be one of: in-process, fluentd, none (got: %q)" (toString .Values.logShipping)) -}}
{{- end -}}
{{- if .Values.inProcessLogs }}
{{- $level := .Values.inProcessLogs.logLevel | default "INFO" -}}
{{- if not (has $level (list "INFO" "WARNING" "WARN" "ERROR" "CRITICAL")) -}}
{{- fail (printf "inProcessLogs.logLevel must be one of: INFO, WARNING, WARN, ERROR, CRITICAL (got: %q). DEBUG is intentionally excluded — it surfaces third-party-library content into shipped logs." $level) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Firewall CA — shared helpers for TLS inspection support.
Used by the agent deployment and both collector daemonsets.
*/}}

{{/* Whether firewall CA is configured (inline cert or external secret reference) */}}
{{- define "hermes.firewallCa.enabled" -}}
{{- if and .Values.firewallCa.cert .Values.firewallCa.externalSecretRef -}}
{{- fail "firewallCa.cert and firewallCa.externalSecretRef are mutually exclusive — set one or the other" -}}
{{- end -}}
{{- if or .Values.firewallCa.cert .Values.firewallCa.externalSecretRef -}}true{{- end -}}
{{- end -}}

{{/* Init container that merges system CAs + firewall CA into a combined bundle.
     Alpine is pinned to a specific minor for reproducibility — bump periodically.
     The container-level securityContext is required (not inherited from the pod)
     by clusters that enforce the K8s `restricted` Pod Security Standard.
     Sourced from .Values.firewallCa.securityContext so operators can override
     (e.g. drop seccompProfile on managed K8s tiers that reject RuntimeDefault). */}}
{{- define "hermes.firewallCa.initContainer" -}}
- name: build-ca-bundle
  image: alpine:3.21
  {{- with .Values.firewallCa.securityContext }}
  securityContext:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  command:
    - sh
    - -c
    - cat /etc/ssl/certs/ca-certificates.crt /etc/ssl/firewall-ca/ca.crt > /etc/ssl/combined-ca/ca-bundle.crt
  volumeMounts:
    - name: firewall-ca
      mountPath: /etc/ssl/firewall-ca
      readOnly: true
    - name: combined-ca
      mountPath: /etc/ssl/combined-ca
{{- end -}}

{{/* Volume mounts for firewall CA (add to main container) */}}
{{- define "hermes.firewallCa.volumeMounts" -}}
- name: firewall-ca
  mountPath: /etc/ssl/firewall-ca
  readOnly: true
- name: combined-ca
  mountPath: /etc/ssl/combined-ca
  readOnly: true
{{- end -}}

{{/* Volume definitions for firewall CA */}}
{{- define "hermes.firewallCa.volumes" -}}
- name: firewall-ca
  {{- if .Values.firewallCa.cert }}
  configMap:
    name: firewall-ca-cert
  {{- else }}
  secret:
    secretName: firewall-ca-cert
  {{- end }}
- name: combined-ca
  emptyDir: {}
{{- end -}}
