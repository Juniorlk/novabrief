//! Measuring how fast an audio device's clock actually runs.
//!
//! A capture device counts frames on its own crystal; Windows counts time on
//! the system performance counter. The two never agree exactly, and the
//! difference is what makes two endpoints drift apart over a long meeting.
//!
//! The size of that difference is a property of the hardware, not of NovaBrief:
//! a laptop whose microphone and speakers hang off the same codec may share a
//! clock and barely drift at all, while a built-in microphone paired with a USB
//! or Bluetooth headset is two independent crystals and will drift steadily. So
//! nothing here is calibrated against a machine we happen to own. Each stream
//! measures its own rate at run time, and the caller compares.
//!
//! The estimate is a least-squares fit of frames against elapsed time. Fitting
//! rather than dividing the endpoints matters: buffer jitter moves individual
//! packets by milliseconds, which swamps a drift of a few parts per million if
//! you only look at the first and last sample.

/// One observation: how many frames the device had produced at a given instant.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Observation {
    elapsed_100ns: u64,
    frames: u64,
}

/// Estimates a device's true sample rate, and hence its drift.
#[derive(Debug, Clone)]
pub struct DriftEstimator {
    nominal_rate: u32,
    origin_qpc_100ns: Option<u64>,
    observations: Vec<Observation>,
    max_observations: usize,
}

/// The outcome of a drift measurement.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct DriftEstimate {
    /// Sample rate the device claims to run at.
    pub nominal_rate: f64,
    /// Sample rate it actually ran at, measured against the system clock.
    pub measured_rate: f64,
    /// Relative error in parts per million. Positive means the device runs
    /// fast, producing more frames than nominal for a given wall-clock second.
    pub ppm: f64,
    /// Span of the observations the estimate is based on.
    pub observed_seconds: f64,
    /// How many samples the fit used.
    pub samples: usize,
}

impl DriftEstimate {
    /// Offset this drift alone would accumulate over `seconds`, in milliseconds.
    #[must_use]
    pub fn projected_offset_ms(&self, seconds: f64) -> f64 {
        self.ppm * seconds / 1000.0
    }
}

impl DriftEstimator {
    /// Create an estimator for a device that claims `nominal_rate` Hz.
    #[must_use]
    pub fn new(nominal_rate: u32) -> Self {
        Self {
            nominal_rate,
            origin_qpc_100ns: None,
            observations: Vec::new(),
            // An hour of 20 ms packets is 180 000 observations, which is more
            // resolution than a straight line needs and a pointless amount of
            // memory on a client machine. Beyond the cap, observations are
            // thinned by half, which keeps the span while halving the density.
            max_observations: 4096,
        }
    }

    /// Record that the device had produced `frames` frames at `qpc_100ns`.
    ///
    /// Only packets the device actually delivered should be recorded: silence
    /// we synthesised is timed by our own clock, so feeding it back in would
    /// measure the system clock against itself and always report zero drift.
    pub fn observe(&mut self, qpc_100ns: u64, frames: u64) {
        let origin = *self.origin_qpc_100ns.get_or_insert(qpc_100ns);
        let Some(elapsed) = qpc_100ns.checked_sub(origin) else {
            // A timestamp before the origin means the counter moved backwards;
            // there is nothing sensible to fit, so the sample is dropped.
            return;
        };

        self.observations.push(Observation {
            elapsed_100ns: elapsed,
            frames,
        });

        if self.observations.len() > self.max_observations {
            let mut kept = Vec::with_capacity(self.max_observations / 2 + 1);
            for (index, observation) in self.observations.iter().enumerate() {
                if index % 2 == 0 {
                    kept.push(*observation);
                }
            }
            self.observations = kept;
        }
    }

    /// Number of observations currently held.
    #[must_use]
    pub fn len(&self) -> usize {
        self.observations.len()
    }

    /// Whether no observation has been recorded yet.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.observations.is_empty()
    }

    /// Fit the observations and report the measured rate.
    ///
    /// Returns `None` until there is enough of a time span to say anything: a
    /// drift of a few ppm is invisible over a second of buffer jitter, and
    /// reporting a number from too little data would be worse than reporting
    /// nothing.
    #[must_use]
    pub fn estimate(&self) -> Option<DriftEstimate> {
        const MIN_SAMPLES: usize = 16;
        const MIN_SPAN_SECONDS: f64 = 5.0;

        if self.observations.len() < MIN_SAMPLES {
            return None;
        }
        let span_seconds = self
            .observations
            .last()
            .map_or(0.0, |o| o.elapsed_100ns as f64 / 10_000_000.0);
        if span_seconds < MIN_SPAN_SECONDS {
            return None;
        }

        // Least squares fit of frames = slope * seconds + intercept.
        let n = self.observations.len() as f64;
        let mut sum_t = 0.0;
        let mut sum_f = 0.0;
        for observation in &self.observations {
            sum_t += observation.elapsed_100ns as f64 / 10_000_000.0;
            sum_f += observation.frames as f64;
        }
        let mean_t = sum_t / n;
        let mean_f = sum_f / n;

        let mut covariance = 0.0;
        let mut variance = 0.0;
        for observation in &self.observations {
            let dt = observation.elapsed_100ns as f64 / 10_000_000.0 - mean_t;
            covariance += dt * (observation.frames as f64 - mean_f);
            variance += dt * dt;
        }
        if variance <= f64::EPSILON {
            return None;
        }

        let measured_rate = covariance / variance;
        let nominal = f64::from(self.nominal_rate);
        if nominal <= 0.0 || !measured_rate.is_finite() || measured_rate <= 0.0 {
            return None;
        }

        Some(DriftEstimate {
            nominal_rate: nominal,
            measured_rate,
            ppm: (measured_rate - nominal) / nominal * 1_000_000.0,
            observed_seconds: span_seconds,
            samples: self.observations.len(),
        })
    }
}

