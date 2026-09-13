//! How long to wait before trying again.
//!
//! EF-18: exponential, from one second to five minutes, **with no limit on the
//! number of attempts**. The absence of a limit is the requirement, not an
//! oversight - a laptop that comes back online on Monday morning must still
//! send Friday's meeting, and a client that gave up after ten tries would have
//! thrown it away over the weekend.
//!
//! The ceiling matters as much. Doubling without one reaches hours, and a
//! recording whose next attempt is four hours away is a recording the user has
//! already decided is lost.

use std::time::Duration;

/// The first wait.
const FIRST: Duration = Duration::from_secs(1);

/// The longest wait, whatever happens.
const CEILING: Duration = Duration::from_secs(5 * 60);

/// A doubling wait between attempts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Backoff {
    next: Duration,
    attempts: u32,
}

impl Default for Backoff {
    fn default() -> Self {
        Self::new()
    }
}

impl Backoff {
    /// A backoff that has not waited yet.
    #[must_use]
    pub const fn new() -> Self {
        Self {
            next: FIRST,
            attempts: 0,
        }
    }

    /// How long to wait before the next attempt, and move on.
    pub fn wait(&mut self) -> Duration {
        let waiting = self.next;
        self.next = self.next.saturating_mul(2).min(CEILING);
        self.attempts += 1;
        waiting
    }

    /// How long the next wait would be, without taking it.
    #[must_use]
    pub const fn peek(&self) -> Duration {
        self.next
    }

    /// How many attempts have failed.
    ///
    /// For the user interface, never for a decision to stop: there is no
    /// number of failures at which a recorded meeting stops being worth
    /// sending.
    #[must_use]
    pub const fn attempts(&self) -> u32 {
        self.attempts
    }

    /// Start again, after an attempt that worked.
    pub fn reset(&mut self) {
        *self = Self::new();
    }
}

#[cfg(test)]
mod tests {
    use super::{Backoff, CEILING, FIRST};

    #[test]
    fn the_first_wait_is_a_second_and_it_doubles() {
        let mut backoff = Backoff::new();
        assert_eq!(backoff.wait(), FIRST);
        assert_eq!(backoff.wait(), FIRST * 2);
        assert_eq!(backoff.wait(), FIRST * 4);
    }

    /// Doubling without a ceiling reaches hours, and a meeting whose next
    /// attempt is four hours away is one the user has written off.
    #[test]
    fn the_wait_stops_growing_at_five_minutes() {
        let mut backoff = Backoff::new();
        for _ in 0..40 {
            assert!(backoff.wait() <= CEILING);
        }
        assert_eq!(backoff.peek(), CEILING);
    }

    /// EF-18 sets no limit on attempts, and that is the requirement: a laptop
    /// that comes back on Monday must still send Friday's meeting.
    #[test]
    fn it_never_runs_out_of_attempts() {
        let mut backoff = Backoff::new();
        for _ in 0..10_000 {
            backoff.wait();
        }
        assert_eq!(backoff.attempts(), 10_000);
        assert_eq!(backoff.peek(), CEILING);
    }

    #[test]
    fn a_success_starts_the_wait_over() {
        let mut backoff = Backoff::new();
        for _ in 0..5 {
            backoff.wait();
        }
        backoff.reset();
        assert_eq!(backoff.peek(), FIRST);
        assert_eq!(backoff.attempts(), 0);
    }
}
