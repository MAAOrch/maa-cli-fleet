// Package netproxy —— 读取 Windows 系统代理（WinINET），供出网请求使用。
//
// 背景：很多国内机器只有走系统代理才能访问 GitHub；Go 默认直连会超时，
// 而 PowerShell 会自动用系统代理——这就是"PS 能下 59MB/s、Go 却连不上"的原因。
package netproxy

import (
	"net/http"
	"net/netip"
	"net/url"
	"os/exec"
	"strings"
)

var cgnatPrefix = netip.MustParsePrefix("100.64.0.0/10")

// Func 返回 http.Transport 用的 Proxy 函数：公网走系统代理，内网/本机直连。
func Func() func(*http.Request) (*url.URL, error) {
	return funcForServer(systemProxyServer())
}

func funcForServer(raw string) func(*http.Request) (*url.URL, error) {
	server := parseProxyServer(raw)
	if server == "" {
		return nil
	}
	u, err := url.Parse(server)
	if err != nil || u.Host == "" {
		return nil
	}
	return func(req *http.Request) (*url.URL, error) {
		if isLocalHost(req.URL.Hostname()) {
			return nil, nil
		}
		return u, nil
	}
}

// parseProxyServer 解析 WinINET ProxyServer，兼容 "127.0.0.1:7890"、"http://..."
// 以及按协议分项的 "http=127.0.0.1:7890;https=127.0.0.1:7890"。
func parseProxyServer(raw string) string {
	s := strings.TrimSpace(raw)
	if s == "" {
		return ""
	}
	if strings.Contains(s, "=") {
		byScheme := map[string]string{}
		for _, part := range strings.Split(s, ";") {
			part = strings.TrimSpace(part)
			if k, v, ok := strings.Cut(part, "="); ok {
				byScheme[strings.ToLower(strings.TrimSpace(k))] = strings.TrimSpace(v)
			}
		}
		picked := byScheme["https"]
		if picked == "" {
			picked = byScheme["http"]
		}
		if picked == "" {
			picked = byScheme["socks"]
		}
		s = picked
	}
	if s == "" {
		return ""
	}
	if !strings.Contains(s, "://") {
		s = "http://" + s
	}
	return s
}

func isLocalHost(h string) bool {
	if h == "" || strings.EqualFold(h, "localhost") {
		return true
	}
	if addr, err := netip.ParseAddr(h); err == nil {
		return addr.IsLoopback() || addr.IsPrivate() || addr.IsLinkLocalUnicast() || cgnatPrefix.Contains(addr)
	}
	return false
}

// NewClient 建一个带系统代理的 http.Client（Transport 从 DefaultTransport 克隆）。
func NewClient() *http.Client {
	t := http.DefaultTransport.(*http.Transport).Clone()
	if f := Func(); f != nil {
		t.Proxy = f
	}
	return &http.Client{Transport: t}
}

func systemProxyServer() string {
	enable := regQuery("ProxyEnable")
	if !strings.Contains(enable, "1") {
		return ""
	}
	return regQuery("ProxyServer")
}

func regQuery(name string) string {
	out, err := exec.Command("reg", "query",
		`HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings`, "/v", name).Output()
	if err != nil {
		return ""
	}
	// 形如：  ProxyServer    REG_SZ    http://127.0.0.1:7890
	fields := strings.Fields(string(out))
	if len(fields) >= 3 {
		return strings.TrimSpace(fields[len(fields)-1])
	}
	return ""
}
