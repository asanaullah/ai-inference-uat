# Test Suite Notes

## Service endpoint propagation race

Ephemeral test pods that depend on a persistent pod's Service can fail if they start before kube-proxy finishes propagating the endpoint. The vLLM readiness probe passing only means the kubelet sees the pod as ready — the Service endpoint still needs to propagate through kube-proxy iptables/IPVS rules. Fast-starting test pods (sub-100ms) can hit this window.

**Observed:** `wrk-4-test-inference-pass-fail` got `connection refused` on first attempt despite vLLM being healthy. Retry succeeded.

**Fix options:**
1. Init container on test pods that polls the service URL before the main container starts. Only inject when the pod references a service. The generator already tracks `services` in the render context.
2. Explicit wait step in the DAG between deploying a persistent service and the first ephemeral test that uses it.
