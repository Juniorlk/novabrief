/** French strings. The reference locale: every key exists here first. */
export const fr = {
  app: {
    name: "NovaBrief",
    tagline: "Comptes rendus de réunion, sans bot dans l'appel.",
    starting: "Ouverture…",
    signOut: "Se déconnecter",
    hide: "Masquer",
  },
  signIn: {
    title: "Connexion",
    lead: "Reliez ce poste à votre organisation.",
    email: "Adresse email",
    password: "Mot de passe",
    submit: "Se connecter",
    working: "Connexion…",
    once: "Une seule fois : ce poste reste relié jusqu'à révocation.",
  },
  status: {
    idle: "Prêt à enregistrer",
    recording: "Enregistrement en cours",
    paused: "En pause",
    uploading: "Envoi en cours",
    processing: "Traitement en cours",
    offline: "Hors ligne — l'enregistrement continue",
  },
  recorder: {
    // EF-34. Said plainly, because the only useful action is to finish before
    // the software does it.
    nearLimit: "Moins de 5 minutes avant la limite d'enregistrement.",
    paused: "{time} écartées par les pauses — non facturées.",
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
  meetings: {
    title: "Réunions",
    onThisMachine: "Sur ce poste",
    empty: "Aucune réunion pour l'instant.",
    untitled: "Réunion sans titre",
    sending: "Envoi",
    stuck: "Bloqué — action requise",
  },
  meetingStatus: {
    CREATED: "Déclarée",
    UPLOADING: "Envoi",
    QUOTA_HOLD: "En attente de paiement",
    QUEUED: "En file",
    TRANSCRIBING: "Transcription",
    FALLBACK_STT: "Transcription (second fournisseur)",
    ANALYZING: "Analyse",
    COMPLETED: "Prête",
    PUBLISHED: "Publiée",
    AUDIO_PURGED: "Audio effacé",
    FAILED: "Échec",
    CANCELLED: "Annulée",
    DELETED: "Supprimée",
  },
  failures: {
    // Stable codes from the API, never a provider message: those quote the
    // payload they choked on, which is meeting content.
    TRANSCRIPTION_FAILED: "La transcription n'a pas abouti.",
    ANALYSIS_FAILED: "L'analyse n'a pas abouti.",
    AUDIO_UNREADABLE: "L'audio reçu n'a pas pu être lu.",
  },
  report: {
    back: "← Retour",
    summary: "Résumé",
    decisions: "Décisions",
    tasks: "Tâches",
    noDecisions: "Aucune décision relevée.",
    noTasks: "Aucune tâche relevée.",
    // Never a guessed name: une tâche sans responsable nommé est un fait de la
    // réunion, un responsable inventé est la panne qui décrédibilise tout.
    nobodyNamed: "Responsable non nommé",
    notReady: "Le compte rendu n'est pas encore prêt.",
    showTranscript: "Afficher la transcription",
    hideTranscript: "Masquer la transcription",
  },
  errors: {
    noMicrophone: "Aucun microphone disponible.",
    noSystemAudio: "Impossible de capter l'audio système.",
    diskFull: "Espace disque insuffisant pour continuer l'enregistrement.",
  },
  settings: {
    autostart: "Démarrer avec Windows",
  },
  language: {
    label: "Langue",
    fr: "Français",
    en: "English",
  },
} as const;
