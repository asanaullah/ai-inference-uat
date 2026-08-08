// Assisted by Claude Opus 4.6
package test

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"testing"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	authorizationv1 "k8s.io/api/authorization/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

type Check struct {
	Type     string `json:"type"`
	Verb     string `json:"verb,omitempty"`
	Resource string `json:"resource,omitempty"`
	Group    string `json:"group,omitempty"`
	Expected string `json:"expected"`
}

var (
	checks    []Check
	clientset *kubernetes.Clientset
)

func TestPlatformCheck(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "Platform Check Test Suite")
}

var _ = BeforeSuite(func() {
	raw := os.Getenv("PERMISSION_CHECKS")
	Expect(raw).NotTo(BeEmpty(), "PERMISSION_CHECKS must be set")
	Expect(json.Unmarshal([]byte(raw), &checks)).To(Succeed())
	Expect(checks).NotTo(BeEmpty())

	config, err := rest.InClusterConfig()
	Expect(err).NotTo(HaveOccurred())
	clientset, err = kubernetes.NewForConfig(config)
	Expect(err).NotTo(HaveOccurred())
})

func checkPermission(verb, resource string) string {
	review, err := clientset.AuthorizationV1().SelfSubjectAccessReviews().Create(
		context.TODO(),
		&authorizationv1.SelfSubjectAccessReview{
			Spec: authorizationv1.SelfSubjectAccessReviewSpec{
				ResourceAttributes: &authorizationv1.ResourceAttributes{
					Verb: verb, Resource: resource,
				},
			},
		},
		metav1.CreateOptions{},
	)
	Expect(err).NotTo(HaveOccurred())
	if review.Status.Allowed {
		return "yes"
	}
	return "no"
}

func checkAPIGroup(group string) string {
	groups, err := clientset.Discovery().ServerGroups()
	Expect(err).NotTo(HaveOccurred())
	for _, g := range groups.Groups {
		if g.Name == group {
			return "yes"
		}
	}
	return "no"
}

var _ = Describe("Platform Check", Label("pass-fail"), func() {
	It("validates all checks", func() {
		var failures []string
		for _, c := range checks {
			var label, actual string
			if c.Type == "permission" {
				label = fmt.Sprintf("permission %-7s %s", c.Verb, c.Resource)
				actual = checkPermission(c.Verb, c.Resource)
			} else {
				label = fmt.Sprintf("apiGroup   %s", c.Group)
				actual = checkAPIGroup(c.Group)
			}
			status := "PASS"
			if actual != c.Expected {
				status = "FAIL"
				failures = append(failures, label)
			}
			fmt.Fprintf(os.Stdout, "  %s  %-45s got=%-3s expected=%s\n", status, label, actual, c.Expected)
		}
		Expect(failures).To(BeEmpty(), "failed checks: %v", failures)
	})
})
