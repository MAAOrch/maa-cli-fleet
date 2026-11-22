package agent

import (
	"context"
	"fmt"
	"sync"
	"time"
)

// PerceptionBus fans out ObserveAll to every eye concurrently and keeps the
// latest snapshot. Slow eyes do not block fast eyes inside one snapshot: all
// are started together and each gets its own timeout.
type PerceptionBus struct {
	eyes    []Eye
	timeout time.Duration

	mu     sync.RWMutex
	latest map[string]Observation
}

// NewPerceptionBus creates a bus. timeout <= 0 falls back to 30s.
func NewPerceptionBus(timeout time.Duration, eyes ...Eye) *PerceptionBus {
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	return &PerceptionBus{
		eyes:    eyes,
		timeout: timeout,
		latest:  map[string]Observation{},
	}
}

// ObserveAll runs every eye in parallel and returns one snapshot. A failing
// eye is recorded in Observation.Err; it does not cancel the other eyes.
// Implementations must respect ctx or they can block the snapshot until their
// per-eye timeout fires.
func (b *PerceptionBus) ObserveAll(ctx context.Context) map[string]Observation {
	out := make(map[string]Observation, len(b.eyes))
	var mu sync.Mutex
	var wg sync.WaitGroup

	for _, eye := range b.eyes {
		eye := eye
		wg.Add(1)
		go func() {
			defer wg.Done()
			cctx, cancel := context.WithTimeout(ctx, b.timeout)
			defer cancel()

			data, err := eye.Observe(cctx)
			obs := Observation{
				Source: eye.Name(),
				At:     time.Now(),
				Data:   data,
				Err:    err,
			}
			mu.Lock()
			out[eye.Name()] = obs
			mu.Unlock()
		}()
	}
	wg.Wait()

	b.mu.Lock()
	b.latest = out
	b.mu.Unlock()
	return out
}

// Latest returns a copy of the most recent snapshot.
func (b *PerceptionBus) Latest() map[string]Observation {
	b.mu.RLock()
	defer b.mu.RUnlock()
	out := make(map[string]Observation, len(b.latest))
	for k, v := range b.latest {
		out[k] = v
	}
	return out
}

// ActionBus routes commands to hands. Commands on the same device are
// serialized by a lease; commands on different devices may run concurrently.
type ActionBus struct {
	hands        map[string]Hand
	leases       map[string]chan struct{}
	leaseTimeout time.Duration
	approve      func(Command) bool

	mu sync.Mutex
}

// NewActionBus creates a bus. approve is called for high-risk commands; nil
// means all high-risk commands are rejected.
func NewActionBus(leaseTimeout time.Duration, approve func(Command) bool) *ActionBus {
	if leaseTimeout <= 0 {
		leaseTimeout = 10 * time.Minute
	}
	return &ActionBus{
		hands:        map[string]Hand{},
		leases:       map[string]chan struct{}{},
		leaseTimeout: leaseTimeout,
		approve:      approve,
	}
}

// Register adds a hand. Registering the same name replaces it.
func (b *ActionBus) Register(h Hand) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.hands[h.Name()] = h
}

// Acquire takes the device lease. The returned release func is idempotent.
func (b *ActionBus) Acquire(ctx context.Context, device string) (func(), error) {
	key := deviceKey(device)

	b.mu.Lock()
	ch, ok := b.leases[key]
	if !ok {
		ch = make(chan struct{}, 1)
		b.leases[key] = ch
	}
	b.mu.Unlock()

	lctx, cancel := context.WithTimeout(ctx, b.leaseTimeout)
	defer cancel()

	select {
	case ch <- struct{}{}:
		var once sync.Once
		return func() { once.Do(func() { <-ch }) }, nil
	case <-lctx.Done():
		return nil, fmt.Errorf("%w: %s", ErrLeaseTimeout, key)
	}
}

// Do looks up the hand, applies the risk gate, takes the device lease, and
// executes the command.
func (b *ActionBus) Do(ctx context.Context, cmd Command) error {
	b.mu.Lock()
	h, ok := b.hands[cmd.Hand]
	b.mu.Unlock()
	if !ok {
		return fmt.Errorf("%w: %s", ErrNoHand, cmd.Hand)
	}

	if h.Risk() >= RiskHigh && (b.approve == nil || !b.approve(cmd)) {
		return ErrNeedsApproval
	}

	release, err := b.Acquire(ctx, cmd.Device)
	if err != nil {
		return err
	}
	defer release()
	return h.Do(ctx, cmd)
}

// DoAll runs commands concurrently. Same-device commands still serialize
// inside Do via the lease; different devices run in parallel. Errors are
// returned in the same order as cmds.
func (b *ActionBus) DoAll(ctx context.Context, cmds []Command) []error {
	errs := make([]error, len(cmds))
	var wg sync.WaitGroup
	for i, cmd := range cmds {
		i, cmd := i, cmd
		wg.Add(1)
		go func() {
			defer wg.Done()
			errs[i] = b.Do(ctx, cmd)
		}()
	}
	wg.Wait()
	return errs
}

func deviceKey(device string) string {
	if device == "" {
		return "default"
	}
	return device
}
