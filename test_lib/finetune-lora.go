// Assisted by Claude Opus
package test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
)

var (
	serverURL     string
	baseModel     string
	kubeflowModel string
	kuberayModel  string
	prompt        string
)

// chatResponse captures the OpenAI chat-completions fields the quantitative
// checks report on (content + token usage).
type chatResponse struct {
	Choices []struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
	} `json:"choices"`
	Usage struct {
		PromptTokens     int `json:"prompt_tokens"`
		CompletionTokens int `json:"completion_tokens"`
		TotalTokens      int `json:"total_tokens"`
	} `json:"usage"`
}

func TestFinetuneLora(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "Multi-framework LoRA Fine-tuning Suite")
}

var _ = BeforeSuite(func() {
	serverURL = os.Getenv("SERVER_URL")
	baseModel = os.Getenv("BASE_MODEL")
	kubeflowModel = os.Getenv("KUBEFLOW_MODEL")
	kuberayModel = os.Getenv("KUBERAY_MODEL")
	prompt = os.Getenv("PROMPT")
	GinkgoWriter.Printf("Server:   %s\n", serverURL)
	GinkgoWriter.Printf("Models:   %s, %s, %s\n", baseModel, kubeflowModel, kuberayModel)
	GinkgoWriter.Printf("Prompt:   %q\n", prompt)
})

// servedModels returns the model ids registered on /v1/models.
func servedModels(url string) []string {
	resp, err := http.Get(fmt.Sprintf("%s/v1/models", url))
	Expect(err).NotTo(HaveOccurred())
	defer resp.Body.Close()
	Expect(resp.StatusCode).To(Equal(http.StatusOK))

	var result struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	Expect(json.NewDecoder(resp.Body).Decode(&result)).To(Succeed())

	ids := make([]string, 0, len(result.Data))
	for _, m := range result.Data {
		ids = append(ids, m.ID)
	}
	return ids
}

// completion fires a single /v1/completions request against one model id and
// asserts it returns a non-empty choice (used by the pass/fail checks).
func completion(url, model string) {
	body, err := json.Marshal(map[string]interface{}{
		"model":      model,
		"prompt":     "Hello",
		"max_tokens": 8,
	})
	Expect(err).NotTo(HaveOccurred())

	resp, err := http.Post(
		fmt.Sprintf("%s/v1/completions", url),
		"application/json",
		bytes.NewReader(body),
	)
	Expect(err).NotTo(HaveOccurred())
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	Expect(err).NotTo(HaveOccurred())
	Expect(resp.StatusCode).To(Equal(http.StatusOK),
		"inference for %q failed (%d): %s", model, resp.StatusCode, string(respBody))

	var result map[string]interface{}
	Expect(json.Unmarshal(respBody, &result)).To(Succeed())
	choices, ok := result["choices"].([]interface{})
	Expect(ok).To(BeTrue(), "response missing 'choices'")
	Expect(choices).NotTo(BeEmpty(), "empty choices for %q", model)
}

// chat sends the instruction prompt through /v1/chat/completions and returns the
// parsed response plus the wall-clock latency of the request.
func chat(url, model, prompt string) (chatResponse, time.Duration) {
	body, err := json.Marshal(map[string]interface{}{
		"model":       model,
		"messages":    []map[string]string{{"role": "user", "content": prompt}},
		"max_tokens":  256,
		"temperature": 0.2,
	})
	Expect(err).NotTo(HaveOccurred())

	start := time.Now()
	resp, err := http.Post(
		fmt.Sprintf("%s/v1/chat/completions", url),
		"application/json",
		bytes.NewReader(body),
	)
	Expect(err).NotTo(HaveOccurred())
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	Expect(err).NotTo(HaveOccurred())
	elapsed := time.Since(start)
	Expect(resp.StatusCode).To(Equal(http.StatusOK),
		"chat for %q failed (%d): %s", model, resp.StatusCode, string(respBody))

	var parsed chatResponse
	Expect(json.Unmarshal(respBody, &parsed)).To(Succeed())
	return parsed, elapsed
}

var _ = Describe("LoRA fine-tuning: serving", Label("pass-fail"), func() {

	It("should be healthy", func() {
		Expect(serverURL).NotTo(BeEmpty(), "SERVER_URL must be set")
		resp, err := http.Get(fmt.Sprintf("%s/health", serverURL))
		Expect(err).NotTo(HaveOccurred())
		defer resp.Body.Close()
		Expect(resp.StatusCode).To(Equal(http.StatusOK))
	})

	It("should register the base model and both adapters", func() {
		ids := servedModels(serverURL)
		GinkgoWriter.Printf("Registered models: %s\n", strings.Join(ids, ", "))
		Expect(ids).To(ContainElement(baseModel), "base model not registered")
		Expect(ids).To(ContainElement(kubeflowModel), "kubeflow adapter not registered")
		Expect(ids).To(ContainElement(kuberayModel), "kuberay adapter not registered")
	})

	It("should serve inference from the base model", func() {
		completion(serverURL, baseModel)
	})

	It("should serve inference from the kubeflow adapter", func() {
		completion(serverURL, kubeflowModel)
	})

	It("should serve inference from the kuberay adapter", func() {
		completion(serverURL, kuberayModel)
	})
})

var _ = Describe("LoRA fine-tuning: quantitative", Label("quantitative"), func() {

	// One measured chat request per model id, asserting real generation and
	// recording tokens / latency / throughput so the frameworks can be compared.
	for _, tc := range []struct{ label, model string }{
		{"base", baseModel},
		{"kubeflow", kubeflowModel},
		{"kuberay", kuberayModel},
	} {
		tc := tc
		It(fmt.Sprintf("should generate a response from the %s model", tc.label), func() {
			Expect(serverURL).NotTo(BeEmpty(), "SERVER_URL must be set")
			Expect(tc.model).NotTo(BeEmpty(), "model id must be set for %s", tc.label)

			parsed, elapsed := chat(serverURL, tc.model, prompt)

			Expect(parsed.Choices).NotTo(BeEmpty(), "no choices returned for %s", tc.label)
			content := strings.TrimSpace(parsed.Choices[0].Message.Content)
			Expect(content).NotTo(BeEmpty(), "empty generation for %s", tc.label)
			Expect(parsed.Usage.CompletionTokens).To(BeNumerically(">", 0),
				"no tokens generated for %s", tc.label)

			secs := elapsed.Seconds()
			tokPerSec := float64(parsed.Usage.CompletionTokens) / secs

			GinkgoWriter.Printf("\n=== %s (model=%s) ===\n", tc.label, tc.model)
			GinkgoWriter.Printf("prompt:  %s\n", prompt)
			GinkgoWriter.Printf("reply:   %s\n", content)
			GinkgoWriter.Printf("tokens:  prompt=%d completion=%d total=%d\n",
				parsed.Usage.PromptTokens, parsed.Usage.CompletionTokens,
				parsed.Usage.TotalTokens)
			GinkgoWriter.Printf("latency: %.3fs  throughput: %.2f tok/s\n", secs, tokPerSec)

			AddReportEntry(fmt.Sprintf("%s_completion_tokens", tc.label),
				parsed.Usage.CompletionTokens)
			AddReportEntry(fmt.Sprintf("%s_latency_s", tc.label), secs)
			AddReportEntry(fmt.Sprintf("%s_throughput_tok_per_s", tc.label), tokPerSec)
		})
	}
})
