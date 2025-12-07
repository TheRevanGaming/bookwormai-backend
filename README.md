# Book Worm AI – Backend & Full App System

This repository contains the **entire backend + app UI** for **Book Worm AI**, an all-in-one creator workstation built for:

- Writers & Novelists  
- Game Developers  
- Music Creators  
- Worldbuilders  
- Visual Artists  
- Voice Actors / Audiobook Creators  
- Indie Studios

Book Worm AI is a standalone platform that brings together high-end AI tooling into one unified environment.

---

## 🚀 Features (Backend + App)

### 🧠 Multi-Lab Creative System

Book Worm includes fully isolated creative labs:

- **Book Writing Lab** – storytelling, lore, worldbuilding, characters  
- **Game Dev Lab** – AAA game design systems, combat, AI, quests, bosses  
- **Music Lab** – lyrics, rhythm, song structure, audio scene design  
- **Image Lab** – text-to-image prompt building  
- **Language Lab** – conlang creation (phonetics, grammar, scripts)  
- **Code Lab** – programming helper for scripts, engines, tools  
- **Admin Mode** – owner-only analytics, controls, bypasses  

Each lab is logically isolated, preventing crossover or contamination between creative projects.

---

## 📚 Canon & Project System

Each user project contains:

- Canon Documents  
- Images  
- Language packs  
- Game systems  
- Worldbuilding files  
- Writing drafts  

The backend enforces canon rules so the AI does **NOT contradict established lore**.

---

## 💾 Database

The backend uses **SQLite** with the following core components:

- `projects` table  
- `docs` table (canon)  
- `images` table  
- `languages` table  
- `system_prompt` loader  
- `admin logging`  

Schema auto-initializes on startup.

---

## 🔐 Admin / Owner Mode

Owner mode is activated by:

- Setting `BOOKWORM_OWNER_CODE` in your environment  
- Entering that code one time in the Admin Login UI  

Owner mode grants:

- Infinite generation  
- Subscription bypass  
- Analytics dashboard  
- Vector memory editing (future)  
- System prompt editing  
- Resetting user data  
- Internal testing/dev tools  

---

## 💸 Stripe Subscription Integration

The backend supports:

- Free Tier  
- Basic Tier  
- Pro Tier  
- Patron Tier  

Stripe checkout links are provided from the frontend and stored locally per-browser.

The backend enforces subscription rules for non-owner users.

---

## 🌐 Deployment

This backend is deployed using:

- **Render.com** (Web Service)  
- Python 3.11+  
- Uvicorn server  
- FastAPI

Environment variables required:

