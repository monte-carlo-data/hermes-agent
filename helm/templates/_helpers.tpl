{{/*
Log shipping mode validation. Fails on the legacy two-flag keys (renamed to
the `logShipping` enum) and rejects unknown enum values.
*/}}
{{- define "hermes.logShipping.validate" -}}
{{- if hasKey .Values.logsCollector "enabled" -}}
{{- fail "logsCollector.enabled has been replaced by the top-level `logShipping` setting. Use `logShipping: daemonset` (was `logsCollector.enabled: true`) or `logShipping: in-process` (was `logsCollector.enabled: false`). See helm/README.md." -}}
{{- end -}}
{{- if not (has .Values.logShipping (list "in-process" "daemonset" "none")) -}}
{{- fail (printf "logShipping must be one of: in-process, daemonset, none (got: %q)" (toString .Values.logShipping)) -}}
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