/// Relative drift between two endpoints, which is what actually desynchronises
/// the two voices of a meeting.
///
/// Each device's own error against the system clock is harmless on its own: a
/// recording that runs uniformly 20 ppm fast is still perfectly listenable.
/// What breaks a meeting is the *difference* between the two.
#[must_use]
pub fn relative_ppm(left: &DriftEstimate, right: &DriftEstimate) -> f64 {
    left.ppm - right.ppm
}

/// Offset two endpoints drifting `ppm` apart accumulate over `seconds`, in
/// milliseconds.
#[must_use]
pub fn projected_offset_ms(ppm: f64, seconds: f64) -> f64 {
    ppm * seconds / 1000.0
}

#[cfg(test)]
mod tests {
    use super::{projected_offset_ms, relative_ppm, DriftEstimator};

    /// Feed an estimator a device running at exactly `real_rate` Hz.
    fn feed(estimator: &mut DriftEstimator, real_rate: f64, seconds: f64, packet_ms: f64) {
        let packets = (seconds * 1000.0 / packet_ms) as u64;
        for packet in 0..packets {
            let elapsed_seconds = packet as f64 * packet_ms / 1000.0;
            let qpc = 1_000_000_000 + (elapsed_seconds * 10_000_000.0) as u64;
            let frames = (elapsed_seconds * real_rate) as u64;
            estimator.observe(qpc, frames);
        }
    }

    #[test]
    fn a_perfect_clock_measures_as_zero_drift() {
        let mut estimator = DriftEstimator::new(48_000);
        feed(&mut estimator, 48_000.0, 60.0, 20.0);
        let estimate = estimator.estimate().expect("enough data");
        assert!(
            estimate.ppm.abs() < 1.0,
            "expected no drift, got {} ppm",
            estimate.ppm
        );
    }

    #[test]
    fn a_fast_clock_is_reported_as_positive_ppm() {
        // 48 kHz nominal, running 50 ppm fast.
        let real = 48_000.0 * (1.0 + 50.0 / 1_000_000.0);
        let mut estimator = DriftEstimator::new(48_000);
        feed(&mut estimator, real, 120.0, 20.0);
        let estimate = estimator.estimate().expect("enough data");
        assert!(
            (estimate.ppm - 50.0).abs() < 2.0,
            "expected about +50 ppm, got {}",
            estimate.ppm
        );
    }

    #[test]
    fn a_slow_clock_is_reported_as_negative_ppm() {
        let real = 44_100.0 * (1.0 - 30.0 / 1_000_000.0);
        let mut estimator = DriftEstimator::new(44_100);
        feed(&mut estimator, real, 120.0, 20.0);
        let estimate = estimator.estimate().expect("enough data");
        assert!(
            (estimate.ppm + 30.0).abs() < 2.0,
            "expected about -30 ppm, got {}",
            estimate.ppm
        );
    }

    #[test]
    fn no_estimate_before_there_is_enough_span_to_justify_one() {
        let mut estimator = DriftEstimator::new(48_000);
        feed(&mut estimator, 48_000.0, 1.0, 20.0);
        assert!(
            estimator.estimate().is_none(),
            "one second cannot resolve a few ppm"
        );
    }

    #[test]
    fn observations_are_thinned_rather_than_grown_without_bound() {
        let mut estimator = DriftEstimator::new(48_000);
        feed(&mut estimator, 48_000.0, 3600.0, 20.0);
        assert!(
            estimator.len() <= 4096,
            "memory must stay bounded over an hour, got {}",
            estimator.len()
        );
        // Thinning must not destroy the measurement.
        let estimate = estimator.estimate().expect("still measurable");
        assert!(estimate.observed_seconds > 3000.0);
        assert!(estimate.ppm.abs() < 1.0);
    }

    #[test]
    fn relative_drift_is_the_difference_between_the_two_devices() {
        let mut fast = DriftEstimator::new(48_000);
        feed(&mut fast, 48_000.0 * (1.0 + 40.0 / 1e6), 120.0, 20.0);
        let mut slow = DriftEstimator::new(48_000);
        feed(&mut slow, 48_000.0 * (1.0 - 10.0 / 1e6), 120.0, 20.0);

        let relative = relative_ppm(
            &fast.estimate().expect("fast"),
            &slow.estimate().expect("slow"),
        );
        assert!(
            (relative - 50.0).abs() < 3.0,
            "expected about 50 ppm apart, got {relative}"
        );
    }

    #[test]
    fn the_c2_budget_corresponds_to_eleven_ppm() {
        // C2 allows 40 ms after 60 minutes. That is the number the hardware has
        // to beat, or that the compensator has to make up for.
        assert!((projected_offset_ms(11.0, 3600.0) - 39.6).abs() < 0.1);
        assert!(projected_offset_ms(40.0, 3600.0) > 140.0, "40 ppm busts it");
    }

    #[test]
    fn a_timestamp_going_backwards_is_ignored_rather_than_wrapping() {
        let mut estimator = DriftEstimator::new(48_000);
        estimator.observe(1_000_000_000, 0);
        estimator.observe(999_000_000, 48_000); // earlier than the origin
        assert_eq!(estimator.len(), 1);
    }
}
