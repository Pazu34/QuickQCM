# QCM Scanner — contexte du projet

Système de QCM papier auto-corrigé pour un enseignant de physique-chimie
(collège/SEGPA). Développé avec une autre instance de Claude (conversation
dans claude.ai), qui a produit et testé les 4 modules Python ci-dessous.
Objectif de cette session Claude Code : construire une **application locale
avec interface graphique** (fenêtre ou page web locale) qui les utilise,
puis l'**empaqueter en exécutable Windows autonome** (l'utilisateur n'a pas
Python installé et ne veut pas utiliser de terminal).

## Modules déjà écrits et testés (ne pas réécrire from scratch)

- **`generate_sheet_modular.py`** — Génère les feuilles-réponses PDF.
  Modulaire : titre, sous-titre, nombre de questions (1-64), nombre de
  colonnes de réponses (3-6), champ "Classe" oui/non, échelle de la
  feuille (homothétie, plafonnée à A4), nombre de feuilles par page
  imprimée, recto-verso automatique si les questions ne tiennent pas sur
  une face. Chaque feuille porte : 4 repères de calage (coins), un motif
  numéroté (1-255, identifie l'élève), et un code-barres (barres
  verticales, bas de la feuille) qui encode automatiquement TOUTE la
  configuration (nb questions, nb réponses, échelle, classe, face
  recto/verso) — donc le script de lecture n'a jamais besoin qu'on la lui
  précise à la main.
  - Classe principale : `SheetConfig` (dataclass) — voir ses champs pour
    toutes les options.
  - Fonctions principales : `build(path, cfg, numbers=[...])` (aperçu
    rapide, numéros au choix) et `build_batch(path, cfg, start_number=1,
    n_sheets=N)` (génère N feuilles numérotées consécutivement, gère le
    recto-verso automatiquement).

- **`sheet_layout_v2.py`** — Géométrie bas niveau des 4 repères de coin
  (utilisée par `detect_modular.py`). Ne pas modifier sans comprendre
  `generate_sheet_modular.py` en détail (beaucoup d'itérations de debug
  visuel ont abouti à ces constantes).

- **`detect_modular.py`** — Lit une photo/scan d'une feuille remplie.
  Fonction principale : `analyse(image_path, cfg=None)`. Si `cfg=None`
  (cas normal), la configuration est lue automatiquement depuis le
  code-barres. Retourne un dict avec `sheet_number`, `side` (0=recto,
  1=verso), `questions` (réponses détectées + cas "à vérifier" quand
  l'algorithme n'est pas sûr), `config_parity_ok`, etc. Le seuil de
  détection est adaptatif (recalculé à partir des propres valeurs de
  chaque feuille) — pas de constante à régler à la main.

- **`roster_match.py`** — Associe chaque feuille à un élève à partir d'un
  CSV (`numero,nom,classe`). Fonction principale :
  `process_batch(image_paths, roster, output_dir)` — regroupe
  automatiquement recto+verso d'une même copie, signale les numéros
  inconnus (avec image recadrée de la zone Nom/Classe pour identifier
  l'élève et corriger le tableau), les feuilles douteuses, et les faces
  manquantes en recto-verso.

## Ce qu'il reste à faire (votre mission)

1. **Interface de génération** : formulaire pour les champs de
   `SheetConfig` (titre, sous-titre, nb questions, nb réponses, classe
   oui/non, nb feuilles à générer...) + upload du CSV classe (ou saisie
   manuelle) → génère le PDF à imprimer (via `build_batch`) + le CSV de
   correspondance numéro/élève si généré automatiquement.
2. **Interface de scan/correction** : upload d'une ou plusieurs photos +
   du CSV classe → lance `roster_match.process_batch` → affiche les
   résultats (élève, réponses, signalements) de façon lisible, avec les
   images recadrées pour les cas à vérifier manuellement.
3. **Empaquetage** : un exécutable Windows autonome (l'utilisatrice n'a
   pas Python et préfère éviter le terminal). PyInstaller est probablement
   le bon outil ; à vous de voir l'approche la plus fiable pour embarquer
   opencv/numpy/reportlab proprement.

## Contraintes importantes

- L'utilisatrice (Jules, enseignante) n'est pas développeuse : l'app doit
  être utilisable sans terminal après l'installation initiale.
- Tout le texte de l'interface doit être en français.
- Les 4 modules existants sont fonctionnels et testés de bout en bout
  (génération → simulation de remplissage → photo simulée avec
  rotation/perspective → lecture → association élève) sur de nombreux cas
  (12 à 64 questions, 3 à 6 réponses, avec/sans champ classe, recto simple
  et recto-verso). Réutilisez-les tels quels comme bibliothèque plutôt que
  de réimplémenter leur logique dans l'interface.
