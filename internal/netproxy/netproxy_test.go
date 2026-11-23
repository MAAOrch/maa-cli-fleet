package netproxy

import (
	"net/http"
	"net/url"
	"testing"
)

func TestParseProxyServerFormats(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"", ""},
		{"127.0.0.1:7890", "http://127.0.0.1:7890"},
		{"http://127.0.0.1:7890", "http://127.0.0.1:7890"},
		{"http=127.0.0.1:7890;https=127.0.0.1:7891", "http://127.0.0.1:7891"},
		{"http=127.0.0.1:7890;ftp=127.0.0.1:7892", "http://127.0.0.1:7890"},
	}
	for _, tc := range cases {
		if got := parseProxyServer(tc.in); got != tc.want {
			t.Errorf("parseProxyServer(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestProxyBypassAndPublicRouting(t *testing.T) {
	fn := funcForServer("http=127.0.0.1:7890;https=127.0.0.1:7890")
	if fn == nil {
		t.Fatal("expected non-nil proxy func")
	}

	bypass := []string{
		"http://localhost:8790/api/health",
		"http://127.0.0.1:8790/api/health",
		"http://[::1]:8790/api/health",
		"http://192.168.1.5:8080/",
		"http://10.0.0.2/",
		"http://172.16.5.1/",
		"http://100.80.1.2/", // Tailscale CGNAT
	}
	for _, raw := range bypass {
		u, _ := url.Parse(raw)
		p, err := fn(&http.Request{URL: u})
		if err != nil || p != nil {
			t.Errorf("expected bypass for %s, got (%v, %v)", raw, p, err)
		}
	}

	proxied := []string{
		"https://github.com/MAAOrch/maa-cli-fleet",
		"https://172.64.155.209/", // Cloudflare public IP, must NOT bypass
	}
	for _, raw := range proxied {
		u, _ := url.Parse(raw)
		p, err := fn(&http.Request{URL: u})
		if err != nil || p == nil || p.Host != "127.0.0.1:7890" {
			t.Errorf("expected proxy for %s, got (%v, %v)", raw, p, err)
		}
	}
}
