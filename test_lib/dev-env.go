// Assisted by Claude Opus 4.6
package test

import (
	"bufio"
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
)

// ---------------------------------------------------------------------------
// Data types
// ---------------------------------------------------------------------------

type DiagResult struct {
	GPUCount      int          `json:"gpu_count"`
	Devices       []DeviceInfo `json:"devices"`
	NvidiaSmiTopo string       `json:"nvidia_smi_topo"`
}

type DeviceInfo struct {
	Name                string `json:"name"`
	TotalMemoryMB       int    `json:"total_memory_mb"`
	Major               int    `json:"major"`
	Minor               int    `json:"minor"`
	MultiProcessorCount int    `json:"multi_processor_count"`
}

type jupyterContents struct {
	Name    string `json:"name"`
	Path    string `json:"path"`
	Type    string `json:"type"`
	Content string `json:"content"`
}

type kernelInfo struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

type kernelMsg struct {
	Header       msgHeader              `json:"header"`
	ParentHeader msgHeader              `json:"parent_header"`
	Metadata     map[string]interface{} `json:"metadata"`
	Content      map[string]interface{} `json:"content"`
	Buffers      []interface{}          `json:"buffers"`
	Channel      string                 `json:"channel"`
}

type msgHeader struct {
	MsgID    string `json:"msg_id"`
	MsgType  string `json:"msg_type"`
	Username string `json:"username"`
	Session  string `json:"session"`
	Version  string `json:"version"`
}

// ---------------------------------------------------------------------------
// Minimal websocket client (standard library only)
// ---------------------------------------------------------------------------

type wsConn struct {
	conn net.Conn
	br   *bufio.Reader
}

func wsDial(httpURL, path string) (*wsConn, error) {
	u, err := url.Parse(httpURL)
	if err != nil {
		return nil, err
	}
	host := u.Host
	if !strings.Contains(host, ":") {
		host += ":80"
	}

	conn, err := net.DialTimeout("tcp", host, 10*time.Second)
	if err != nil {
		return nil, err
	}

	keyBytes := make([]byte, 16)
	rand.Read(keyBytes)
	key := base64.StdEncoding.EncodeToString(keyBytes)

	reqStr := fmt.Sprintf(
		"GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n",
		path, u.Host, key)

	if _, err := conn.Write([]byte(reqStr)); err != nil {
		conn.Close()
		return nil, err
	}

	br := bufio.NewReader(conn)
	resp, err := http.ReadResponse(br, nil)
	if err != nil {
		conn.Close()
		return nil, err
	}
	if resp.StatusCode != 101 {
		conn.Close()
		return nil, fmt.Errorf("websocket upgrade failed: status %d", resp.StatusCode)
	}

	return &wsConn{conn: conn, br: br}, nil
}

func (ws *wsConn) writeText(data []byte) error {
	frame := []byte{0x81}
	n := len(data)
	switch {
	case n < 126:
		frame = append(frame, byte(n)|0x80)
	case n < 65536:
		frame = append(frame, 126|0x80, byte(n>>8), byte(n))
	default:
		frame = append(frame, 127|0x80)
		b := make([]byte, 8)
		binary.BigEndian.PutUint64(b, uint64(n))
		frame = append(frame, b...)
	}
	mask := make([]byte, 4)
	rand.Read(mask)
	frame = append(frame, mask...)
	masked := make([]byte, n)
	for i, b := range data {
		masked[i] = b ^ mask[i%4]
	}
	_, err := ws.conn.Write(append(frame, masked...))
	return err
}

