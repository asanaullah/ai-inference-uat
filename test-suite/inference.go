// Assisted by Claude Opus 4.6
package test

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
)

type BenchmarksFile struct {
	Benchmarks []Benchmark `json:"benchmarks"`
}

type Benchmark struct {
	Metrics BenchmarkMetrics `json:"metrics"`
}

type BenchmarkMetrics struct {
	OutputTokensPerSecond MetricGroup `json:"output_tokens_per_second"`
	RequestLatency        MetricGroup `json:"request_latency"`
}

type MetricGroup struct {
	Successful MetricStats `json:"successful"`
}

type MetricStats struct {
	Mean   float64 `json:"mean"`
	Median float64 `json:"median"`
	Min    float64 `json:"min"`
	Max    float64 `json:"max"`
}

var (
	serverURL  string
	resultsDir string
)

func TestInference(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "Inference Server Test Suite")
}

var _ = BeforeSuite(func() {
	serverURL = os.Getenv("SERVER_URL")
	Expect(serverURL).NotTo(BeEmpty(), "SERVER_URL must be set")

	resultsDir = os.Getenv("RESULTS_DIR")
	Expect(resultsDir).NotTo(BeEmpty(), "RESULTS_DIR must be set")

	GinkgoWriter.Printf("Server: %s\n", serverURL)
	GinkgoWriter.Printf("Results: %s\n", resultsDir)
})

var _ = Describe("vLLM Inference Server", func() {

	Describe("Health Check", Label("pass-fail"), func() {
		It("should return healthy status", func() {
			resp, err := http.Get(fmt.Sprintf("%s/health", serverURL))
			Expect(err).NotTo(HaveOccurred())
			defer resp.Body.Close()
			Expect(resp.StatusCode).To(Equal(http.StatusOK))
		})
	})

	Describe("Model Endpoint", Label("pass-fail"), func() {
		It("should list available models", func() {
			resp, err := http.Get(fmt.Sprintf("%s/v1/models", serverURL))
			Expect(err).NotTo(HaveOccurred())
			defer resp.Body.Close()
			Expect(resp.StatusCode).To(Equal(http.StatusOK))

			var result map[string]interface{}
			err = json.NewDecoder(resp.Body).Decode(&result)
			Expect(err).NotTo(HaveOccurred())

			data, ok := result["data"].([]interface{})
			Expect(ok).To(BeTrue(), "response missing 'data' field")
			Expect(data).NotTo(BeEmpty(), "no models available")
		})
	})

	Describe("Benchmark Execution", Label("quantitative"), func() {
		It("should execute benchmark successfully", func() {
			Expect(os.MkdirAll(resultsDir, 0755)).To(Succeed())

			sweepCmdJSON := os.Getenv("SWEEP_COMMAND")
			Expect(sweepCmdJSON).NotTo(BeEmpty(), "SWEEP_COMMAND must be set")

			var sweepCmd []string
			err := json.Unmarshal([]byte(sweepCmdJSON), &sweepCmd)
			Expect(err).NotTo(HaveOccurred(), "Failed to parse SWEEP_COMMAND")
			Expect(sweepCmd).NotTo(BeEmpty(), "SWEEP_COMMAND is empty")

			cmd := exec.Command(sweepCmd[0], sweepCmd[1:]...)
			cmd.Env = append(os.Environ(), "HOME=/tmp")

			output, err := cmd.CombinedOutput()
			GinkgoWriter.Printf("benchmark output:\n%s\n", string(output))
			Expect(err).NotTo(HaveOccurred(), "benchmark failed: %s", string(output))
		})

		It("should generate result files", func() {
			files, err := os.ReadDir(resultsDir)
			Expect(err).NotTo(HaveOccurred())
			Expect(files).NotTo(BeEmpty(), "no result files generated")
		})

		It("should collect benchmark results", func() {
			benchmarkFile := filepath.Join(resultsDir, "benchmarks.json")
			data, err := os.ReadFile(benchmarkFile)
			Expect(err).NotTo(HaveOccurred(), "benchmarks.json not found in %s", resultsDir)

			var benchmarks BenchmarksFile
			Expect(json.Unmarshal(data, &benchmarks)).To(Succeed())
			Expect(benchmarks.Benchmarks).NotTo(BeEmpty(), "no benchmarks in results")

			b := benchmarks.Benchmarks[0]
			throughput := b.Metrics.OutputTokensPerSecond.Successful
			latency := b.Metrics.RequestLatency.Successful

			GinkgoWriter.Printf("=== Benchmark Results ===\n")
			GinkgoWriter.Printf("Throughput (tok/s): mean=%.2f median=%.2f min=%.2f max=%.2f\n",
				throughput.Mean, throughput.Median, throughput.Min, throughput.Max)
			GinkgoWriter.Printf("Latency (s):        mean=%.4f median=%.4f min=%.4f max=%.4f\n",
				latency.Mean, latency.Median, latency.Min, latency.Max)

			AddReportEntry("throughput_mean_tok_s", throughput.Mean)
			AddReportEntry("throughput_median_tok_s", throughput.Median)
			AddReportEntry("latency_mean_s", latency.Mean)
			AddReportEntry("latency_median_s", latency.Median)
		})
	})

})
