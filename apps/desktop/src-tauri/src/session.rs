//! What a recording does with each segment, and when it has to stop.
//!
//! The rules only, with no audio device anywhere near them. Everything EF-33
//! and EF-34 are measured on - what a pause costs, what gets billed, when the
//! warning appears, when the meeting is cut off - is decided here and can
//! therefore be tested without a microphone.
//!
//! The plumbing that feeds it lives elsewhere; if these rules were tangled into
//! the capture threads, the only way to check "paused time is not billed" would
//! be to record a real meeting and read the invoice.

use std::time::Duration;

use crate::state::AppState;

/// The four-hour ceiling of EF-34.
///
/// A technical limit rather than a plan value: past four hours the analysis is
/// split anyway (section 16) and no supplier quotes a price for a single
/// recording that long. **The Free plan's 60 minutes is not here** - that is a
/// quota, it lives in the `plans` table, and ADR-09 forbids writing it down in
/// the code. The session is told its limit; it does not know where it came
/// from.
pub const CEILING: Duration = Duration::from_secs(4 * 60 * 60);

/// How long before the limit the user is warned (EF-34).
pub const WARN_BEFORE: Duration = Duration::from_secs(5 * 60);

/// What should happen to a segment that has just finished encoding.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Disposition {
    /// Store it in the vault.
    Keep,
    /// Paused: throw it away, and do not count it against anything.
    ///
    /// Discarded rather than stored-and-ignored. The pause is the only control
    /// a person has over what NovaBrief captures - somebody steps out for a
    /// private call, or two people have an aside - and "we kept it but will not
    /// upload it" is not the promise the button makes. It must not reach the
    /// disk at all.
    Discard,
}

/// What counting a segment did to the budget.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BudgetEvent {
    /// Nothing to say.
    Continue,
    /// Five minutes left. Raised once, not on every segment afterwards.
    Warn,
    /// The limit is reached; the recording has to be finalised now.
    Stop,
}

/// One recording, from the rules' point of view.
#[derive(Debug)]
pub struct Session {
    state: AppState,
    limit: Duration,
    /// Audio actually kept, in milliseconds.
    ///
    /// The sum of the segments that were stored - never a wall clock. That is
    /// the whole of EF-33's "paused time is not billed": a pause simply does
    /// not add to this, so there is no separate subtraction to get wrong.
    recorded_ms: u64,
    /// Audio thrown away because the recording was paused.
    discarded_ms: u64,
    warned: bool,
}

impl Session {
    /// Begin a recording with `limit` as its maximum length.
    ///
    /// The limit is clamped to [`CEILING`]: a plan may buy less than four
    /// hours, never more.
    #[must_use]
    pub fn start(limit: Duration) -> Self {
        Self {
            state: AppState::Recording,
            limit: limit.min(CEILING),
            recorded_ms: 0,
            discarded_ms: 0,
            warned: false,
        }
    }

    /// Where the recording is.
    #[must_use]
    pub const fn state(&self) -> AppState {
        self.state
    }

    /// Audio kept so far. This is what gets billed.
    #[must_use]
    pub const fn recorded(&self) -> Duration {
        Duration::from_millis(self.recorded_ms)
    }

    /// Audio dropped because the recording was paused.
    #[must_use]
    pub const fn discarded(&self) -> Duration {
        Duration::from_millis(self.discarded_ms)
    }

    /// Suspend capture.
    ///
    /// # Errors
    ///
    /// When the recording is not running.
    pub fn pause(&mut self) -> Result<(), String> {
        self.transition(AppState::Paused)
    }

    /// Carry on.
    ///
    /// # Errors
    ///
    /// When the recording is not paused.
    pub fn resume(&mut self) -> Result<(), String> {
        if self.state != AppState::Paused {
            return Err(format!("cannot resume from {:?}", self.state));
        }
        self.transition(AppState::Recording)
    }

    /// Finish, and hand the recording to the uploader.
    ///
    /// # Errors
    ///
    /// When the recording is neither running nor paused.
    pub fn finish(&mut self) -> Result<(), String> {
        self.transition(AppState::Uploading)
    }

    fn transition(&mut self, target: AppState) -> Result<(), String> {
        if !self.state.may_move_to(target) {
            return Err(format!("cannot move from {:?} to {target:?}", self.state));
        }
        self.state = target;
        Ok(())
    }

    /// What to do with a segment that has just been encoded.
    #[must_use]
    pub const fn disposition(&self) -> Disposition {
        match self.state {
            AppState::Recording => Disposition::Keep,
            _ => Disposition::Discard,
        }
    }

    /// Account for a segment, and say whether the meeting must now end.
    ///
    /// Call it for **every** finished segment, kept or not: a discarded one
    /// still has to be counted somewhere, or the pause becomes invisible and
    /// nobody can answer "why is this recording shorter than the meeting?".
    pub fn counted(&mut self, duration_ms: u64) -> BudgetEvent {
        if self.disposition() == Disposition::Discard {
            self.discarded_ms = self.discarded_ms.saturating_add(duration_ms);
            return BudgetEvent::Continue;
        }

        self.recorded_ms = self.recorded_ms.saturating_add(duration_ms);
        let recorded = self.recorded();

        if recorded >= self.limit {
            return BudgetEvent::Stop;
        }
        // `warned` latches, so the warning is an event and not a condition that
        // keeps firing for the last five minutes of every long meeting.
        if !self.warned && recorded + WARN_BEFORE >= self.limit {
            self.warned = true;
            return BudgetEvent::Warn;
        }
        BudgetEvent::Continue
    }
}

#[cfg(test)]
mod tests {
    use super::{BudgetEvent, Disposition, Session, CEILING, WARN_BEFORE};
    use crate::state::AppState;
    use std::time::Duration;

