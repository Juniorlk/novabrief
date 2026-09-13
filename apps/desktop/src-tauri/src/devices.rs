//! The [`Source`](crate::engine::Source) that is real hardware.
//!
//! A thin wrapper and nothing else. Every decision about what to do with the
//! frames - keep them, drop them, count them, stop - is in `engine.rs`, which
//! is why that can be tested without a microphone. What lives here is the part
//! that genuinely cannot: opening WASAPI endpoints and closing them again.

use audio_engine::pipeline::{Endpoints, Recorder as Pipeline};

use crate::engine::{Report, Source};

/// The microphone and the system loopback, as the engine sees them.
#[derive(Debug)]
pub struct Devices {
    /// Taken by [`Source::finish`], which consumes the pipeline.
    pipeline: Option<Pipeline>,
}

impl Devices {
    /// Open both endpoints and start capturing.
    ///
    /// No duration limit is given to the pipeline: the four-hour ceiling of
    /// EF-34 belongs to the session, which counts audio actually kept, and a
    /// wall clock down here would cut a meeting short by however long it was
    /// paused.
    ///
    /// # Errors
    ///
    /// A message naming the endpoint that could not be opened.
    pub fn open(endpoints: &Endpoints) -> Result<Self, String> {
        let pipeline = Pipeline::start(endpoints, None).map_err(|error| error.to_string())?;
        Ok(Self {
            pipeline: Some(pipeline),
        })
    }
}

impl Source for Devices {
    fn poll(&mut self, out: &mut Vec<f32>) -> usize {
        self.pipeline
            .as_mut()
            .map_or(0, |pipeline| pipeline.poll(out))
    }

    fn levels(&mut self) -> (f32, f32) {
        self.pipeline.as_mut().map_or((0.0, 0.0), Pipeline::levels)
    }

    fn running(&self) -> bool {
        self.pipeline.as_ref().is_some_and(Pipeline::running)
    }

    fn newly_stalled(&mut self) -> Vec<String> {
        self.pipeline.as_mut().map_or_else(Vec::new, |pipeline| {
            pipeline
                .newly_stalled()
                .into_iter()
                .map(|endpoint| endpoint.to_string())
                .collect()
        })
    }

    fn finish(&mut self, out: &mut Vec<f32>) -> Result<Report, String> {
        let Some(pipeline) = self.pipeline.take() else {
            return Err("the capture has already been finished".to_owned());
        };

        // Read before `finish` consumes the pipeline. The skew is what was
        // corrected at the start, and it belongs in the manifest so that a
        // transcript's timestamps can be traced back to it.
        let skew_frames = pipeline.applied_skew();
        let outcomes = pipeline.finish(out).map_err(|error| error.to_string())?;

        let device_of = |wanted: audio_engine::Endpoint| {
            outcomes
                .iter()
                .find(|outcome| outcome.endpoint == wanted)
                .map_or_else(|| "not recorded".to_owned(), |o| o.device_name.clone())
        };

        Ok(Report {
            input_device: device_of(audio_engine::Endpoint::Microphone),
            output_device: device_of(audio_engine::Endpoint::SystemLoopback),
            // `None` means one endpoint never delivered a real packet, so
            // nothing was aligned against anything; zero is the honest figure
            // for a correction that was never applied.
            start_skew_ms: skew_frames.unwrap_or(0) * 1000
                / i64::from(audio_engine::resample::TARGET_SAMPLE_RATE),
        })
    }
}
