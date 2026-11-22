package agent

import (
	"context"
	"errors"
	"log"
	"time"
)

// Loop is the minimal play loop: observe all eyes, ask the planner for the
// next commands, run them (parallel across devices), then wait Interval.
type Loop struct {
	Eyes     *PerceptionBus
	Hands    *ActionBus
	Planner  Planner
	Interval time.Duration
}

// Run blocks until the planner returns done, an error occurs, or ctx is
// canceled.
func (l *Loop) Run(ctx context.Context) error {
	if l.Eyes == nil || l.Hands == nil || l.Planner == nil {
		return errors.New("agent loop: eyes, hands and planner are required")
	}
	interval := l.Interval
	if interval <= 0 {
		interval = time.Second
	}

	for {
		state := l.Eyes.ObserveAll(ctx)

		cmds, done, err := l.Planner.Next(ctx, state)
		if err != nil {
			return err
		}
		if done {
			return nil
		}

		if len(cmds) > 0 {
			for i, err := range l.Hands.DoAll(ctx, cmds) {
				if err != nil {
					log.Printf("agent: command %d (%s/%s) failed: %v", i, cmds[i].Device, cmds[i].Hand, err)
				}
			}
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(interval):
		}
	}
}
