package test

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

type Check struct {
	Type          string `json:"type"`
	Name          string `json:"name,omitempty"`
	Namespace     string `json:"namespace,omitempty"`
	Group         string `json:"group,omitempty"`
	Version       string `json:"version,omitempty"`
	Resource      string `json:"resource,omitempty"`
	Field         string `json:"field,omitempty"`
	Value         string `json:"value,omitempty"`
	ConditionType string `json:"conditionType,omitempty"`
	LabelSelector string `json:"labelSelector,omitempty"`
	Label         string `json:"label,omitempty"`
	Expected      string `json:"expected"`
}

var (
	checks    []Check
	clientset *kubernetes.Clientset
	dynClient dynamic.Interface
)

func TestPlatformCheckAdmin(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "Platform Check Admin")
}

var _ = BeforeSuite(func() {
	raw := os.Getenv("ADMIN_CHECKS")
	Expect(raw).NotTo(BeEmpty(), "ADMIN_CHECKS must be set")
	Expect(json.Unmarshal([]byte(raw), &checks)).To(Succeed())
	Expect(checks).NotTo(BeEmpty())

	config, err := rest.InClusterConfig()
	Expect(err).NotTo(HaveOccurred())
	clientset, err = kubernetes.NewForConfig(config)
	Expect(err).NotTo(HaveOccurred())
	dynClient, err = dynamic.NewForConfig(config)
	Expect(err).NotTo(HaveOccurred())
})

func checkDeployment(name, namespace string) string {
	dep, err := clientset.AppsV1().Deployments(namespace).Get(context.TODO(), name, metav1.GetOptions{})
	if err != nil {
		return "no"
	}
	for _, c := range dep.Status.Conditions {
		if c.Type == "Available" && string(c.Status) == "True" {
			return "yes"
		}
	}
	return "no"
}

func checkCRField(group, version, resource, field, value string) string {
	gvr := schema.GroupVersionResource{Group: group, Version: version, Resource: resource}
	list, err := dynClient.Resource(gvr).List(context.TODO(), metav1.ListOptions{})
	if err != nil || len(list.Items) == 0 {
		return "no"
	}
	status, ok := list.Items[0].Object["status"].(map[string]interface{})
	if !ok {
		return "no"
	}
	if fmt.Sprintf("%v", status[field]) == value {
		return "yes"
	}
	return "no"
}

func checkCRCondition(group, version, resource, conditionType string) string {
	gvr := schema.GroupVersionResource{Group: group, Version: version, Resource: resource}
	list, err := dynClient.Resource(gvr).List(context.TODO(), metav1.ListOptions{})
	if err != nil || len(list.Items) == 0 {
		return "no"
	}
	status, ok := list.Items[0].Object["status"].(map[string]interface{})
	if !ok {
		return "no"
	}
	conditions, ok := status["conditions"].([]interface{})
	if !ok {
		return "no"
	}
	for _, c := range conditions {
		cond, ok := c.(map[string]interface{})
		if !ok {
			continue
		}
		if fmt.Sprintf("%v", cond["type"]) == conditionType && fmt.Sprintf("%v", cond["status"]) == "True" {
			return "yes"
		}
	}
	return "no"
}

func checkNodeLabel(labelSelector, label string) string {
	nodes, err := clientset.CoreV1().Nodes().List(context.TODO(), metav1.ListOptions{LabelSelector: labelSelector})
	if err != nil || len(nodes.Items) == 0 {
		return "no"
	}
	alternatives := strings.Split(label, "|")
	for _, node := range nodes.Items {
		found := false
		for _, l := range alternatives {
			if _, ok := node.Labels[strings.TrimSpace(l)]; ok {
				found = true
				break
			}
		}
		if !found {
			return "no"
		}
	}
	return "yes"
}

func checkPodsRunning(namespace, labelSelector string) string {
	pods, err := clientset.CoreV1().Pods(namespace).List(context.TODO(), metav1.ListOptions{LabelSelector: labelSelector})
	if err != nil {
		return "no"
	}
	for _, pod := range pods.Items {
		if pod.Status.Phase == "Running" {
			return "yes"
		}
	}
	return "no"
}

var _ = Describe("Platform Check Admin", Label("pass-fail"), func() {
	It("validates all admin checks", func() {
		var failures []string
		for _, c := range checks {
			var label, actual string
			switch c.Type {
			case "deployment":
				label = fmt.Sprintf("deployment  %s/%s", c.Namespace, c.Name)
				actual = checkDeployment(c.Name, c.Namespace)
			case "crField":
				label = fmt.Sprintf("crField     %s.%s=%s", c.Resource, c.Field, c.Value)
				actual = checkCRField(c.Group, c.Version, c.Resource, c.Field, c.Value)
			case "crCondition":
				label = fmt.Sprintf("crCondition %s/%s", c.Resource, c.ConditionType)
				actual = checkCRCondition(c.Group, c.Version, c.Resource, c.ConditionType)
			case "nodeLabel":
				label = fmt.Sprintf("nodeLabel   %s", c.Label)
				actual = checkNodeLabel(c.LabelSelector, c.Label)
			case "podsRunning":
				label = fmt.Sprintf("podsRunning %s/%s", c.Namespace, c.LabelSelector)
				actual = checkPodsRunning(c.Namespace, c.LabelSelector)
			default:
				label = fmt.Sprintf("unknown     %s", c.Type)
				actual = "error"
			}
			status := "PASS"
			if actual != c.Expected {
				status = "FAIL"
				failures = append(failures, label)
			}
			fmt.Fprintf(os.Stdout, "  %s  %-65s got=%-5s expected=%s\n", status, label, actual, c.Expected)
		}
		Expect(failures).To(BeEmpty(), "failed checks: %v", failures)
	})
})
