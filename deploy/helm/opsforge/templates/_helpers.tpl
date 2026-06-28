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

{{/* Superuser DB URL — migrate Job only (alembic DDL + role management). */}}
{{- define "opsforge.databaseUrl" -}}
{{- if .Values.externalDatabaseUrl -}}
{{ .Values.externalDatabaseUrl }}
{{- else -}}
postgresql+psycopg://{{ .Values.postgres.user }}:{{ .Values.postgres.password }}@{{ include "opsforge.fullname" . }}-db:5432/{{ .Values.postgres.database }}
{{- end -}}
{{- end -}}

{{/* Restricted app-role URL — api and worker (NOSUPERUSER NOBYPASSRLS, RLS enforced). */}}
{{- define "opsforge.appDatabaseUrl" -}}
{{- if .Values.externalAppDatabaseUrl -}}
{{ .Values.externalAppDatabaseUrl }}
{{- else if .Values.externalDatabaseUrl -}}
{{ .Values.externalDatabaseUrl }}
{{- else -}}
postgresql+psycopg://{{ .Values.postgres.appUser }}:{{ .Values.postgres.appPassword }}@{{ include "opsforge.fullname" . }}-db:5432/{{ .Values.postgres.database }}
{{- end -}}
{{- end -}}

{{/* Shared OPSFORGE_* env for api + worker + migrate.
     NOTE: api and worker override OPSFORGE_DATABASE_URL with the app-role URL. */}}
{{- define "opsforge.env" -}}
- name: OPSFORGE_ENVIRONMENT
  value: "production"
- name: OPSFORGE_DATABASE_URL
  value: {{ include "opsforge.databaseUrl" . | quote }}
- name: OPSFORGE_MODEL
  value: {{ .Values.model | quote }}
- name: OPSFORGE_FERNET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "opsforge.fullname" . }}-secrets
      key: fernet-key
- name: OPSFORGE_TOKEN_HMAC_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "opsforge.fullname" . }}-secrets
      key: token-hmac-secret
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
