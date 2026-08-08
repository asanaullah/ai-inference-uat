// Assisted by Claude Opus 4.6
package test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
)

type LifecycleReport struct {
	Successes SuccessMetrics `json:"successes"`
	Failures  FailureMetrics `json:"failures"`
}

type SuccessMetrics struct {
	Count      int               `json:"count"`
	Latency    LatencyMetrics    `json:"latency"`
	Throughput ThroughputMetrics `json:"throughput"`
}

type LatencyMetrics struct {
	RequestLatency   *LatencyStats `json:"request_latency"`
	TimeToFirstToken *LatencyStats `json:"time_to_first_token"`
	TimePerOutputTkn *LatencyStats `json:"time_per_output_token"`
}

type LatencyStats struct {
	Mean   float64 `json:"mean"`
	Median float64 `json:"median"`
	Min    float64 `json:"min"`
	Max    float64 `json:"max"`
}

type ThroughputMetrics struct {
	RequestsPerSec     float64 `json:"requests_per_sec"`
	OutputTokensPerSec float64 `json:"output_tokens_per_sec"`
	TotalTokensPerSec  float64 `json:"total_tokens_per_sec"`
}

type FailureMetrics struct {
	Count int `json:"count"`
}

var (
	prefillURL string
	decodeURL  string
	gatewayURL string
	modelName  string
	prefillTp  string
	decodeTp   string
	serverURL  string
	resultsDir string
)

func TestLlmDLocal(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "llm-d Colocated P/D Suite (NVLink/cuda_ipc)")
}

var _ = BeforeSuite(func() {
	prefillURL = os.Getenv("PREFILL_URL")
	decodeURL = os.Getenv("DECODE_URL")
	gatewayURL = os.Getenv("GATEWAY_URL")
	modelName = os.Getenv("MODEL_NAME")
	prefillTp = os.Getenv("PREFILL_TP")
	decodeTp = os.Getenv("DECODE_TP")
	serverURL = os.Getenv("SERVER_URL")
	resultsDir = os.Getenv("RESULTS_DIR")
	if prefillURL != "" {
		GinkgoWriter.Printf("Prefill:   %s (TP=%s)\n", prefillURL, prefillTp)
	}
	if decodeURL != "" {
		GinkgoWriter.Printf("Decode:    %s (TP=%s)\n", decodeURL, decodeTp)
	}
	if gatewayURL != "" {
		GinkgoWriter.Printf("Gateway:   %s [nginx header-injector]\n", gatewayURL)
	}
	if serverURL != "" {
		GinkgoWriter.Printf("Server:    %s\n", serverURL)
	}
})

func healthCheck(url string) {
	resp, err := http.Get(fmt.Sprintf("%s/health", url))
	Expect(err).NotTo(HaveOccurred())
	defer resp.Body.Close()
	Expect(resp.StatusCode).To(Equal(http.StatusOK))
}

func checkModels(url string) {
	resp, err := http.Get(fmt.Sprintf("%s/v1/models", url))
	Expect(err).NotTo(HaveOccurred())
	defer resp.Body.Close()
	Expect(resp.StatusCode).To(Equal(http.StatusOK))

	var result map[string]interface{}
	Expect(json.NewDecoder(resp.Body).Decode(&result)).To(Succeed())
	data, ok := result["data"].([]interface{})
	Expect(ok).To(BeTrue(), "response missing 'data' field")
	Expect(data).NotTo(BeEmpty(), "no models loaded")
}

func runInference(url, model string) {
	body := map[string]interface{}{
		"model":      model,
		"prompt":     "Hello",
		"max_tokens": 8,
	}
	payload, err := json.Marshal(body)
	Expect(err).NotTo(HaveOccurred())

	resp, err := http.Post(
		fmt.Sprintf("%s/v1/completions", url),
		"application/json",
		bytes.NewReader(payload),
	)
	Expect(err).NotTo(HaveOccurred())
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	Expect(err).NotTo(HaveOccurred())
	Expect(resp.StatusCode).To(Equal(http.StatusOK),
		"inference failed (%d): %s", resp.StatusCode, string(respBody))

	var result map[string]interface{}
	Expect(json.Unmarshal(respBody, &result)).To(Succeed())
	choices, ok := result["choices"].([]interface{})
	Expect(ok).To(BeTrue(), "response missing 'choices'")
	Expect(choices).NotTo(BeEmpty(), "empty choices in response")
}


var _ = Describe("llm-d Colocated P/D", Label("pass-fail"), func() {

	Describe("Prefill Server", func() {
		It("should be healthy", func() {
			Expect(prefillURL).NotTo(BeEmpty(), "PREFILL_URL must be set")
			healthCheck(prefillURL)
		})
		It("should have model loaded", func() {
			checkModels(prefillURL)
		})
		It("should serve inference requests", func() {
			runInference(prefillURL, modelName)
		})
	})

	Describe("Decode Server", func() {
		It("should be healthy", func() {
			Expect(decodeURL).NotTo(BeEmpty(), "DECODE_URL must be set")
			healthCheck(decodeURL)
		})
		It("should have model loaded", func() {
			checkModels(decodeURL)
		})
		It("should serve inference requests", func() {
			runInference(decodeURL, modelName)
		})
	})

	Describe("Gateway (nginx header-injector → sidecar → P/D)", func() {
		It("should route disaggregated inference", func() {
			Expect(gatewayURL).NotTo(BeEmpty(), "GATEWAY_URL must be set")
			runInference(gatewayURL, modelName)
		})
	})

	Describe("GPU Configuration", func() {
		It("should report split", func() {
			Expect(prefillTp).NotTo(BeEmpty(), "PREFILL_TP must be set")
			GinkgoWriter.Printf("=== GPU Split ===\n")
			GinkgoWriter.Printf("Prefill: TP=%s (%s GPUs)\n", prefillTp, prefillTp)
			GinkgoWriter.Printf("Decode:  TP=%s (%s GPUs)\n", decodeTp, decodeTp)
			AddReportEntry("prefill_tp", prefillTp)
			AddReportEntry("decode_tp", decodeTp)
		})
	})

})

