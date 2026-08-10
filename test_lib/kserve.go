// Assisted by Claude Opus 4.6
package test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"testing"
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
)

var (
	serviceURL string
	modelName  string
)

func TestKServe(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "KServe InferenceService Suite")
}

var _ = BeforeSuite(func() {
	serviceURL = os.Getenv("SERVICE_URL")
	modelName = os.Getenv("MODEL_NAME")

	if serviceURL != "" {
		GinkgoWriter.Printf("Service: %s\n", serviceURL)
	}
})

var _ = Describe("KServe InferenceService", Label("pass-fail"), func() {

	It("should become available within 10 minutes", func() {
		Expect(serviceURL).NotTo(BeEmpty(), "SERVICE_URL must be set")

		timeout := 10 * time.Minute
		interval := 15 * time.Second
		deadline := time.Now().Add(timeout)

		for time.Now().Before(deadline) {
			resp, err := http.Get(fmt.Sprintf("%s/health", serviceURL))
			if err == nil {
				resp.Body.Close()
				if resp.StatusCode == http.StatusOK {
					GinkgoWriter.Printf("Endpoint available after %v\n",
						timeout-time.Until(deadline))
					return
				}
			}
			time.Sleep(interval)
		}
		Fail(fmt.Sprintf("endpoint %s/health not available after %v", serviceURL, timeout))
	})

	It("should have model loaded", func() {
		resp, err := http.Get(fmt.Sprintf("%s/v1/models", serviceURL))
		Expect(err).NotTo(HaveOccurred())
		defer resp.Body.Close()
		Expect(resp.StatusCode).To(Equal(http.StatusOK))

		var result map[string]interface{}
		Expect(json.NewDecoder(resp.Body).Decode(&result)).To(Succeed())
		data, ok := result["data"].([]interface{})
		Expect(ok).To(BeTrue(), "response missing 'data' field")
		Expect(data).NotTo(BeEmpty(), "no models loaded")
	})

	It("should serve inference requests", func() {
		Expect(modelName).NotTo(BeEmpty(), "MODEL_NAME must be set")

		body := map[string]interface{}{
			"model":      modelName,
			"prompt":     "Hello",
			"max_tokens": 8,
		}
		payload, err := json.Marshal(body)
		Expect(err).NotTo(HaveOccurred())

		resp, err := http.Post(
			fmt.Sprintf("%s/v1/completions", serviceURL),
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
	})
})
