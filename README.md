# UlysseTif Bot

Bot Telegram de gestion des trades automatiques.

### Fonctionnalités
- Détection automatique des calls depuis le canal annonces
- Vérification des prix via l’API CoinGecko
- Marquage automatique des PE/TP touchés
- Passage automatique de la SL au BE après TP1
- Commandes manuelles pour les admins :
  - `/edit` : modifier SL ou TP
  - `/clear` : supprimer des trades
  - `/trades` : afficher les trades actifs

### Déploiement sur Render
1. Connectez Render à votre compte GitHub.
2. Créez un **Web Service** à partir de ce dépôt.
3. Choisissez Python 3 et la commande :
