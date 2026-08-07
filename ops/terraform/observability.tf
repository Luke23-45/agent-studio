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
EOF
  ]
}
