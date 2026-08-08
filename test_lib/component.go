// Assisted by Claude Opus 4.6
package test

import (
	_ "embed"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	"gopkg.in/yaml.v3"
)

//go:embed cluster.yaml
var clusterYAMLData []byte

type ClusterConfig struct {
	Spec ClusterConfigSpec `yaml:"spec"`
}

type ClusterConfigSpec struct {
	Compliance ComplianceSpec `yaml:"compliance"`
	Nodes      []NodeSpec     `yaml:"nodes"`
}

type ComplianceSpec struct {
	FipsEnabled bool `yaml:"fipsEnabled"`
}

type ResourceNames struct {
	GpuModel string `yaml:"nvidia.com/gpu"`
	CpuModel string `yaml:"cpu"`
}

type SanitySpec struct {
	GpuCount       int           `yaml:"nvidia.com/gpu"`
	CpuCount       int           `yaml:"cpu"`
	Memory         string        `yaml:"memory"`
	ResourceNames  ResourceNames `yaml:"resourceNames"`
	Nvlink         string        `yaml:"nvlink"`
	NvlinkTopology string        `yaml:"nvlinkTopology"`
	PcieWidth      int           `yaml:"pcieWidth"`
	PcieGen        int           `yaml:"pcieGen"`
	NumaNodes      int           `yaml:"numaNodes"`
}

type IdealSpec struct {
	CudaDriverVersion    string   `yaml:"cudaDriverVersion"`
	GpuPowerLimit        int      `yaml:"gpuPowerLimit"`
	GpuPersistenceMode   bool     `yaml:"gpuPersistenceMode"`
	Kernel               string   `yaml:"kernel"`
	Hugepages            string   `yaml:"hugepages"`
	CpuFreqGovernor      string   `yaml:"cpuFreqGovernor"`
	CpuIdleDriver        string   `yaml:"cpuIdleDriver"`
	CpuIdleGovernor      string   `yaml:"cpuIdleGovernor"`
	CpuCStatesEnabled    []string `yaml:"cpuCStatesEnabled"`
	TransparentHugepages string   `yaml:"transparentHugepages"`
}

type ComponentValidation struct {
	Sanity SanitySpec `yaml:"sanity"`
	Ideal  IdealSpec  `yaml:"ideal"`
}

type NodeSpec struct {
	Name                string              `yaml:"name"`
	ComponentValidation ComponentValidation `yaml:"componentValidation"`
}

var (
	sanity     SanitySpec
	ideal      IdealSpec
	compliance ComplianceSpec
	componentResultsDir string
)

func TestComponent(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "Component Validation Suite")
}

var _ = BeforeSuite(func() {
	componentResultsDir = os.Getenv("RESULTS_DIR")
	Expect(componentResultsDir).NotTo(BeEmpty(), "RESULTS_DIR must be set")

	nodeName := os.Getenv("NODE_NAME")
	Expect(nodeName).NotTo(BeEmpty(), "NODE_NAME must be set")

	var cluster ClusterConfig
	err := yaml.Unmarshal(clusterYAMLData, &cluster)
	Expect(err).NotTo(HaveOccurred(), "Failed to parse embedded cluster.yaml")

	found := false
	for _, n := range cluster.Spec.Nodes {
		if n.Name == nodeName {
			sanity = n.ComponentValidation.Sanity
			ideal = n.ComponentValidation.Ideal
			found = true
			break
		}
	}
	Expect(found).To(BeTrue(), "Node %s not found in cluster config", nodeName)

	compliance = cluster.Spec.Compliance

	GinkgoWriter.Printf("Node: %s\n", nodeName)
	GinkgoWriter.Printf("Expected GPU: %dx %s\n", sanity.GpuCount, sanity.ResourceNames.GpuModel)
	GinkgoWriter.Printf("Expected CPU: %s (count: %d)\n", sanity.ResourceNames.CpuModel, sanity.CpuCount)
	GinkgoWriter.Printf("Expected memory: %s\n", sanity.Memory)
})

