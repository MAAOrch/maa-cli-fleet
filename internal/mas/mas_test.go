package mas

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestClassifyOldAndNewSchema(t *testing.T) {
	// New schema: success / time / message
	s1 := Classify("acc1", []HistoryEntry{
		{Time: "2026-11-22T01:00:00+04:00", Status: "error", Message: "MAA 未能正确登录 PRTS"},
		{Time: "2026-11-22T02:00:00+04:00", Status: "success", Message: "完成"},
	})
	if s1.Bucket != "ok" || !s1.DoneAny || s1.Attempts != 2 {
		t.Fatalf("s1 = %+v, want ok", s1)
	}

	// Old schema: DONE / date / result followed by ERROR -> done_then_error
	s2 := Classify("acc2", []HistoryEntry{
		{Date: "2026-11-22 05:00:00", Status: "DONE", Result: "ok"},
		{Date: "2026-11-22 06:00:00", Status: "ERROR", Result: "timeout"},
	})
	if s2.Bucket != "done_then_error" || s2.LastResult != "timeout" {
		t.Fatalf("s2 = %+v, want done_then_error", s2)
	}
}

func TestFetchBriefHTTP(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":{"2026-11-22":{
			"okUser":{"index":[{"time":"2026-11-22T01:00:00","status":"success","message":"ok"}]},
			"badUser":{"index":[{"time":"2026-11-22T02:00:00","status":"error","message":"login failed"}]}
		}}}`))
	}))
	defer srv.Close()

	b, err := FetchBrief(context.Background(), srv.URL, "2026-11-22", 5*time.Second)
	if err != nil {
		t.Fatalf("FetchBrief: %v", err)
	}
	if b.Counts["ok"] != 1 || b.Counts["failed"] != 1 || len(b.ErrorClusters) != 1 {
		t.Fatalf("unexpected brief: %+v", b)
	}
}
