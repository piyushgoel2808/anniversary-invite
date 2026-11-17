# Navneet & Ruby Goel — Silver Jubilee (25th Wedding Anniversary) Invitation

A mobile-first, architectural celebration invitation website celebrating 25 years of togetherness for **Navneet Goel & Ruby Goel** (25 November 2026).

## Features

- **Architectural Framing**: Traditional carved pillars and floral vines continuously frame every section.
- **Silver Foil Scratch Card**: Interactive rub-to-reveal card displaying the date with a celebratory petal burst.
- **Celebration Video**: Animated 3D couple scene under the floral arch with golden sparkles and falling petals.
- **Venue & Google Maps**: Clean ceremony card with 1-tap navigation to Golden Dreams Banquets, Kalkaji, New Delhi.
- **Guest Blessings**: Local SQLite backend for private family storage and static fallback for GitHub Pages.

## Project Structure

```
invite/
├── index.html              # Main invitation website (GitHub Pages entrypoint)
├── blessings.html          # Family blessings dashboard
├── couple-video.mp4        # 3D couple celebration video
├── og-preview.png          # WhatsApp and social media link preview card
├── og-preview.svg          # Vector source for preview card
├── server.py               # Python HTTP + SQLite server
├── blessings.db            # SQLite database (auto-created, gitignored)
├── static/                 # Static assets directory for server.py
└── README.md
```

## Running Locally

To launch the full backend with the SQLite database:

```bash
python3 server.py
```

Then visit:
- **Invitation Website**: [http://localhost:8080](http://localhost:8080)
- **Family Blessings Vault**: [http://localhost:8080/blessings](http://localhost:8080/blessings) *(Password: `goel25`)*

## GitHub Pages Deployment

This repository is structured to deploy directly on **GitHub Pages**:
1. Push this repository to GitHub.
2. In your repository on GitHub, navigate to **Settings** > **Pages**.
3. Under **Build and deployment** > **Source**, choose **Deploy from a branch**.
4. Select `main` branch and `/ (root)` folder, then click **Save**.
5. Your live invitation site will be active at `https://<your-username>.github.io/<repo-name>/`.