func nvidiaSmiQuery(field string) string {
	output, err := exec.Command("nvidia-smi",
		"--query-gpu="+field, "--format=csv,noheader,nounits").CombinedOutput()
	ExpectWithOffset(1, err).NotTo(HaveOccurred(),
		"nvidia-smi query %s failed: %s", field, string(output))
	lines := strings.Split(strings.TrimSpace(string(output)), "\n")
	ExpectWithOffset(1, lines).NotTo(BeEmpty())
	return strings.TrimSpace(lines[0])
}

func nvidiaSmiQueryAll(field string) []string {
	output, err := exec.Command("nvidia-smi",
		"--query-gpu="+field, "--format=csv,noheader,nounits").CombinedOutput()
	ExpectWithOffset(1, err).NotTo(HaveOccurred(),
		"nvidia-smi query %s failed: %s", field, string(output))
	var results []string
	for _, line := range strings.Split(strings.TrimSpace(string(output)), "\n") {
		results = append(results, strings.TrimSpace(line))
	}
	return results
}

func readSysfs(path string) string {
	data, err := os.ReadFile(path)
	ExpectWithOffset(1, err).NotTo(HaveOccurred(), "Failed to read %s", path)
	return strings.TrimSpace(string(data))
}

func readSysfsWithFallback(paths ...string) string {
	for _, p := range paths {
		data, err := os.ReadFile(p)
		if err == nil {
			return strings.TrimSpace(string(data))
		}
	}
	Fail(fmt.Sprintf("None of the paths were readable: %v", paths))
	return ""
}

// ---------------------------------------------------------------------------
// Sanity checks
// ---------------------------------------------------------------------------