    const SEGMENT_MS: u64 = 5_000;

    /// Feed `count` five-second segments and return the events they raised.
    fn feed(session: &mut Session, count: usize) -> Vec<BudgetEvent> {
        (0..count).map(|_| session.counted(SEGMENT_MS)).collect()
    }

    #[test]
    fn a_new_session_is_recording_and_empty() {
        let session = Session::start(Duration::from_secs(3600));
        assert_eq!(session.state(), AppState::Recording);
        assert_eq!(session.recorded(), Duration::ZERO);
        assert_eq!(session.disposition(), Disposition::Keep);
    }

    /// EF-33, the criterion as the brief states it: the quota consumed equals
    /// the effective recording time.
    #[test]
    fn paused_time_is_not_billed() {
        let mut session = Session::start(Duration::from_secs(3600));

        feed(&mut session, 4); // 20 s recorded
        session.pause().expect("pausing while recording");
        feed(&mut session, 120); // ten minutes of pause
        session.resume().expect("resuming while paused");
        feed(&mut session, 4); // 20 s more

        assert_eq!(
            session.recorded(),
            Duration::from_secs(40),
            "the pause was billed"
        );
        assert_eq!(session.discarded(), Duration::from_secs(600));
    }

    /// The pause is the only control a person has over what is captured, so
    /// audio recorded during one must not reach the disk at all.
    #[test]
    fn a_paused_recording_keeps_nothing() {
        let mut session = Session::start(Duration::from_secs(3600));
        session.pause().expect("pauses");
        assert_eq!(session.disposition(), Disposition::Discard);

        session.resume().expect("resumes");
        assert_eq!(session.disposition(), Disposition::Keep);
    }

    /// A pause does not end the recording: the same vault recording stays open,
    /// which is what "without creating multiple files" means.
    #[test]
    fn pausing_and_resuming_stays_in_one_recording() {
        let mut session = Session::start(Duration::from_secs(3600));
        for _ in 0..5 {
            session.pause().expect("pauses");
            session.resume().expect("resumes");
        }
        assert_eq!(session.state(), AppState::Recording);
    }

    #[test]
    fn pausing_twice_is_refused_rather_than_ignored() {
        let mut session = Session::start(Duration::from_secs(3600));
        session.pause().expect("pauses");
        assert!(session.pause().is_err());
        assert!(session.resume().is_ok());
        assert!(session.resume().is_err(), "resuming while recording");
    }

    /// EF-34: five minutes before the limit, once.
    #[test]
    fn the_warning_comes_five_minutes_before_the_limit_and_only_once() {
        let limit = Duration::from_secs(600); // ten minutes
        let mut session = Session::start(limit);

        let events = feed(&mut session, 119); // up to 9 min 55 s
        let warnings = events
            .iter()
            .filter(|event| **event == BudgetEvent::Warn)
            .count();

        assert_eq!(warnings, 1, "expected exactly one warning, got {warnings}");

        // And it landed where EF-34 asks: on the first segment that took the
        // recording within five minutes of the end.
        let first = events
            .iter()
            .position(|event| *event == BudgetEvent::Warn)
            .expect("a warning was raised");
        let recorded_at_warning = Duration::from_millis((first as u64 + 1) * SEGMENT_MS);
        assert!(recorded_at_warning + WARN_BEFORE >= limit);
        assert!(recorded_at_warning < limit);
    }

    #[test]
    fn the_limit_stops_the_recording() {
        let limit = Duration::from_secs(60);
        let mut session = Session::start(limit);

        let events = feed(&mut session, 12);
        assert_eq!(events.last(), Some(&BudgetEvent::Stop));
        assert_eq!(session.recorded(), limit);
    }

    /// A pause must not shorten the meeting a person is allowed to record: the
    /// budget counts kept audio, so ten minutes of pause buy ten more minutes
    /// of recording rather than eating into the limit.
    #[test]
    fn a_pause_does_not_spend_the_budget() {
        let limit = Duration::from_secs(60);
        let mut session = Session::start(limit);

        feed(&mut session, 6); // 30 s
        session.pause().expect("pauses");
        feed(&mut session, 600); // fifty minutes of pause
        session.resume().expect("resumes");

        let events = feed(&mut session, 5); // 25 s more, still under the limit
        assert!(!events.contains(&BudgetEvent::Stop));
        assert_eq!(session.recorded(), Duration::from_secs(55));
    }

    /// ADR-09: the limit is told to the session, never read from a constant
    /// here. The only thing the code knows is the technical ceiling.
    #[test]
    fn a_limit_beyond_the_ceiling_is_clamped() {
        let mut session = Session::start(Duration::from_secs(99 * 3600));
        // Four hours of five-second segments, less one.
        let segments = (CEILING.as_millis() / u128::from(SEGMENT_MS)) as usize;
        let events = feed(&mut session, segments);

        assert_eq!(events.last(), Some(&BudgetEvent::Stop));
        assert_eq!(session.recorded(), CEILING);
    }

    #[test]
    fn finishing_hands_the_recording_to_the_uploader() {
        let mut session = Session::start(Duration::from_secs(3600));
        session.finish().expect("finishes");
        assert_eq!(session.state(), AppState::Uploading);
        // And a finished recording keeps nothing more.
        assert_eq!(session.disposition(), Disposition::Discard);
    }

    /// Audio that was captured is always owed an upload attempt, so there is no
    /// path from a paused recording straight back to idle.
    #[test]
    fn a_paused_recording_can_still_be_finished() {
        let mut session = Session::start(Duration::from_secs(3600));
        feed(&mut session, 2);
        session.pause().expect("pauses");

        session.finish().expect("a paused meeting can be ended");
        assert_eq!(session.recorded(), Duration::from_secs(10));
    }
}
