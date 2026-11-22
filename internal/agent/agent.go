// Package agent provides a small concurrent perception/action runtime.
//
// The design is deliberately boring: eyes observe, hands act, a planner
// decides. Eyes and hands are independent registries, so adding a new
// screenshot source, MAA callback reader, Copilot runner or adb tap does not
// change the loop. All eyes run concurrently; hands are serialized per device.
package agent

import (
	"context"
	"errors"
	"time"
)

var (
	// ErrNeedsApproval is returned for high-risk actions without a human gate.
	ErrNeedsApproval = errors.New("action requires human approval")
	// ErrNoHand is returned when a command names an unregistered hand.
	ErrNoHand = errors.New("hand not registered")
	// ErrLeaseTimeout is returned when a device is busy for too long.
	ErrLeaseTimeout = errors.New("device lease timeout")
)

// Observation is one eye's latest result. Data is intentionally any: a
// screenshot is []byte, MAA state is a struct, a roster is a slice, etc.
type Observation struct {
	Source string
	At     time.Time
	Data   any
	Err    error
}

// Eye is one perception source. Implementations must respect ctx.
type Eye interface {
	Name() string
	Observe(ctx context.Context) (any, error)
}

// Risk describes how dangerous a hand is. High-risk hands require approval.
type Risk int

const (
	RiskLow Risk = iota
	RiskMedium
	RiskHigh
)

// Command is one action request. Device is the lease key (usually an adb
// serial or account id). Hand names a registered Hand.
type Command struct {
	Device string
	Hand   string
	Args   map[string]any
}

// Hand is one action capability.
type Hand interface {
	Name() string
	Risk() Risk
	Do(ctx context.Context, cmd Command) error
}

// Planner decides the next command(s) from the latest world state. Returning
// done=true stops the loop.
type Planner interface {
	Next(ctx context.Context, state map[string]Observation) (cmds []Command, done bool, err error)
}