var _ = Describe("Sanity Checks", Label("pass-fail"), func() {

	It("should detect correct GPU count", func() {
		output, err := exec.Command("nvidia-smi", "-L").CombinedOutput()
		Expect(err).NotTo(HaveOccurred(), "nvidia-smi failed: %s", string(output))

		count := 0
		for _, line := range strings.Split(strings.TrimSpace(string(output)), "\n") {
			if strings.HasPrefix(line, "GPU ") {
				count++
			}
		}
		GinkgoWriter.Printf("GPU count: %d (expected: %d)\n", count, sanity.GpuCount)
		Expect(count).To(Equal(sanity.GpuCount),
			"GPU count mismatch: nvidia-smi reports %d GPU(s) but the cluster config expects %d. "+
				"This means the node is either missing GPUs (hardware fault, driver issue) or the "+
				"cluster config has the wrong nvidia.com/gpu count for this node.", count, sanity.GpuCount)
	})

	It("should detect correct GPU model", func() {
		gpuName := nvidiaSmiQuery("name")
		actual := strings.ReplaceAll(gpuName, " ", "-")
		GinkgoWriter.Printf("GPU model: %s (expected: %s)\n", actual, sanity.ResourceNames.GpuModel)
		Expect(actual).To(Equal(sanity.ResourceNames.GpuModel),
			"GPU model mismatch: nvidia-smi reports '%s' but the cluster config expects '%s'. "+
				"This node may have been assigned the wrong spec in cluster.yaml, or GPUs were "+
				"physically swapped without updating the config.", actual, sanity.ResourceNames.GpuModel)
	})

	It("should have correct NVLink width", func() {
		output, err := exec.Command("nvidia-smi", "topo", "-m").CombinedOutput()
		Expect(err).NotTo(HaveOccurred(), "nvidia-smi topo failed: %s", string(output))

		nvRe := regexp.MustCompile(`NV(\d+)`)
		var widths []string
		for _, line := range strings.Split(string(output), "\n") {
			if !strings.HasPrefix(line, "GPU") {
				continue
			}
			for _, match := range nvRe.FindAllString(line, -1) {
				widths = append(widths, match)
			}
		}
		Expect(widths).NotTo(BeEmpty(), "No NVLink connections found in nvidia-smi topo output. "+
			"The GPUs may not be NVLink-capable, or the topology query format has changed.")
		for _, w := range widths {
			GinkgoWriter.Printf("NVLink connection: %s (expected: %s)\n", w, sanity.Nvlink)
			Expect(w).To(Equal(sanity.Nvlink),
				"NVLink width mismatch: detected '%s' but expected '%s'. "+
					"A lower NVLink width (e.g. NV4 vs NV12) degrades GPU-to-GPU bandwidth "+
					"and will bottleneck distributed training workloads.", w, sanity.Nvlink)
		}
	})

	It("should have correct NVLink topology", func() {
		if sanity.NvlinkTopology != "all-to-all" {
			Skip("Only all-to-all topology validation is supported")
		}

		output, err := exec.Command("nvidia-smi", "topo", "-m").CombinedOutput()
		Expect(err).NotTo(HaveOccurred(), "nvidia-smi topo failed: %s", string(output))

		nvRe := regexp.MustCompile(`NV\d+`)
		gpuLines := 0
		for _, line := range strings.Split(string(output), "\n") {
			if !strings.HasPrefix(line, "GPU") {
				continue
			}
			gpuLines++
			fields := strings.Fields(line)
			// fields[0] = "GPU0", fields[1:] = connection types to other GPUs/NICs
			// GPU-to-GPU connections should all be NV#
			gpuConnections := 0
			nvConnections := 0
			for _, f := range fields[1:] {
				if f == "X" {
					continue
				}
				if strings.HasPrefix(f, "GPU") || strings.HasPrefix(f, "NV") ||
					f == "SYS" || f == "NODE" || f == "PHB" || f == "PXB" || f == "PIX" {
					if nvRe.MatchString(f) {
						nvConnections++
					}
					// Count only up to GpuCount-1 fields as GPU connections
					gpuConnections++
					if gpuConnections >= sanity.GpuCount-1 {
						break
					}
				}
			}
			GinkgoWriter.Printf("GPU line %d: %d/%d connections are NVLink\n",
				gpuLines, nvConnections, sanity.GpuCount-1)
			Expect(nvConnections).To(Equal(sanity.GpuCount-1),
				"NVLink topology mismatch on GPU line %d: only %d of %d GPU-to-GPU connections "+
					"use NVLink. For all-to-all topology, every GPU must have a direct NVLink path "+
					"to every other GPU. Missing links force traffic through PCIe/SYS, which is "+
					"orders of magnitude slower for NCCL collectives.", gpuLines, nvConnections, sanity.GpuCount-1)
		}
		Expect(gpuLines).To(Equal(sanity.GpuCount),
			"GPU topology row count mismatch: nvidia-smi topo shows %d GPU rows but expected %d. "+
				"Some GPUs may not be visible to the driver.", gpuLines, sanity.GpuCount)
	})

	It("should report correct PCIe link width", func() {
		for i, w := range nvidiaSmiQueryAll("pcie.link.width.current") {
			GinkgoWriter.Printf("PCIe width: %s (expected: %d)\n", w, sanity.PcieWidth)
			actual, err := strconv.Atoi(w)
			Expect(err).NotTo(HaveOccurred())
			Expect(actual).To(Equal(sanity.PcieWidth),
				"PCIe link width mismatch on GPU %d: running at x%d but expected x%d. "+
					"A narrower link (e.g. x8 vs x16) halves the host-to-GPU bandwidth. "+
					"Common causes: GPU not fully seated in the slot, or a PCIe riser/cable issue.",
				i, actual, sanity.PcieWidth)
		}
	})

	It("should report correct PCIe generation", func() {
		for i, g := range nvidiaSmiQueryAll("pcie.link.gen.current") {
			GinkgoWriter.Printf("PCIe gen: %s (expected: %d)\n", g, sanity.PcieGen)
			actual, err := strconv.Atoi(g)
			Expect(err).NotTo(HaveOccurred())
			Expect(actual).To(Equal(sanity.PcieGen),
				"PCIe generation mismatch on GPU %d: running at Gen%d but expected Gen%d. "+
					"A lower generation (e.g. Gen4 vs Gen5) reduces per-lane throughput. "+
					"Check that the slot supports the expected generation and that no BIOS "+
					"setting is capping it.", i, actual, sanity.PcieGen)
		}
	})

	It("should match expected CPU model", func() {
		data, err := os.ReadFile("/proc/cpuinfo")
		Expect(err).NotTo(HaveOccurred())

		var actual string
		for _, line := range strings.Split(string(data), "\n") {
			parts := strings.SplitN(line, ":", 2)
			if len(parts) == 2 && strings.TrimSpace(parts[0]) == "model name" {
				actual = strings.TrimSpace(parts[1])
				break
			}
		}
		Expect(actual).NotTo(BeEmpty(), "model name not found in /proc/cpuinfo")
		GinkgoWriter.Printf("CPU model: %s (expected: %s)\n", actual, sanity.ResourceNames.CpuModel)
		Expect(actual).To(Equal(sanity.ResourceNames.CpuModel),
			"CPU model mismatch: /proc/cpuinfo reports '%s' but the cluster config expects '%s'. "+
				"This node may have been assigned the wrong spec in cluster.yaml, or the pod "+
				"scheduled on the wrong node.", actual, sanity.ResourceNames.CpuModel)
	})

	It("should report correct CPU count", func() {
		data, err := os.ReadFile("/proc/cpuinfo")
		Expect(err).NotTo(HaveOccurred())

		count := 0
		for _, line := range strings.Split(string(data), "\n") {
			if strings.HasPrefix(line, "processor") {
				count++
			}
		}
		GinkgoWriter.Printf("CPU count: %d (expected: %d)\n", count, sanity.CpuCount)
		Expect(count).To(Equal(sanity.CpuCount),
			"CPU count mismatch: /proc/cpuinfo shows %d logical CPUs but the cluster config "+
				"expects %d. If lower, check whether CPU cores are offline or the container's "+
				"cpuset is restricted. If higher, the cluster config may be outdated.",
			count, sanity.CpuCount)
	})

	It("should report memory capacity within expected range", func() {
		data, err := os.ReadFile("/proc/meminfo")
		Expect(err).NotTo(HaveOccurred())

		memTotalKB := extractMemInfoKB(string(data), "MemTotal")
		Expect(memTotalKB).NotTo(BeZero(), "MemTotal not found in /proc/meminfo")

		memTotalGi := float64(memTotalKB) / (1024 * 1024)
		expectedGi := parseGi(sanity.Memory)
		GinkgoWriter.Printf("Memory: %.1f Gi (expected: %.1f Gi)\n", memTotalGi, expectedGi)
		Expect(memTotalGi).To(BeNumerically("~", expectedGi, expectedGi*0.05),
			"Memory capacity mismatch: /proc/meminfo reports %.1f Gi but the cluster config "+
				"expects %.1f Gi (tolerance: 5%%). A significant shortfall may indicate failed "+
				"DIMMs or a BIOS memory configuration issue.", memTotalGi, expectedGi)
	})

	It("should detect correct NUMA node count", func() {
		entries, err := filepath.Glob("/sys/devices/system/node/node[0-9]*")
		Expect(err).NotTo(HaveOccurred())

		count := len(entries)
		GinkgoWriter.Printf("NUMA nodes: %d (expected: %d)\n", count, sanity.NumaNodes)
		Expect(count).To(Equal(sanity.NumaNodes),
			"NUMA node count mismatch: found %d NUMA nodes but expected %d. "+
				"Wrong NUMA topology affects memory locality for GPU workloads "+
				"and can cause cross-socket memory access penalties.", count, sanity.NumaNodes)
	})
})