var _ = Describe("llm-d NVLink Stress Test", Label("quantitative"), func() {

	Describe("Gateway Health", func() {
		It("should be reachable through nginx gateway", func() {
			Expect(serverURL).NotTo(BeEmpty(), "SERVER_URL must be set")
			healthCheck(serverURL)
		})
	})

	Describe("NVLink Bandwidth Stress", func() {
		It("should execute stress test successfully", func() {
			Expect(resultsDir).NotTo(BeEmpty(), "RESULTS_DIR must be set")
			Expect(os.MkdirAll(resultsDir, 0755)).To(Succeed())

			sweepCmdJSON := os.Getenv("SWEEP_COMMAND")
			Expect(sweepCmdJSON).NotTo(BeEmpty(), "SWEEP_COMMAND must be set")

			var sweepCmd []string
			err := json.Unmarshal([]byte(sweepCmdJSON), &sweepCmd)
			Expect(err).NotTo(HaveOccurred(), "Failed to parse SWEEP_COMMAND")
			Expect(sweepCmd).NotTo(BeEmpty(), "SWEEP_COMMAND is empty")

			cmd := exec.Command(sweepCmd[0], sweepCmd[1:]...)
			cmd.Env = append(os.Environ(), "HOME=/tmp")
			cmd.Stdout = os.Stdout
			cmd.Stderr = os.Stderr

			err = cmd.Run()
			Expect(err).NotTo(HaveOccurred(), "inference-perf stress test failed")
		})

		It("should generate result files", func() {
			files, err := os.ReadDir(resultsDir)
			Expect(err).NotTo(HaveOccurred())
			Expect(files).NotTo(BeEmpty(), "no result files generated")
		})

		It("should report stress test results", func() {
			reportFile := filepath.Join(resultsDir, "summary_lifecycle_metrics.json")
			data, err := os.ReadFile(reportFile)
			Expect(err).NotTo(HaveOccurred(),
				"summary_lifecycle_metrics.json not found in %s", resultsDir)

			var report LifecycleReport
			Expect(json.Unmarshal(data, &report)).To(Succeed())

			totalRequests := report.Successes.Count + report.Failures.Count
			Expect(totalRequests).To(BeNumerically(">", 0), "no requests completed")
			Expect(report.Successes.Count).To(BeNumerically(">", 0), "no successful requests")

			Expect(report.Successes.Latency.RequestLatency).NotTo(BeNil(),
				"missing request_latency")
			Expect(report.Successes.Latency.RequestLatency.Mean).To(
				BeNumerically(">", 0))

			GinkgoWriter.Printf("=== NVLink Stress Test Results ===\n")
			GinkgoWriter.Printf("Requests: %d success, %d failed (%.1f%% failure rate)\n",
				report.Successes.Count, report.Failures.Count,
				float64(report.Failures.Count)/float64(totalRequests)*100)
			GinkgoWriter.Printf("Throughput (tok/s): total=%.2f output=%.2f requests=%.4f\n",
				report.Successes.Throughput.TotalTokensPerSec,
				report.Successes.Throughput.OutputTokensPerSec,
				report.Successes.Throughput.RequestsPerSec)
			GinkgoWriter.Printf("Request Latency (s): mean=%.4f median=%.4f\n",
				report.Successes.Latency.RequestLatency.Mean,
				report.Successes.Latency.RequestLatency.Median)
			if report.Successes.Latency.TimeToFirstToken != nil {
				GinkgoWriter.Printf("TTFT (s): mean=%.4f median=%.4f\n",
					report.Successes.Latency.TimeToFirstToken.Mean,
					report.Successes.Latency.TimeToFirstToken.Median)
				AddReportEntry("ttft_mean_s",
					report.Successes.Latency.TimeToFirstToken.Mean)
			}
			if report.Successes.Latency.TimePerOutputTkn != nil {
				GinkgoWriter.Printf("TPOT (s): mean=%.4f median=%.4f\n",
					report.Successes.Latency.TimePerOutputTkn.Mean,
					report.Successes.Latency.TimePerOutputTkn.Median)
				AddReportEntry("tpot_mean_s",
					report.Successes.Latency.TimePerOutputTkn.Mean)
			}

			AddReportEntry("throughput_total_tokens_per_sec",
				report.Successes.Throughput.TotalTokensPerSec)
			AddReportEntry("throughput_output_tokens_per_sec",
				report.Successes.Throughput.OutputTokensPerSec)
			AddReportEntry("request_latency_mean_s",
				report.Successes.Latency.RequestLatency.Mean)
			AddReportEntry("stress_success_count", report.Successes.Count)
			AddReportEntry("stress_failure_count", report.Failures.Count)
		})
	})

})
