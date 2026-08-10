package test

import (
	"encoding/json"
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
	serverURL  string
	resultsDir string
)

func TestPeerLoadLow(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "Multi-Tenant Contention Benchmark Suite")
}

var _ = BeforeSuite(func() {
	serverURL = os.Getenv("SERVER_URL")
	Expect(serverURL).NotTo(BeEmpty(), "SERVER_URL must be set")

	resultsDir = os.Getenv("RESULTS_DIR")
	Expect(resultsDir).NotTo(BeEmpty(), "RESULTS_DIR must be set")

	GinkgoWriter.Printf("Server: %s\n", serverURL)
	GinkgoWriter.Printf("Results: %s\n", resultsDir)
})

var _ = Describe("peer-load-low", func() {

	Describe("Benchmark Under Contention", Label("quantitative"), func() {
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
			cmd.Stdout = os.Stdout
			cmd.Stderr = os.Stderr

			err = cmd.Run()
			Expect(err).NotTo(HaveOccurred(), "inference-perf failed")
		})

		It("should generate result files", func() {
			files, err := os.ReadDir(resultsDir)
			Expect(err).NotTo(HaveOccurred())
			Expect(files).NotTo(BeEmpty(), "no result files generated")
		})

		It("should collect benchmark results", func() {
			reportFile := filepath.Join(resultsDir, "summary_lifecycle_metrics.json")
			data, err := os.ReadFile(reportFile)
			Expect(err).NotTo(HaveOccurred(), "summary_lifecycle_metrics.json not found in %s", resultsDir)

			var report LifecycleReport
			Expect(json.Unmarshal(data, &report)).To(Succeed())

			Expect(report.Successes.Count).To(BeNumerically(">", 0), "no successful requests")
			Expect(report.Failures.Count).To(Equal(0), "unexpected request failures")

			Expect(report.Successes.Latency.RequestLatency).NotTo(BeNil(), "missing request_latency")
			Expect(report.Successes.Latency.RequestLatency.Mean).To(BeNumerically(">", 0))
			Expect(report.Successes.Throughput.TotalTokensPerSec).To(BeNumerically(">", 0))

			GinkgoWriter.Printf("=== Contention Benchmark Results ===\n")
			GinkgoWriter.Printf("Successes: %d  Failures: %d\n", report.Successes.Count, report.Failures.Count)
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
				AddReportEntry("ttft_mean_s", report.Successes.Latency.TimeToFirstToken.Mean)
			}
			if report.Successes.Latency.TimePerOutputTkn != nil {
				GinkgoWriter.Printf("TPOT (s): mean=%.4f median=%.4f\n",
					report.Successes.Latency.TimePerOutputTkn.Mean,
					report.Successes.Latency.TimePerOutputTkn.Median)
				AddReportEntry("tpot_mean_s", report.Successes.Latency.TimePerOutputTkn.Mean)
			}

			AddReportEntry("throughput_total_tokens_per_sec", report.Successes.Throughput.TotalTokensPerSec)
			AddReportEntry("throughput_output_tokens_per_sec", report.Successes.Throughput.OutputTokensPerSec)
			AddReportEntry("request_latency_mean_s", report.Successes.Latency.RequestLatency.Mean)
		})
	})

})