// ---------------------------------------------------------------------------
// Ideal checks
// ---------------------------------------------------------------------------

var _ = Describe("Ideal Configuration", Label("pass-fail"), func() {

	It("should report correct CUDA driver version", func() {
		actual := nvidiaSmiQuery("driver_version")
		GinkgoWriter.Printf("CUDA driver: %s (expected: %s)\n", actual, ideal.CudaDriverVersion)
		Expect(actual).To(Equal(ideal.CudaDriverVersion),
			"CUDA driver version mismatch: node has '%s' but expected '%s'. "+
				"Mismatched driver versions across nodes can cause silent failures "+
				"in distributed training. Check whether the GPU operator deployed "+
				"the expected driver daemonset.", actual, ideal.CudaDriverVersion)
	})

	It("should have GPU power limit at expected value", func() {
		for _, raw := range nvidiaSmiQueryAll("power.limit") {
			watts, err := strconv.ParseFloat(raw, 64)
			Expect(err).NotTo(HaveOccurred(), "failed to parse power.limit: %s", raw)
			actual := int(watts)
			GinkgoWriter.Printf("GPU power limit: %d W (expected: %d W)\n", actual, ideal.GpuPowerLimit)
			Expect(actual).To(Equal(ideal.GpuPowerLimit),
				"GPU power limit mismatch: set to %d W but expected %d W. "+
					"A lower power limit throttles GPU clock speeds and reduces training "+
					"throughput. Check nvidia-smi or the GPU operator's power management config.",
				actual, ideal.GpuPowerLimit)
		}
	})

	It("should have GPU persistence mode set correctly", func() {
		actual := nvidiaSmiQuery("persistence_mode")
		expected := "Disabled"
		if ideal.GpuPersistenceMode {
			expected = "Enabled"
		}
		GinkgoWriter.Printf("GPU persistence mode: %s (expected: %s)\n", actual, expected)
		Expect(actual).To(Equal(expected),
			"GPU persistence mode is '%s' but expected '%s'. "+
				"When persistence mode is disabled, the GPU driver unloads between jobs, "+
				"adding several seconds of cold-start latency to every GPU workload.",
			actual, expected)
	})

	It("should match expected kernel version", func() {
		output, err := exec.Command("uname", "-r").CombinedOutput()
		Expect(err).NotTo(HaveOccurred(), "uname failed: %s", string(output))

		actual := strings.TrimSpace(string(output))
		GinkgoWriter.Printf("Kernel: %s (expected: %s)\n", actual, ideal.Kernel)
		Expect(actual).To(Equal(ideal.Kernel),
			"Kernel version mismatch: running '%s' but expected '%s'. "+
				"A different kernel version may lack required drivers, security patches, "+
				"or feature support (e.g. IPsec full offload requires kernel 6.12+). "+
				"Check that MachineConfig is applying the correct OS image.", actual, ideal.Kernel)
	})

	It("should have hugepages configured correctly", func() {
		data := readSysfs("/sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages")
		actualPages, err := strconv.Atoi(data)
		Expect(err).NotTo(HaveOccurred())

		expectedMi := parseMi(ideal.Hugepages)
		expectedPages := expectedMi / 2
		GinkgoWriter.Printf("Hugepages 2Mi: %d pages (expected: %d)\n", actualPages, expectedPages)
		Expect(actualPages).To(Equal(expectedPages),
			"Hugepages mismatch: %d 2Mi pages allocated but expected %d (= %s). "+
				"Insufficient hugepages starve DPDK and RDMA-based workloads. "+
				"Check the node's kernel boot parameters or the Tuned/PerformanceProfile config.",
			actualPages, expectedPages, ideal.Hugepages)
	})

	It("should have CPU frequency governor set to expected value", func() {
		actual := readSysfs("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
		GinkgoWriter.Printf("CPU freq governor: %s (expected: %s)\n", actual, ideal.CpuFreqGovernor)
		Expect(actual).To(Equal(ideal.CpuFreqGovernor),
			"CPU frequency governor mismatch: running '%s' but expected '%s'. "+
				"'powersave' or 'ondemand' governors throttle CPU frequency under load, "+
				"adding latency to data preprocessing and CPU-bound inference steps. "+
				"'performance' governor keeps all cores at max frequency.",
			actual, ideal.CpuFreqGovernor)
	})

	It("should have correct CPU idle driver", func() {
		actual := readSysfs("/sys/devices/system/cpu/cpuidle/current_driver")
		GinkgoWriter.Printf("CPU idle driver: %s (expected: %s)\n", actual, ideal.CpuIdleDriver)
		Expect(actual).To(Equal(ideal.CpuIdleDriver),
			"CPU idle driver mismatch: running '%s' but expected '%s'. "+
				"The idle driver controls which C-states are available. Using the wrong "+
				"driver (e.g. intel_idle vs acpi_idle) can enable deep sleep states that "+
				"add microseconds of wake-up latency to interrupt handling.",
			actual, ideal.CpuIdleDriver)
	})

	It("should have correct CPU idle governor", func() {
		actual := readSysfsWithFallback(
			"/sys/devices/system/cpu/cpuidle/current_governor_ro",
			"/sys/devices/system/cpu/cpuidle/current_governor",
		)
		GinkgoWriter.Printf("CPU idle governor: %s (expected: %s)\n", actual, ideal.CpuIdleGovernor)
		Expect(actual).To(Equal(ideal.CpuIdleGovernor),
			"CPU idle governor mismatch: running '%s' but expected '%s'. "+
				"The idle governor decides when to enter deeper C-states. "+
				"For latency-sensitive GPU workloads, 'menu' is standard; "+
				"'ladder' may cause excessive deep-sleep transitions.",
			actual, ideal.CpuIdleGovernor)
	})

	It("should have only expected C-states enabled", func() {
		states, err := filepath.Glob("/sys/devices/system/cpu/cpu0/cpuidle/state[0-9]*")
		Expect(err).NotTo(HaveOccurred())
		Expect(states).NotTo(BeEmpty(), "No cpuidle states found")

		var enabled []string
		for _, s := range states {
			disabled := readSysfs(filepath.Join(s, "disable"))
			if disabled == "0" {
				name := readSysfs(filepath.Join(s, "name"))
				enabled = append(enabled, name)
			}
		}

		GinkgoWriter.Printf("Enabled C-states: %v (expected: %v)\n", enabled, ideal.CpuCStatesEnabled)
		Expect(enabled).To(Equal(ideal.CpuCStatesEnabled),
			"C-state mismatch: enabled states are %v but expected %v. "+
				"Deep C-states (C3+) save power but add wake-up latency that impacts "+
				"interrupt-driven workloads like RDMA and GPU data transfers. "+
				"For AI nodes, typically only POLL and C1 should be enabled.",
			enabled, ideal.CpuCStatesEnabled)
	})

	It("should have transparent hugepages set correctly", func() {
		raw := readSysfs("/sys/kernel/mm/transparent_hugepage/enabled")
		// format: "always [madvise] never" — active value is in brackets
		re := regexp.MustCompile(`\[(\w+)\]`)
		match := re.FindStringSubmatch(raw)
		Expect(match).To(HaveLen(2), "Could not parse THP setting from: %s", raw)

		actual := match[1]
		GinkgoWriter.Printf("Transparent hugepages: %s (expected: %s)\n", actual, ideal.TransparentHugepages)
		Expect(actual).To(Equal(ideal.TransparentHugepages),
			"Transparent hugepages (THP) mismatch: set to '%s' but expected '%s'. "+
				"THP set to 'always' can cause unpredictable latency spikes from "+
				"background compaction (khugepaged). 'madvise' lets applications "+
				"opt in explicitly; 'never' disables THP entirely.",
			actual, ideal.TransparentHugepages)
	})
})

// ---------------------------------------------------------------------------
// Compliance checks
// ---------------------------------------------------------------------------

var _ = Describe("Compliance Checks", Label("pass-fail"), func() {

	It("should have FIPS mode enabled", func() {
		if !compliance.FipsEnabled {
			Skip("FIPS mode not required by cluster config")
		}

		actual := readSysfs("/proc/sys/crypto/fips_enabled")
		GinkgoWriter.Printf("FIPS mode: %s (expected: 1)\n", actual)
		Expect(actual).To(Equal("1"),
			"FIPS mode is not enabled on this node (/proc/sys/crypto/fips_enabled = '%s'). "+
				"NIST 800-171 requires FIPS-validated cryptographic modules (requirement 3.13.11). "+
				"FIPS mode must be enabled at OpenShift install time via the install-config.yaml "+
				"'fips: true' setting — it cannot be turned on after installation without a "+
				"full cluster reinstall.", actual)
	})
})

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func extractMemInfoKB(meminfo, field string) int64 {
	for _, line := range strings.Split(meminfo, "\n") {
		if strings.HasPrefix(line, field+":") {
			parts := strings.Fields(line)
			if len(parts) >= 2 {
				val, _ := strconv.ParseInt(parts[1], 10, 64)
				return val
			}
		}
	}
	return 0
}

func parseGi(s string) float64 {
	s = strings.TrimSuffix(s, "Gi")
	val, _ := strconv.ParseFloat(s, 64)
	return val
}

func parseMi(s string) int {
	s = strings.TrimSuffix(s, "Mi")
	val, _ := strconv.Atoi(s)
	return val
}
