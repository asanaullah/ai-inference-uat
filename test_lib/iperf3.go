// Assisted by Claude Opus 4.6
package test

import (
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/exec"
	"testing"
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
)

type IperfResult struct {
	End IperfEnd `json:"end"`
}

type IperfEnd struct {
	SumReceived IperfSum `json:"sum_received"`
}

type IperfSum struct {
	BitsPerSecond float64 `json:"bits_per_second"`
}

var (
	serverHost string
	resultsDir string
)

func TestIperf3(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "iperf3 TCP Bandwidth Test Suite")
}

var _ = BeforeSuite(func() {
	serverHost = os.Getenv("SERVER_HOST")
	Expect(serverHost).NotTo(BeEmpty(), "SERVER_HOST must be set")

	resultsDir = os.Getenv("RESULTS_DIR")
	Expect(resultsDir).NotTo(BeEmpty(), "RESULTS_DIR must be set")

	GinkgoWriter.Printf("Server: %s\n", serverHost)
	GinkgoWriter.Printf("Results: %s\n", resultsDir)
})

var _ = Describe("iperf3 TCP Bandwidth", func() {

	Describe("Server Connectivity", Label("pass-fail"), func() {
		It("should be reachable on port 5201", func() {
			addr := fmt.Sprintf("%s:5201", serverHost)
			conn, err := net.DialTimeout("tcp", addr, 10*time.Second)
			Expect(err).NotTo(HaveOccurred(), "cannot reach iperf3 server at %s", addr)
			conn.Close()
		})
	})

	Describe("Bandwidth Measurement", Label("pass-fail"), func() {
		It("should measure non-zero TCP bandwidth", func() {
			Expect(os.MkdirAll(resultsDir, 0755)).To(Succeed())

			cmd := exec.Command("iperf3", "-c", serverHost, "-t", "10", "-J")
			output, err := cmd.CombinedOutput()
			GinkgoWriter.Printf("iperf3 output:\n%s\n", string(output))
			Expect(err).NotTo(HaveOccurred(), "iperf3 failed: %s", string(output))

			var result IperfResult
			Expect(json.Unmarshal(output, &result)).To(Succeed())

			bps := result.End.SumReceived.BitsPerSecond
			gbps := bps / 1e9
			GinkgoWriter.Printf("TCP bandwidth: %.2f Gbps\n", gbps)
			Expect(bps).To(BeNumerically(">", 0), "measured bandwidth is zero")

			AddReportEntry("bandwidth_gbps", gbps)
		})
	})

})