func (ws *wsConn) readMessage() (int, []byte, error) {
	hdr := make([]byte, 2)
	if _, err := io.ReadFull(ws.br, hdr); err != nil {
		return 0, nil, err
	}
	opcode := int(hdr[0] & 0x0f)
	isMasked := (hdr[1] & 0x80) != 0
	length := int64(hdr[1] & 0x7f)
	if length == 126 {
		ext := make([]byte, 2)
		if _, err := io.ReadFull(ws.br, ext); err != nil {
			return 0, nil, err
		}
		length = int64(binary.BigEndian.Uint16(ext))
	} else if length == 127 {
		ext := make([]byte, 8)
		if _, err := io.ReadFull(ws.br, ext); err != nil {
			return 0, nil, err
		}
		length = int64(binary.BigEndian.Uint64(ext))
	}
	var mask []byte
	if isMasked {
		mask = make([]byte, 4)
		if _, err := io.ReadFull(ws.br, mask); err != nil {
			return 0, nil, err
		}
	}
	payload := make([]byte, length)
	if _, err := io.ReadFull(ws.br, payload); err != nil {
		return 0, nil, err
	}
	if isMasked {
		for i := range payload {
			payload[i] ^= mask[i%4]
		}
	}
	return opcode, payload, nil
}

func (ws *wsConn) readText() ([]byte, error) {
	for {
		opcode, payload, err := ws.readMessage()
		if err != nil {
			return nil, err
		}
		switch opcode {
		case 1:
			return payload, nil
		case 8:
			return nil, fmt.Errorf("websocket closed by server")
		case 9:
			pong := []byte{0x8a}
			n := len(payload)
			if n < 126 {
				pong = append(pong, byte(n)|0x80)
			}
			mask := make([]byte, 4)
			rand.Read(mask)
			pong = append(pong, mask...)
			for i, b := range payload {
				pong = append(pong, b^mask[i%4])
			}
			ws.conn.Write(pong)
		}
	}
}

func (ws *wsConn) close() {
	frame := []byte{0x88, 0x82}
	mask := make([]byte, 4)
	rand.Read(mask)
	frame = append(frame, mask...)
	frame = append(frame, 0x03^mask[0], 0xE8^mask[1])
	ws.conn.Write(frame)
	ws.conn.Close()
}

// ---------------------------------------------------------------------------
// Jupyter API helpers
// ---------------------------------------------------------------------------

func randHex() string {
	b := make([]byte, 16)
	rand.Read(b)
	return fmt.Sprintf("%x", b)
}

func jupyterPut(baseURL, path string, body interface{}) error {
	data, err := json.Marshal(body)
	if err != nil {
		return err
	}
	req, err := http.NewRequest("PUT", baseURL+"/api/contents/"+path,
		strings.NewReader(string(data)))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 && resp.StatusCode != 201 {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("PUT %s returned %d: %s", path, resp.StatusCode, string(b))
	}
	return nil
}

func jupyterGet(baseURL, path string) ([]byte, error) {
	resp, err := http.Get(baseURL + "/api/contents/" + path)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("GET %s returned %d", path, resp.StatusCode)
	}
	return io.ReadAll(resp.Body)
}

func startKernel(baseURL string) (string, error) {
	resp, err := http.Post(baseURL+"/api/kernels", "application/json",
		strings.NewReader(`{"name":"python3"}`))
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 201 && resp.StatusCode != 200 {
		b, _ := io.ReadAll(resp.Body)
		return "", fmt.Errorf("start kernel returned %d: %s", resp.StatusCode, string(b))
	}
	var ki kernelInfo
	json.NewDecoder(resp.Body).Decode(&ki)
	return ki.ID, nil
}

func shutdownKernel(baseURL, id string) {
	req, _ := http.NewRequest("DELETE", baseURL+"/api/kernels/"+id, nil)
	http.DefaultClient.Do(req)
}

