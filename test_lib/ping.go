// Assisted by Claude Opus 4.6
package test

import (
	"net/http"
	"os"
	"testing"
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
)

var (
	ownURL       string
	otherShort   string
	otherFQDN    string
)

func TestPing(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "Ping Cross-Namespace Connectivity Suite")
}

var _ = BeforeSuite(func() {
	ownURL = os.Getenv("OWN_URL")
	otherShort = os.Getenv("OTHER_SHORT_URL")
	otherFQDN = os.Getenv("OTHER_FQDN_URL")
})

func httpReachable(url string, timeout time.Duration) bool {
	client := &http.Client{Timeout: timeout}
	resp, err := client.Get(url)
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

func connectivityChecks() {
	It("should reach own service", func() {
		Expect(ownURL).NotTo(BeEmpty(), "OWN_URL must be set")
		Expect(httpReachable(ownURL, 10*time.Second)).To(BeTrue(),
			"own service at %s is not reachable", ownURL)
	})

	It("should not reach other service by short name", func() {
		Expect(otherShort).NotTo(BeEmpty(), "OTHER_SHORT_URL must be set")
		Expect(httpReachable(otherShort, 5*time.Second)).To(BeFalse(),
			"other service at %s should not be reachable by short name", otherShort)
	})

	It("should not reach other service by FQDN", func() {
		Expect(otherFQDN).NotTo(BeEmpty(), "OTHER_FQDN_URL must be set")
		Expect(httpReachable(otherFQDN, 5*time.Second)).To(BeFalse(),
			"other service at %s should not be reachable by FQDN", otherFQDN)
	})
}

var _ = Describe("Project Namespace Connectivity", Label("project-check"), func() {
	connectivityChecks()
})

var _ = Describe("Peer Namespace Connectivity", Label("peer-check"), func() {
	connectivityChecks()
})
