{{- define "opsforge.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "opsforge.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "opsforge.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "opsforge.labels" -}}
app.kubernetes.io/name: {{ include "opsforge.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "opsforge.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}

{{/* Database URL: external if provided, else the in-cluster Postgres service. */}}
{{- define "opsforge.databaseUrl" -}}
{{- if .Values.externalDatabaseUrl -}}
{{ .Values.externalDatabaseUrl }}
{{- else -}}
postgresql+psycopg://{{ .Values.postgres.user }}:{{ .Values.postgres.password }}@{{ include "opsforge.fullname" . }}-db:5432/{{ .Values.postgres.database }}
{{- end -}}
{{- end -}}

{{/* Shared OPSFORGE_* env for api + worker + migrate. */}}
{{- define "opsforge.env" -}}
- name: OPSFORGE_DATABASE_URL
  value: {{ include "opsforge.databaseUrl" . | quote }}
- name: OPSFORGE_MODEL
  value: {{ .Values.model | quote }}
- name: OPSFORGE_FERNET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "opsforge.fullname" . }}-secrets
      key: fernet-key
- name: OPSFORGE_WEBHOOK_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "opsforge.fullname" . }}-secrets
      key: webhook-secret
- name: OPSFORGE_SLACK_BOT_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "opsforge.fullname" . }}-secrets
      key: slack-bot-token
- name: OPSFORGE_SLACK_SIGNING_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "opsforge.fullname" . }}-secrets
      key: slack-signing-secret
{{- end -}}