func executeOnKernel(baseURL, id, code string, timeout time.Duration) (string, error) {
	wsPath := fmt.Sprintf("/api/kernels/%s/channels", id)
	ws, err := wsDial(baseURL, wsPath)
	if err != nil {
		return "", fmt.Errorf("websocket connect: %w", err)
	}
	defer ws.close()

	ws.conn.SetDeadline(time.Now().Add(timeout))

	session := randHex()
	msgID := randHex()

	msg := kernelMsg{
		Header: msgHeader{
			MsgID:   msgID,
			MsgType: "execute_request",
			Session: session,
			Version: "5.3",
		},
		ParentHeader: msgHeader{},
		Content: map[string]interface{}{
			"code":             code,
			"silent":           false,
			"store_history":    false,
			"user_expressions": map[string]interface{}{},
			"allow_stdin":      false,
			"stop_on_error":    true,
		},
		Metadata: map[string]interface{}{},
		Buffers:  []interface{}{},
		Channel:  "shell",
	}

	data, _ := json.Marshal(msg)
	if err := ws.writeText(data); err != nil {
		return "", fmt.Errorf("send execute_request: %w", err)
	}

	var stdout strings.Builder
	for {
		raw, err := ws.readText()
		if err != nil {
			return stdout.String(), fmt.Errorf("read: %w", err)
		}

		var reply kernelMsg
		if err := json.Unmarshal(raw, &reply); err != nil {
			continue
		}

		switch reply.Header.MsgType {
		case "stream":
			if reply.Content["name"] == "stdout" {
				if text, ok := reply.Content["text"].(string); ok {
					stdout.WriteString(text)
				}
			}
		case "error":
			ename, _ := reply.Content["ename"].(string)
			evalue, _ := reply.Content["evalue"].(string)
			return "", fmt.Errorf("kernel error: %s: %s", ename, evalue)
		case "execute_reply":
			status, _ := reply.Content["status"].(string)
			if status == "error" {
				ename, _ := reply.Content["ename"].(string)
				evalue, _ := reply.Content["evalue"].(string)
				return "", fmt.Errorf("execution failed: %s: %s", ename, evalue)
			}
			return stdout.String(), nil
		}
	}
}

// ---------------------------------------------------------------------------
// Test variables
// ---------------------------------------------------------------------------

var (
	serverURL        string
	expectedGPUCount int
	expectedNvlink   string
	devEnvKernelID   string
	diagResult       DiagResult
)

// ---------------------------------------------------------------------------
// Ginkgo
// ---------------------------------------------------------------------------

func TestDevEnv(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "Dev Environment Validation Suite")
}

var _ = BeforeSuite(func() {
	serverURL = os.Getenv("SERVER_URL")
	Expect(serverURL).NotTo(BeEmpty(), "SERVER_URL must be set")

	raw := os.Getenv("EXPECTED_GPU_COUNT")
	Expect(raw).NotTo(BeEmpty(), "EXPECTED_GPU_COUNT must be set")
	var err error
	expectedGPUCount, err = strconv.Atoi(raw)
	Expect(err).NotTo(HaveOccurred(), "EXPECTED_GPU_COUNT must be an integer")

	expectedNvlink = os.Getenv("EXPECTED_NVLINK")
	Expect(expectedNvlink).NotTo(BeEmpty(), "EXPECTED_NVLINK must be set")

	GinkgoWriter.Printf("Server URL: %s\n", serverURL)
	GinkgoWriter.Printf("Expected GPU count: %d\n", expectedGPUCount)
	GinkgoWriter.Printf("Expected NVLink: %s\n", expectedNvlink)
})

var _ = AfterSuite(func() {
	if devEnvKernelID != "" {
		shutdownKernel(serverURL, devEnvKernelID)
	}
})

const diagnosticCode = `import torch, json, subprocess

result = {}
result["gpu_count"] = torch.cuda.device_count()
result["devices"] = []
for i in range(torch.cuda.device_count()):
    props = torch.cuda.get_device_properties(i)
    result["devices"].append({
        "name": props.name,
        "total_memory_mb": props.total_memory // (1024 * 1024),
        "major": props.major,
        "minor": props.minor,
        "multi_processor_count": props.multi_processor_count,
    })

topo = subprocess.run(["nvidia-smi", "topo", "-m"], capture_output=True, text=True)
result["nvidia_smi_topo"] = topo.stdout

with open("/uat_workspace/result.txt", "w") as f:
    json.dump(result, f, indent=2)

print("DIAG_OK")
`

