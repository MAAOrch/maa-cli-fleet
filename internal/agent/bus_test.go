package agent

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type fakeEye struct {
	name  string
	delay time.Duration
	data  any
	err   error
}

func (e fakeEye) Name() string { return e.name }

func (e fakeEye) Observe(ctx context.Context) (any, error) {
	select {
	case <-time.After(e.delay):
		return e.data, e.err
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func TestPerceptionBusRunsConcurrently(t *testing.T) {
	bus := NewPerceptionBus(2*time.Second,
		fakeEye{name: "a", delay: 100 * time.Millisecond, data: 1},
		fakeEye{name: "b", delay: 100 * time.Millisecond, data: 2},
		fakeEye{name: "c", delay: 100 * time.Millisecond, data: 3},
	)

	start := time.Now()
	got := bus.ObserveAll(context.Background())
	elapsed := time.Since(start)

	if len(got) != 3 {
		t.Fatalf("got %d observations, want 3", len(got))
	}
	if elapsed > 300*time.Millisecond {
		t.Fatalf("eyes took %s, want concurrent (<=300ms)", elapsed)
	}
	if bus.Latest()["b"].Data != 2 {
		t.Fatalf("latest snapshot lost b: %#v", bus.Latest())
	}
}

func TestPerceptionBusKeepsFailures(t *testing.T) {
	wantErr := errors.New("boom")
	bus := NewPerceptionBus(time.Second,
		fakeEye{name: "ok", data: "yes"},
		fakeEye{name: "bad", err: wantErr},
	)
	got := bus.ObserveAll(context.Background())
	if got["ok"].Err != nil || got["ok"].Data != "yes" {
		t.Fatalf("ok eye = %#v", got["ok"])
	}
	if !errors.Is(got["bad"].Err, wantErr) {
		t.Fatalf("bad eye = %#v", got["bad"])
	}
}

type fakeHand struct {
	name   string
	risk   Risk
	delay  time.Duration
	active atomic.Int32
	max    atomic.Int32
}

func (h *fakeHand) Name() string { return h.name }
func (h *fakeHand) Risk() Risk   { return h.risk }

func (h *fakeHand) Do(ctx context.Context, _ Command) error {
	n := h.active.Add(1)
	for {
		m := h.max.Load()
		if n <= m || h.max.CompareAndSwap(m, n) {
			break
		}
	}
	defer h.active.Add(-1)

	select {
	case <-time.After(h.delay):
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func TestActionBusSerializesSameDevice(t *testing.T) {
	h := &fakeHand{name: "tap", risk: RiskLow, delay: 60 * time.Millisecond}
	bus := NewActionBus(time.Second, nil)
	bus.Register(h)

	var wg sync.WaitGroup
	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := bus.Do(context.Background(), Command{Device: "emu0", Hand: "tap"}); err != nil {
				t.Errorf("Do: %v", err)
			}
		}()
	}
	wg.Wait()

	if h.max.Load() != 1 {
		t.Fatalf("same-device max concurrency = %d, want 1", h.max.Load())
	}
}

func TestActionBusRunsDifferentDevicesConcurrently(t *testing.T) {
	h := &fakeHand{name: "tap", risk: RiskLow, delay: 120 * time.Millisecond}
	bus := NewActionBus(time.Second, nil)
	bus.Register(h)

	var wg sync.WaitGroup
	for i := 0; i < 2; i++ {
		i := i
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := bus.Do(context.Background(), Command{Device: fmt.Sprintf("emu%d", i), Hand: "tap"}); err != nil {
				t.Errorf("Do: %v", err)
			}
		}()
	}
	wg.Wait()

	if h.max.Load() < 2 {
		t.Fatalf("different-device max concurrency = %d, want >=2", h.max.Load())
	}
}

func TestHighRiskNeedsApproval(t *testing.T) {
	h := &fakeHand{name: "gacha", risk: RiskHigh}
	bus := NewActionBus(time.Second, nil)
	bus.Register(h)

	err := bus.Do(context.Background(), Command{Device: "emu0", Hand: "gacha"})
	if !errors.Is(err, ErrNeedsApproval) {
		t.Fatalf("err = %v, want ErrNeedsApproval", err)
	}

	approved := NewActionBus(time.Second, func(Command) bool { return true })
	approved.Register(h)
	if err := approved.Do(context.Background(), Command{Device: "emu0", Hand: "gacha"}); err != nil {
		t.Fatalf("approved Do: %v", err)
	}
}

type fakePlanner struct {
	calls int
}

func (p *fakePlanner) Next(context.Context, map[string]Observation) ([]Command, bool, error) {
	p.calls++
	return nil, p.calls >= 1, nil
}

func TestLoopStopsWhenPlannerDone(t *testing.T) {
	p := &fakePlanner{}
	l := &Loop{
		Eyes:     NewPerceptionBus(time.Second, fakeEye{name: "x"}),
		Hands:    NewActionBus(time.Second, nil),
		Planner:  p,
		Interval: time.Millisecond,
	}
	if err := l.Run(context.Background()); err != nil {
		t.Fatalf("Run: %v", err)
	}
	if p.calls != 1 {
		t.Fatalf("planner calls = %d, want 1", p.calls)
	}
}
