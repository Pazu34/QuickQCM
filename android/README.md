# QCM Scanner Mobile (Android) -- première version

Application Android compagnon de QCM Scanner (l'application de bureau) :
**scanner et corriger des copies directement avec l'appareil photo du
téléphone**, en réutilisant exactement le même moteur de lecture que sur
PC (`detect_modular.py`, `roster_match.py`, `scoring.py` -- copiés ici
tels quels, non modifiés).

## Ce que fait cette première version

1. **Importer une classe** (fichier CSV) et/ou **un corrigé** (fichier
   JSON), tous deux exportés depuis l'application PC.
2. **Scanner une copie** : prendre une photo avec l'appareil photo du
   téléphone, lecture et notation automatiques, affichage immédiat du
   résultat (élève reconnu, réponses détectées, note si un corrigé est
   chargé).
3. **Terminer la séance** : les résultats sont enregistrés sur le
   téléphone dans la même structure de dossiers que l'application PC,
   prêts à être recopiés dedans.

**Ce que cette version NE fait PAS encore** (volontairement, pour une
première version plus simple et plus fiable) :
- Générer des feuilles-réponses (PDF) depuis le téléphone.
- Créer une classe ou un corrigé directement sur le téléphone (il faut
  les importer depuis des fichiers créés sur PC).
- Synchronisation automatique en temps réel avec le PC (voir plus bas).

## D'où viennent les fichiers à importer ?

Pas besoin d'« exporter » quoi que ce soit de spécial sur PC : les
fichiers à copier sur le téléphone sont ceux que l'application PC crée
déjà toute seule, dans son dossier **Données** (visible et modifiable
dans l'onglet Préférences de l'application PC) :

- **Classe** : `Données/Classes/<nom de la classe>/eleves.csv`
- **Corrigé** : `Données/Corrigés/<nom du corrigé>.json`

Copie le fichier voulu sur le téléphone (par mail à toi-même, via un
câble USB, un drive partagé...), puis utilise les boutons « Importer
une classe (CSV) » / « Importer un corrigé (JSON) » dans l'application
mobile pour aller le chercher.

## Et pour ramener les résultats sur le PC ?

Une fois une séance de scan terminée sur le téléphone, le dossier de
résultats produit (`resultats.csv` + `report.json`) a exactement le
même format que celui que l'application PC produit elle-même. Copie ce
dossier entier dans :

```
Données/Classes/<même nom de classe>/Corrections/
```

de l'application PC -- il apparaîtra automatiquement dans l'onglet
« Gestion des données » → « Corrections enregistrées », comme n'importe
quelle correction faite depuis le PC.

## Pourquoi pas une synchronisation automatique dès cette version ?

Android protège désormais fortement l'accès aux dossiers d'une
application par les autres applications (et inversement), donc pointer
le téléphone directement sur un dossier partagé en temps réel (par
exemple un dossier Google Drive synchronisé) demande un peu plus de
travail qu'un simple import/export de fichiers. C'est tout à fait
faisable dans une prochaine version, une fois que le scan lui-même
aura fait ses preuves sur de vraies copies.

## Compiler l'APK

Un workflow GitHub Actions (`.github/workflows/build-android-apk.yml`)
compile automatiquement l'application en fichier `.apk` installable,
avec [buildozer](https://buildozer.readthedocs.io/) (qui empaquette
Kivy + les mêmes bibliothèques Python que l'application PC -- OpenCV,
NumPy -- pour Android). Il se déclenche manuellement depuis l'onglet
« Actions » du dépôt GitHub.

Pour installer l'APK obtenu sur un téléphone Android : le transférer
sur le téléphone, puis l'ouvrir avec un gestionnaire de fichiers (il
faudra autoriser « Installer des applications inconnues » pour cette
source la première fois -- Android le demande automatiquement au bon
moment).

## Structure du code

Fichiers copiés tels quels depuis l'application PC (moteur déjà
testé, ne pas les modifier ici sans les remettre aussi à jour côté
PC) : `detect_modular.py`, `roster_match.py`, `scoring.py`,
`answer_key_store.py`, `class_archive.py`, `generate_sheet_modular.py`,
`sheet_layout_v2.py`.

Propres à la version mobile : `app_config.py` (où vivent les données
sur Android, à la place du dossier « à côté de l'exécutable » du PC),
et `main.py` (l'interface Kivy : écran d'accueil, écran de scan, écran
de résultats de séance).
