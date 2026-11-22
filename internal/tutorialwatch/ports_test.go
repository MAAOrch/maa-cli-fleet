package tutorialwatch

import "testing"

func TestParseAdbDevices(t *testing.T) {
	out := `List of devices attached
127.0.0.1:16416	device
127.0.0.1:16448	offline
emulator-5558	device
127.0.0.1:16512	device
`
	got := ParseAdbDevices(out)
	want := []string{"127.0.0.1:16416", "127.0.0.1:16512"}
	if len(got) != len(want) {
		t.Fatalf("得到 %v，期望 %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("得到 %v，期望 %v", got, want)
		}
	}
}