var _ = Describe("Dev Environment Validation", Label("pass-fail"), Ordered, func() {

	It("should create a diagnostic notebook on the server", func() {
		notebook := map[string]interface{}{
			"type":   "notebook",
			"format": "json",
			"content": map[string]interface{}{
				"nbformat":       4,
				"nbformat_minor": 5,
				"metadata": map[string]interface{}{
					"kernelspec": map[string]interface{}{
						"display_name": "Python 3",
						"language":     "python",
						"name":         "python3",
					},
				},
				"cells": []map[string]interface{}{
					{
						"cell_type":       "code",
						"source":          diagnosticCode,
						"metadata":        map[string]interface{}{},
						"outputs":         []interface{}{},
						"execution_count": nil,
					},
					{
						"cell_type":       "code",
						"source":          "!nvidia-smi topo -m",
						"metadata":        map[string]interface{}{},
						"outputs":         []interface{}{},
						"execution_count": nil,
					},
				},
			},
		}

		err := jupyterPut(serverURL, "gpu-diagnostics.ipynb", notebook)
		Expect(err).NotTo(HaveOccurred(), "Failed to create notebook on Jupyter server")
		GinkgoWriter.Println("Created gpu-diagnostics.ipynb on server")
	})

	It("should start a kernel and execute the diagnostic code", func() {
		var err error
		devEnvKernelID, err = startKernel(serverURL)
		Expect(err).NotTo(HaveOccurred(), "Failed to start kernel")
		GinkgoWriter.Printf("Started kernel: %s\n", devEnvKernelID)

		output, err := executeOnKernel(serverURL, devEnvKernelID, diagnosticCode, 120*time.Second)
		Expect(err).NotTo(HaveOccurred(), "Failed to execute diagnostic code on kernel")
		Expect(output).To(ContainSubstring("DIAG_OK"),
			"Diagnostic code did not complete successfully, output: %s", output)
		GinkgoWriter.Println("Diagnostic code executed successfully")
	})

	It("should read and parse result.txt from the server", func() {
		body, err := jupyterGet(serverURL, "result.txt")
		Expect(err).NotTo(HaveOccurred(), "Failed to read result.txt from Jupyter server")

		var contents jupyterContents
		err = json.Unmarshal(body, &contents)
		Expect(err).NotTo(HaveOccurred(), "Failed to parse Jupyter API response")

		err = json.Unmarshal([]byte(contents.Content), &diagResult)
		Expect(err).NotTo(HaveOccurred(), "Failed to parse result.txt JSON")

		GinkgoWriter.Printf("Actual GPU count: %d\n", diagResult.GPUCount)
		GinkgoWriter.Printf("Device count: %d\n", len(diagResult.Devices))
	})

	It("should detect correct GPU count", func() {
		GinkgoWriter.Printf("GPU count: %d (expected: %d)\n", diagResult.GPUCount, expectedGPUCount)
		Expect(diagResult.GPUCount).To(Equal(expectedGPUCount),
			"GPU count mismatch: CUDA reports %d GPU(s) but the cluster config expects %d.",
			diagResult.GPUCount, expectedGPUCount)
	})

	It("should have correct NVLink width", func() {
		nvRe := regexp.MustCompile(`NV(\d+)`)
		var widths []string
		for _, line := range strings.Split(diagResult.NvidiaSmiTopo, "\n") {
			if !strings.HasPrefix(line, "GPU") {
				continue
			}
			for _, match := range nvRe.FindAllString(line, -1) {
				widths = append(widths, match)
			}
		}
		Expect(widths).NotTo(BeEmpty(),
			"No NVLink connections found in nvidia-smi topo output.")
		for _, w := range widths {
			GinkgoWriter.Printf("NVLink connection: %s (expected: %s)\n", w, expectedNvlink)
			Expect(w).To(Equal(expectedNvlink),
				"NVLink width mismatch: detected '%s' but expected '%s'.", w, expectedNvlink)
		}
	})

	It("should report all GPU devices", func() {
		Expect(diagResult.Devices).To(HaveLen(expectedGPUCount),
			"Device list length mismatch: got %d devices but expected %d.",
			len(diagResult.Devices), expectedGPUCount)
		for i, dev := range diagResult.Devices {
			GinkgoWriter.Printf("GPU %d: %s (%d MB)\n", i, dev.Name, dev.TotalMemoryMB)
		}
	})
})
