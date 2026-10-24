# Prototype : interface réécrite en CustomTkinter

Ce fichier (et `qcm_app_ctk_prototype.py`) n'existent que sur la branche
`prototype-customtkinter-ui` — rien ici n'est fusionné dans `main`, c'est
une maquette pour évaluer le résultat avant de décider si ça vaut le
coup d'aller plus loin.

## Ce que c'est

Une réécriture de l'onglet **Scanner / Corriger** (le plus riche de
l'application) avec [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter)
à la place de ttk : cartes arrondies, boutons colorés, look plus proche
d'une application mobile/moderne que l'actuel thème sv_ttk (déjà sur
`main`, voir le commit "Modernise l'interface").

Toute la logique métier est **réutilisée telle quelle, sans
duplication** : lecture/écriture des classes et corrections
(`class_archive.py`), correction automatique (`roster_match.py`,
`detect_modular.py`), notation (`scoring.py`), réglages
(`app_config.py`), traductions (`translations.py`), et même plusieurs
petites fonctions déjà écrites pour `qcm_app.ScanTab` (import CSV,
extraction PDF, rotation, suggestion de nom de correction...) sont
importées directement depuis `qcm_app.py`, pas recopiées. Seule la
couche d'affichage (les widgets) est nouvelle. Ça veut dire : mêmes
fonctionnalités, mêmes données, même moteur de correction — seul
l'habillage change.

## Comment l'essayer

```
pip install -r requirements.txt   # ajoute customtkinter sur cette branche
python qcm_app_ctk_prototype.py
```

Fonctionne réellement (pas juste une maquette visuelle) : charger une
classe/CSV, ajouter des photos ou un PDF, prévisualiser, faire pivoter,
ignorer rectos/versos, lancer une correction, voir les résultats,
exporter le CSV — tout comme dans l'application actuelle.

## Ce qui n'est PAS fait

- **Seul l'onglet Scanner/Corriger est réécrit.** Les 3 autres
  (Gestion des données, Générer les feuilles, Préférences) affichent un
  simple message d'attente — les réécrire representerait un travail
  largement plus important (chacun a ses propres tableaux, listes et
  boîtes de dialogue).
- CustomTkinter **n'a pas de widget liste ou tableau natif** —
  contrairement à ttk qui a `Listbox`/`Treeview`. La liste de photos et
  le tableau de résultats restent donc les mêmes widgets `tk`/`ttk`
  qu'avant, simplement recolorés pour se fondre dans le nouveau style.
  Une version 100 % native demanderait de reconstruire ces deux
  éléments à la main (lignes cliquables dans un `CTkScrollableFrame`,
  tri des colonnes...) — faisable, mais un gros morceau en plus.
- Le mode sombre (que CustomTkinter gère nativement) n'est pas câblé
  aux préférences de l'application.
- Le `.exe` portable n'a pas été reconstruit avec ce prototype (aucun
  run CI lancé sur cette branche) : PyInstaller aurait probablement
  besoin d'un `--collect-data customtkinter` du même genre que celui
  déjà ajouté pour sv_ttk.

## sv_ttk (sur `main`) vs ce prototype — le compromis

| | sv_ttk (actuel, sur `main`) | CustomTkinter (ce prototype) |
|---|---|---|
| Changement de code | ~10 lignes, zéro risque | Réécriture widget par widget |
| Résultat visuel | Plat, propre, style Windows 11 | Cartes arrondies, boutons colorés, plus "appli moderne" |
| Effort pour tout couvrir | Déjà fait, sur toute l'appli | Un onglet fait ; les 3 autres + toutes les boîtes de dialogue restent à faire (plusieurs jours de travail, risque de régressions à chaque écran réécrit) |
| Licence | BSD, gratuit | MIT, gratuit |

En résumé : si le style de ce prototype te plaît, la question est
surtout **combien de temps ça vaut le coup d'y consacrer** pour
réécrire le reste (3 onglets + une dizaine de boîtes de dialogue), avec
le risque de régression que ça implique sur une application que tu
utilises pour de vrai. sv_ttk reste le choix "sûr" déjà en place.
