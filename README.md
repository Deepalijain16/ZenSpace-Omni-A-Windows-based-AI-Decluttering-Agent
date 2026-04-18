# 🌌 ZenSpace Omni 

ZenSpace Omni is a high-performance, AI-powered Windows desktop environment and file management agent. Built with Python and CustomTkinter, it operates as a localized master OS overlay that seamlessly indexes, categorizes, and manages all system files across your local drives. 

Powered by the Gemini 2.5 Flash cognitive matrix, ZenSpace Omni allows users to organize their digital workspace using natural language, while also providing a robust suite of manual, rapid-action UI controls.

## ✨ Key Features

* **🌐 System-Wide Live Mapping:** Indexes entire drives (not just specific folders) into a lightning-fast SQLite WAL database, tracking system health and storage utilization in real-time.
* **🤖 Agentic AI Assistant:** Integrated with the Gemini API to execute natural language commands. Ask Omni to "zip my python files," "delete system junk," or "triage my downloads," and the AI stages the actions for your approval.
* **⚙️ Hybrid Action Center:** Features manual override buttons (Trash, Delete, Move, Zip) right alongside the AI prompt, ensuring you never *have* to use AI for quick, day-to-day file operations.
* **⚡ Lightning Duplicate Detection:** Uses a custom triple-point smart hashing algorithm (reading 64KB chunks at the start, middle, and end of files) to instantly hunt down and eliminate duplicate files without locking up RAM.
* **📡 Background Radar:** Utilizes a Watchdog observer to silently track file creations, deletions, and movements in the background, keeping the database constantly synced with the Windows OS.
* **🗑️ Native Recycle Bin Sync:** Fully integrated with the Windows OS Recycle Bin, allowing you to natively restore or permanently nuke deleted files directly from the ZenSpace UI.
* **🔒 Local Security & Profiles:** Android-style multi-profile authentication with password recovery via security questions.

## 🛠️ Tech Stack

* **Language:** Python 3.x
* **GUI Framework:** CustomTkinter (CTk) / Tkinter
* **Database:** SQLite3 (WAL mode optimized)
* **AI Integration:** `google.generativeai` (Gemini 2.5 Flash)
* **System Listeners:** `watchdog`
* **OS Integrations:** `send2trash`, `pywin32` (win32com)
* **System Tray:** `pystray`, `Pillow`

## 🚀 Installation & Setup

**1. Clone the repository**
```bash
git clone [https://github.com/YOUR-USERNAME/zenspace-omni.git](https://github.com/YOUR-USERNAME/zenspace-omni.git)
cd zenspace-omni