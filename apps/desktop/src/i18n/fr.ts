/** French strings. The reference locale: every key exists here first. */
export const fr = {
  app: {
    name: "NovaBrief",
    tagline: "Comptes rendus de réunion, sans bot dans l'appel.",
  },
  status: {
    idle: "Prêt à enregistrer",
    recording: "Enregistrement en cours",
    paused: "En pause",
    uploading: "Envoi en cours",
    processing: "Traitement en cours",
    offline: "Hors ligne — l'enregistrement continue",
  },
  tray: {
    start: "Démarrer un enregistrement",
    stop: "Terminer l'enregistrement",
    pause: "Mettre en pause",
    resume: "Reprendre",
    meetings: "Mes réunions",
    audioTest: "Test audio",
    settings: "Paramètres",
    quit: "Quitter",
  },
  meters: {
    microphone: "Micro",
    system: "Audio système",
    // Shown under a flat system meter. EF-13 asks that a user notice within
    // five seconds that system audio is not being captured, and the meter
    // alone only tells them something is wrong, not what to do about it.
    systemSilent: "Aucun son système détecté — les voix distantes ne seront pas enregistrées.",
  },
  errors: {
    noMicrophone: "Aucun microphone disponible.",
    noSystemAudio: "Impossible de capter l'audio système.",
    diskFull: "Espace disque insuffisant pour continuer l'enregistrement.",
  },
  language: {
    label: "Langue",
    fr: "Français",
    en: "English",
  },
} as const;
