# Phase 6 Observability Stack Configuration
# Provisions the foundational metrics and tracing backends.

resource "helm_release" "prometheus" {
  name       = "prometheus"
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  namespace  = "monitoring"
  create_namespace = true

  set {
    name  = "grafana.enabled"
    value = "true"
  }

  set {
    name  = "prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues"
    value = "false"
  }

  # Wire the Loki datasource into Grafana so one pane covers
  # metrics (Prometheus), logs (Loki), and traces (OTel collector).
  values = [
    <<EOF
grafana:
  additionalDataSources:
    - name: Loki
      type: loki
      url: http://loki:3100
      access: proxy
      isDefault: false
    - name: OTel Traces
      type: jaeger
      url: http://opentelemetry-collector:16686
      access: proxy
      isDefault: false
EOF
  ]
}

resource "helm_release" "loki" {
  name       = "loki"
  repository = "https://grafana.github.io/helm-charts"
  chart      = "loki"
  namespace  = "monitoring"

  set {
    name  = "loki.auth_enabled"
    value = "false"
  }
}

resource "helm_release" "opentelemetry_collector" {
  name       = "opentelemetry-collector"
  repository = "https://open-telemetry.github.io/opentelemetry-helm-charts"
  chart      = "opentelemetry-collector"
  namespace  = "monitoring"

  values = [
    <<EOF
mode: deployment
config:
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: 0.0.0.0:4317
        http:
          endpoint: 0.0.0.0:4318
  exporters:
    prometheus:
      endpoint: "0.0.0.0:8889"
    otlphttp:
      endpoint: http://loki:3100/otlp
    logging:
      verbosity: detailed
  service:
    pipelines:
      traces:
        receivers: [otlp]
        exporters: [logging]
      metrics:
        receivers: [otlp]
        exporters: [prometheus, logging]
      logs:
        receivers: [otlp]
        exporters: [otlphttp, logging]
EOF
  ]
}
