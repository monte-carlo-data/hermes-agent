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
{{- if or .enabled .remoteRef .awsSecretsManager -}}
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
The AWS region for the selected method's Secrets Manager source, or empty.
Leaving it unset lets boto resolve the region the usual way, which is what a
same-region deployment wants.
*/}}
{{- define "hermes.auth.awsRegion" -}}
{{- $block := ternary (.Values.oauthSecret | default dict) (.Values.tokenSecret | default dict) (eq (include "hermes.auth.method" .) "oauth") -}}
{{- ($block.awsSecretsManager).region | default "" -}}
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
{{- if and $oauth (or ((.Values.tokenSecret).remoteRef) (((.Values.tokenSecret).awsSecretsManager))) -}}
{{- fail "oauthSecret and tokenSecret are both configured with a credential source — the agent uses one authentication method at a time. Remove the oauthSecret block to use key/token authentication, or remove tokenSecret to use OAuth." -}}
{{- end -}}
{{- range $method, $block := dict "oauthSecret" .Values.oauthSecret "tokenSecret" .Values.tokenSecret -}}
{{- if and (($block).remoteRef) ((($block).awsSecretsManager)) -}}
{{- fail (printf "%s sets both remoteRef and awsSecretsManager — a credential comes from one source. Keep remoteRef to sync it with the External Secrets Operator, or awsSecretsManager to have the agent read it directly." $method) -}}
{{- end -}}
{{- end -}}
{{- if and .Values.oauthSecret .Values.oauthSecret.tokenEndpoint (not (hasPrefix "https://" .Values.oauthSecret.tokenEndpoint)) -}}
{{- fail "oauthSecret.tokenEndpoint must use HTTPS" -}}
{{- end -}}
{{- if and (eq $source "externalSecret") .Values.skipExternalSecrets -}}
{{- fail "A remoteRef is configured but skipExternalSecrets is true — nothing would sync the credential. Remove skipExternalSecrets to use the External Secrets Operator, or replace remoteRef with awsSecretsManager to have the agent read the credential itself." -}}
{{- end -}}
{{/* A k8sSecret source means no credential source was configured at all. That
     is only valid when the operator creates the Secret by hand, which is what
     skipExternalSecrets declares. awsSecretsManager needs neither. */}}
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
