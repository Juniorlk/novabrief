/** English strings. Must carry exactly the keys `fr` carries — see the test. */
export const en = {
  app: {
    name: "NovaBrief",
    tagline: "Meeting notes, without a bot in the call.",
  },
  status: {
    idle: "Ready to record",
    recording: "Recording",
    paused: "Paused",
    uploading: "Uploading",
    processing: "Processing",
    offline: "Offline — recording continues",
  },
  tray: {
    start: "Start recording",
    stop: "Stop recording",
    pause: "Pause",
    resume: "Resume",
    meetings: "My meetings",
    audioTest: "Audio test",
    settings: "Settings",
    quit: "Quit",
  },
  meters: {
    microphone: "Microphone",
    system: "System audio",
    systemSilent: "No system audio detected — remote voices will not be recorded.",
  },
  errors: {
    noMicrophone: "No microphone available.",
    noSystemAudio: "System audio could not be captured.",
    diskFull: "Not enough disk space to keep recording.",
  },
  language: {
    label: "Language",
    fr: "Français",
    en: "English",
  },
} as const;
