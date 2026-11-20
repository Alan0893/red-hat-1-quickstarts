{{- /*
The personal-assistant demo: "provision" (sources seeded and wired to the
workspace), "enabled" (shared sources reachable, nothing seeded) or "".
Container tenants with a model (saw.inferenceSeed) and a lab password
(saw.account.password) get the full demo unless saw.demo says otherwise; VM
tenants keep it opt-in (they also need the account Job and credential proxy).
*/ -}}
{{- define "tenant.sawDemo" -}}
{{- $saw := .Values.saw | default dict -}}
{{- $demo := $saw.demo | default dict -}}
{{- $container := eq (lower (toString ($saw.deploymentMode | default "vm"))) "container" -}}
{{- $seeded := eq (lower (toString (($saw.inferenceSeed | default dict).enabled))) "true" -}}
{{- $default := and $container $seeded (not (empty (($saw.account | default dict).password))) -}}
{{- $enabled := ternary $default (eq (lower (toString $demo.enabled)) "true") (not (hasKey $demo "enabled")) -}}
{{- $provision := ternary $default (eq (lower (toString $demo.provisioningEnabled)) "true") (not (hasKey $demo "provisioningEnabled")) -}}
{{- if and $enabled $provision -}}provision{{- else if $enabled -}}enabled{{- end -}}
{{- end -}}
