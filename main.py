import os
import shutil
import sqlite3
import threading
import time
import re
import subprocess
import tkinter as tk
import json 
import queue
import hashlib
import zipfile
import gc
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from tkinter import messagebox, ttk, simpledialog, filedialog
import customtkinter as ctk
import google.generativeai as genai
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import pystray
from PIL import Image, ImageDraw

# --- OS RECYCLE BIN CONNECTORS ---
try:
    from send2trash import send2trash
    import win32com.client
    import win32api
    HAS_WIN_TRASH = True
except ImportError:
    HAS_WIN_TRASH = False

# --- 1. CONFIGURATION & MULTI-KEY RELAY ---
# 🔑 PASTE YOUR GEMINI API KEYS HERE:
from dotenv import load_dotenv

load_dotenv()

k1 = os.getenv("GEMINI_KEY_1")
k2 = os.getenv("GEMINI_KEY_2")

API_KEYS = [k for k in (k1, k2) if k]
current_key_index = 0

def init_ai():
    if not API_KEYS: return None
    try:
        genai.configure(api_key=API_KEYS[current_key_index])
        return genai.GenerativeModel('gemini-2.5-flash')
    except: return None

gemini_model = init_ai()

DB_NAME = "zenspace_final.db"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, DB_NAME)
CHAT_HISTORY_FILE = os.path.join(SCRIPT_DIR, "chat_history.json") 

# SINGLE PORTAL
ZEN_TRASH_DIR = os.path.join(SCRIPT_DIR, "ZenTrash_Vault")
os.makedirs(ZEN_TRASH_DIR, exist_ok=True)

ZEN_PORTAL_DIR = os.path.join(os.environ['USERPROFILE'], 'Desktop', '📂 ZenSpace Portal')
os.makedirs(ZEN_PORTAL_DIR, exist_ok=True)

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

BLACKLIST = {
    'WINDOWS', 'PROGRAM FILES', 'PROGRAM FILES (X86)', 'PROGRAMDATA', 
    '$RECYCLE.BIN', 'APPDATA', 'SYSTEM VOLUME INFORMATION', 'PERFLOGS',
    'MSOCACHE', 'RECOVERY', 'APPLICATION DATA', 'ONEDRIVETEMP'
}

CATEGORIES = ["All Types", "Videos", "Photos", "Audio", "Documents", "Archives", "Apps", "System Junk", "Others"]

file_event_queue = queue.Queue()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=60, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL;') 
    conn.execute('''CREATE TABLE IF NOT EXISTS trash 
                    (name TEXT, trash_path TEXT UNIQUE, original_path TEXT, size REAL, deleted_at REAL, cat TEXT DEFAULT 'Others')''')
    
    # 🆕 UPGRADED USERS TABLE WITH SECURITY QUESTIONS
    conn.execute('''CREATE TABLE IF NOT EXISTS users 
                    (username TEXT UNIQUE, password TEXT, q1 TEXT, a1 TEXT, q2 TEXT, a2 TEXT)''')
    
    # 🩹 Failsafe: If you already created a user table without questions, this safely upgrades it
    try:
        conn.execute("ALTER TABLE users ADD COLUMN q1 TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN a1 TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN q2 TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN a2 TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass # Columns already exist
        
    return conn

def get_safe_drives():
    valid_drives = []
    for l in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        d = f"{l}:\\"
        if os.path.exists(d):
            try:
                os.listdir(d) 
                valid_drives.append(d)
            except: pass
    return valid_drives

def get_smart_hash(filepath):
    """Industry-standard fast hash: Reads Start, Middle, and End chunks to guarantee uniqueness."""
    try:
        if os.path.isdir(filepath): return None
        sz = os.path.getsize(filepath)
        
        # If the file is tiny (under 200KB), just read the whole thing instantly
        if sz < 200000:
            with open(filepath, 'rb') as f: return hashlib.md5(f.read()).hexdigest()
            
        # Triple-Point Read for large files (Insanely fast, 100% accurate)
        with open(filepath, 'rb') as f:
            h = hashlib.md5()
            h.update(f.read(65536))           # Read first 64KB
            f.seek(sz // 2)
            h.update(f.read(65536))           # Read middle 64KB
            f.seek(-65536, os.SEEK_END)
            h.update(f.read(65536))           # Read last 64KB
            return h.hexdigest()
    except: return None
# --- 2. SCANNING ENGINE & QUEUED RADAR ---
class LiveHybridScanner:
    def __init__(self, update_ui_cb):
        self.update_ui_cb = update_ui_cb
        self.total_scanned = 0
        self.db_lock = threading.Lock()

    def flush_batch(self, batch):
        with self.db_lock:
            conn = get_db_connection()
            conn.executemany("INSERT OR IGNORE INTO files (name, path, ext, size, cat, mtime) VALUES (?,?,?,?,?,?)", batch)
            conn.commit()
            conn.close()
            self.total_scanned += len(batch)
        self.update_ui_cb(f"Scanning... {self.total_scanned:,} files")

    def scan_path(self, path):
        queue_dirs = [path]
        batch = []
        while queue_dirs:
            current_dir = queue_dirs.pop(0)
            try:
                with os.scandir(current_dir) as entries:
                    for entry in entries:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.upper() not in BLACKLIST and not entry.name.startswith('.'):
                                queue_dirs.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                stat = entry.stat()
                                sz = stat.st_size / (1024 * 1024) 
                                if sz == 0: continue 
                                ext = os.path.splitext(entry.name)[1].lower()
                                
                                is_temp = 'temp' in entry.path.lower()
                                
                                if is_temp or ext in {'.tmp', '.log', '.bak', '.cache'}: cat = "System Junk"
                                elif ext in {'.mp4', '.mkv', '.mov', '.avi', '.m4v'}: cat = "Videos"
                                elif ext in {'.jpg', '.png', '.jpeg', '.gif', '.webp', '.bmp', '.psd'}: cat = "Photos"
                                elif ext in {'.mp3', '.wav', '.aac', '.flac', '.m4a'}: cat = "Audio"
                                elif ext in {'.pdf', '.docx', '.txt', '.xlsx', '.csv', '.pptx', '.json', '.xml', '.md'}: cat = "Documents"
                                elif ext in {'.zip', '.rar', '.7z', '.tar', '.gz'}: cat = "Archives"
                                elif ext in {'.exe', '.msi', '.dmg', '.iso'}: cat = "Apps"
                                else: cat = "Others"
                                
                                batch.append((entry.name, entry.path, ext, sz, cat, stat.st_mtime))
                                if len(batch) >= 1000:
                                    self.flush_batch(batch)
                                    batch = []
                            except: pass
            except: pass
        if batch:
            self.flush_batch(batch)

class BackgroundRadar(FileSystemEventHandler):
    def on_created(self, event):
        if "$RECYCLE.BIN" not in event.src_path.upper():
            file_event_queue.put(('add', event.src_path, event.is_directory))
            
    def on_deleted(self, event):
        if "$RECYCLE.BIN" not in event.src_path.upper():
            file_event_queue.put(('delete', event.src_path, event.is_directory))
            
    def on_moved(self, event):
        if "$RECYCLE.BIN" not in event.src_path.upper():
            file_event_queue.put(('delete', event.src_path, event.is_directory))
            file_event_queue.put(('add', event.dest_path, event.is_directory))

def process_file_events_worker(app_instance=None):
    while True:
        time.sleep(3)
        updates, deletes, dir_deletes = [], [], []
        
        while not file_event_queue.empty():
            action, path, is_dir = file_event_queue.get()
            if action == 'add':
                if not is_dir: updates.append(path)
            elif action == 'delete':
                if is_dir: dir_deletes.append(path)
                else: deletes.append(path)
        
        if updates or deletes or dir_deletes:
            conn = get_db_connection()
            try:
                # 1. Handle Individual File Deletions
                if deletes: 
                    conn.executemany("DELETE FROM files WHERE path=?", [(p,) for p in deletes])
                
                # 2. 🆕 Handle BULK Folder Deletions (The missing fix!)
                if dir_deletes:
                    for dp in dir_deletes:
                        # Safely wipe the folder AND every single file hidden inside it
                        conn.execute("DELETE FROM files WHERE path=?", (dp,))
                        conn.execute("DELETE FROM files WHERE path LIKE ?", (f"{dp}\\%",))
                
                # 3. Handle New Files
                if updates:
                    batch = []
                    for p in updates:
                        try:
                            if not os.path.exists(p) or os.path.isdir(p): continue
                            stat = os.lstat(p)
                            sz = stat.st_size / (1024 * 1024)
                            if sz == 0: continue
                            name = os.path.basename(p)
                            ext = os.path.splitext(name)[1].lower()
                            
                            is_temp = 'temp' in p.lower()
                            if is_temp or ext in {'.tmp', '.log', '.bak', '.cache'}: cat = "System Junk"
                            elif ext in {'.mp4', '.mkv', '.mov', '.avi', '.m4v'}: cat = "Videos"
                            elif ext in {'.jpg', '.png', '.jpeg', '.gif', '.webp', '.bmp', '.psd'}: cat = "Photos"
                            elif ext in {'.mp3', '.wav', '.aac', '.flac', '.m4a'}: cat = "Audio"
                            elif ext in {'.pdf', '.docx', '.txt', '.xlsx', '.csv', '.pptx', '.json', '.xml', '.md'}: cat = "Documents"
                            elif ext in {'.zip', '.rar', '.7z', '.tar', '.gz'}: cat = "Archives"
                            elif ext in {'.exe', '.msi', '.dmg', '.iso'}: cat = "Apps"
                            else: cat = "Others"
                                
                            batch.append((name, p, ext, sz, cat, stat.st_mtime))
                        except: pass
                    if batch: 
                        conn.executemany("INSERT OR IGNORE INTO files (name, path, ext, size, cat, mtime) VALUES (?,?,?,?,?,?)", batch)
                
                conn.commit()
                # Instantly refresh the UI and Storage Stats so you see the changes live!
                if app_instance: app_instance.after(0, app_instance.silent_refresh)
            except: pass
            finally: conn.close()

def start_background_radar(app_instance=None):
    observer = Observer()
    user_path = os.environ['USERPROFILE'] 
    safe_folders = ['Desktop', 'Downloads', 'Documents', 'Pictures']
    event_handler = BackgroundRadar()
    for folder in safe_folders:
        target_path = os.path.join(user_path, folder)
        if os.path.exists(target_path):
            observer.schedule(event_handler, target_path, recursive=True)
    observer.start()
    threading.Thread(target=process_file_events_worker, args=(app_instance,), daemon=True).start()
    return observer

# --- 3. MAIN UI & LOGIC ---
class ZenSpaceOmni(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.withdraw() # Hide OS until authenticated
        self.title("ZENSPACE OMNI | Final Master OS")
        self.geometry("1600x950")

        # 🆕 INIT DATABASE FOR USERS
        conn = get_db_connection()
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS users (username TEXT UNIQUE, password TEXT, q1 TEXT, a1 TEXT, q2 TEXT, a2 TEXT)')
            conn.commit()
        except:
            pass
        finally:
            conn.close()

        self.current_sql_filter = "1=1" 
        self.current_trash_sql_filter = "1=1" 
        self.pending_action_type = None 
        self.pending_target_folder = None
        self.memory_buffer = [] 
        self.current_drive_path = "" 
        self.trash_items_cache = {}
        self.active_user = None # Tracks who is currently logged in
        self.is_scanning = False
        self.checked_paths = set() # 🆕 Persistent Checkbox Memory
        
        # 🔄 RAM-OPTIMIZED UNDO/REDO ENGINE
        self.undo_stack = []
        self.redo_stack = []
        self.MAX_HISTORY = 50 
        
        # 🛡️ API TELEMETRY SYSTEM
        self.last_ai_call_time = 0
        self.ai_processing = False
        
        valid_keys = [k for k in API_KEYS if not k.startswith("PASTE_")]
        self.MAX_RPM = 15 * max(1, len(valid_keys))
        self.MAX_RPD = 1500 * max(1, len(valid_keys))
        self.api_calls_this_minute = 0
        self.api_calls_today = 0
        self.minute_tracker_start = time.time()
        
        self.protocol('WM_DELETE_WINDOW', self.minimize_to_tray)
        self.tray_icon = None

        # LIGHT MODE INITIALIZATION
        self.style = ttk.Style(self)
        self.style.theme_use("default")
        self.style.configure("Treeview", background="#ecf0f1", foreground="black", rowheight=32, fieldbackground="#ecf0f1", font=("Arial", 11))
        self.style.map('Treeview', background=[('selected', '#3498db')])
        self.style.configure("Treeview.Heading", background="#bdc3c7", foreground="black", font=("Arial", 12, "bold"), padding=5)

        # --- SIDEBAR ---
        self.sidebar = ctk.CTkFrame(self, width=280, corner_radius=0)
        self.sidebar.pack(side="left", fill="y")
        ctk.CTkLabel(self.sidebar, text="ZENSPACE", font=("Arial", 28, "bold")).pack(pady=15)
        
        btn_font = ctk.CTkFont(family="Arial", size=13, weight="bold")
        
        self.btn_scan = ctk.CTkButton(self.sidebar, text="🔍 Map Storage", height=40, font=btn_font, command=self.start_scan)
        self.btn_scan.pack(pady=4, padx=20, fill="x")
        
        ctk.CTkButton(self.sidebar, text="🌐 All Files", height=40, font=btn_font, command=self.reset_filters).pack(pady=4, padx=20, fill="x")
        ctk.CTkButton(self.sidebar, text="📅 Filter Date", height=40, font=btn_font, fg_color="#E67E22", command=self.ask_date_filter).pack(pady=4, padx=20, fill="x")
        ctk.CTkButton(self.sidebar, text="🧹 System Junk", height=40, font=btn_font, command=lambda: self.prepare_action("RECYCLE", "cat='System Junk'")).pack(pady=4, padx=20, fill="x")
        
        self.btn_dupes = ctk.CTkButton(self.sidebar, text="⚡ Lightning Dupes", height=40, font=btn_font, command=self.trigger_lightning_dupes)
        self.btn_dupes.pack(pady=4, padx=20, fill="x")
        
        # --- STORAGE UI & LIVE STATS ---
        storage_header = ctk.CTkLabel(self.sidebar, text="STORAGE HEALTH (Click for details)", font=("Arial", 11, "bold"), text_color="#3498db", cursor="hand2")
        storage_header.pack(pady=(15, 5))
        
        self.usage_lbl = ctk.CTkLabel(self.sidebar, text="Used: 0 GB / 0 GB", font=("Arial", 13, "bold"), cursor="hand2")
        self.usage_lbl.pack()
        
        self.storage_bar = ctk.CTkProgressBar(self.sidebar, width=200, height=12, fg_color="#1a1a1a", progress_color="#3498db", cursor="hand2")
        self.storage_bar.set(0)
        self.storage_bar.pack(pady=5)
        
        self.free_lbl = ctk.CTkLabel(self.sidebar, text="Free: 0 GB Left", font=("Arial", 11), text_color="gray", cursor="hand2")
        self.free_lbl.pack(pady=(0, 5))
        
        # Bind the mouse clicks to launch the Advanced Drive Matrix
        storage_header.bind("<Button-1>", self.show_drive_analysis)
        self.usage_lbl.bind("<Button-1>", self.show_drive_analysis)
        self.storage_bar.bind("<Button-1>", self.show_drive_analysis)
        self.free_lbl.bind("<Button-1>", self.show_drive_analysis)
        
        self.total_files_lbl = ctk.CTkLabel(self.sidebar, text="Total Files Mapped: 0", font=("Arial", 12, "bold"), text_color="#E67E22")
        self.total_files_lbl.pack(pady=(5, 10))
        
        # RESTORED & EXPANDED FILE CATEGORIES
        self.stats_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        self.stats_frame.pack(pady=0, padx=20, fill="x")
        
        # We store these labels dynamically so they instantly update
        self.cat_labels = {}
        all_cats = ["Videos", "Photos", "Audio", "Documents", "Archives", "Apps", "System Junk", "Others"]
        
        for cat in all_cats:
            self.cat_labels[cat] = self.create_stat_row(f"{cat}:", "0 MB (0)", cat)

        ctk.CTkLabel(self.sidebar, text="API TELEMETRY", font=("Arial", 11, "bold"), text_color="gray").pack(pady=(15, 5))
        self.api_rpm_lbl = ctk.CTkLabel(self.sidebar, text=f"Tasks Left (Min): {self.MAX_RPM}", font=("Arial", 12, "bold"), text_color="#2ecc71")
        self.api_rpm_lbl.pack()
        self.api_rpd_lbl = ctk.CTkLabel(self.sidebar, text=f"Tasks Left (Day): {self.MAX_RPD}", font=("Arial", 11), text_color="#2ecc71")
        self.api_rpd_lbl.pack()

        self.status_lbl = ctk.CTkLabel(self.sidebar, text="System Idle", font=("Arial", 10), text_color="gray")
        self.status_lbl.pack(side="bottom", pady=15)

        # --- 🗂️ MAIN RIGHT AREA CONTAINER ---
        self.main_area = ctk.CTkFrame(self, fg_color="transparent")
        self.main_area.pack(side="right", fill="both", expand=True)

        # --- 🎛️ TOP NAVIGATION BAR (Clean UI) ---
        self.top_bar = ctk.CTkFrame(self.main_area, fg_color="transparent", height=40)
        self.top_bar.pack(side="top", fill="x", padx=10, pady=(10, 0))

        top_btn_font = ctk.CTkFont(family="Arial", size=12, weight="bold")

        self.btn_logout = ctk.CTkButton(self.top_bar, text="🚪 Logout", width=110, height=35, font=top_btn_font, fg_color="#c0392b", command=self.logout_user)
        self.btn_logout.pack(side="right", padx=5)

        self.btn_update = ctk.CTkButton(self.top_bar, text="🔄 Update", width=110, height=35, font=top_btn_font, fg_color="#2980b9", command=self.check_updates)
        self.btn_update.pack(side="right", padx=5)

        self.appearance_mode = "Light"
        self.btn_theme = ctk.CTkButton(self.top_bar, text="🌓 Theme", width=110, height=35, font=top_btn_font, fg_color="#34495E", command=self.toggle_theme)
        self.btn_theme.pack(side="right", padx=5)

        # --- MAIN TABS ---
        self.tabs = ctk.CTkTabview(self.main_area, corner_radius=10)
        self.tabs.pack(side="top", fill="both", expand=True, padx=10, pady=(5, 10))
        
        self.tab_action = self.tabs.add("Action Center") 
        self.tab_history = self.tabs.add("Action Ledger") 
        self.tab_drive = self.tabs.add("Drive Manager")
        self.tab_trash = self.tabs.add("Zen Trash")
        self.tab_omni = self.tabs.add("Omni Assistant")

        self.setup_action_tab()
        self.setup_history_tab() 
        self.setup_drive_tab()
        self.setup_trash_tab()
        self.setup_omni_tab()
        self.update_sidebar_stats()
        
        self.radar_observer = start_background_radar(self)
        self.load_chat_history() 
        self.update_trash_view(silent=False) 
        
        if not HAS_WIN_TRASH:
            self.after(1000, lambda: messagebox.showwarning(
                "Missing OS Connectors", 
                "To seamlessly connect ZenSpace with the native Windows OS Recycle Bin, you need two standard libraries.\n\nPlease open your terminal and run:\npip install send2trash pywin32\n\nThe app will now close so you can install them."
            ))
            self.after(5000, self.destroy)
            
        self.start_silent_hunter() # Boot the background duplicate hunter
        self.show_login_panel()    # Trigger the login screen
    # --- 🔒 MULTI-PROFILE AUTHENTICATION SYSTEM ---
    # --- 👻 SILENT GHOST SWEEPER ---
    def trigger_ghost_sweeper(self):
        """Silently cleans the database of files deleted outside of the watched folders."""
        threading.Thread(target=self._ghost_sweeper_thread, daemon=True).start()

    def _ghost_sweeper_thread(self):
        conn = get_db_connection()
        try:
            # Grab every single file path the database thinks we have
            rows = conn.execute("SELECT path FROM files").fetchall()
            missing_files = []
            
            for (path,) in rows:
                # If the file physically doesn't exist anymore, flag it
                if not os.path.exists(path):
                    missing_files.append((path,))
            
            if missing_files:
                # Mass-delete the ghosts from the database
                conn.executemany("DELETE FROM files WHERE path=?", missing_files)
                conn.commit()
                
                # Instantly update the UI with the newly freed storage numbers
                self.after(0, self.update_sidebar_stats)
                self.after(0, self.update_view)
                self.after(0, lambda: self.log_to(self.omni_log, f"👻 Omni: Swept away {len(missing_files)} ghost files that were permanently deleted off-radar. Stats updated!"))
        except Exception as e:
            pass
        finally:
            conn.close()
    # --- 🔒 ANDROID-STYLE AUTHENTICATION & RECOVERY ---
    def show_login_panel(self):
        if hasattr(self, 'auth_window') and self.auth_window.winfo_exists():
            self.auth_window.destroy()
            
        self.auth_window = ctk.CTkToplevel(self)
        self.auth_window.title("ZenSpace Login")
        self.auth_window.attributes('-topmost', True)
        self.auth_window.protocol("WM_DELETE_WINDOW", self.quit_app) 

        conn = get_db_connection()
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        conn.close()

        if user_count == 0:
            # 🆕 REGISTRATION: Ask for Security Questions
            self.auth_window.geometry("450x700")
            self.center_window(self.auth_window, 450, 700)
            
            ctk.CTkLabel(self.auth_window, text="🔒 ZENSPACE", font=("Arial", 28, "bold")).pack(pady=(30, 5))
            ctk.CTkLabel(self.auth_window, text="Create your Master Profile & Security Setup", font=("Arial", 12), text_color="#E67E22").pack(pady=(0, 20))
            
            self.new_user_entry = ctk.CTkEntry(self.auth_window, placeholder_text="Username", width=300, height=35)
            self.new_user_entry.pack(pady=5)
            self.new_pass_entry = ctk.CTkEntry(self.auth_window, placeholder_text="Password", show="*", width=300, height=35)
            self.new_pass_entry.pack(pady=5)
            
            sec_questions = [
                "What was the name of your first pet?",
                "In what city were you born?",
                "What is your mother's maiden name?",
                "What was your childhood nickname?"
            ]
            
            ctk.CTkLabel(self.auth_window, text="Security Question 1:", font=("Arial", 11, "bold")).pack(pady=(15,0))
            self.q1_combo = ctk.CTkComboBox(self.auth_window, values=sec_questions, width=300)
            self.q1_combo.set(sec_questions[0])
            self.q1_combo.pack(pady=5)
            self.a1_entry = ctk.CTkEntry(self.auth_window, placeholder_text="Answer 1", width=300, height=35)
            self.a1_entry.pack(pady=5)

            ctk.CTkLabel(self.auth_window, text="Security Question 2:", font=("Arial", 11, "bold")).pack(pady=(10,0))
            self.q2_combo = ctk.CTkComboBox(self.auth_window, values=sec_questions, width=300)
            self.q2_combo.set(sec_questions[1])
            self.q2_combo.pack(pady=5)
            self.a2_entry = ctk.CTkEntry(self.auth_window, placeholder_text="Answer 2", width=300, height=35)
            self.a2_entry.pack(pady=5)
            
            ctk.CTkButton(self.auth_window, text="Create Profile & Boot OS", font=("Arial", 14, "bold"), width=300, height=45, fg_color="#8e44ad", command=self.register_user).pack(pady=20)
        else:
            # 🔐 STANDARD LOGIN
            self.auth_window.geometry("400x500")
            self.center_window(self.auth_window, 400, 500)
            
            ctk.CTkLabel(self.auth_window, text="🔒 ZENSPACE", font=("Arial", 28, "bold")).pack(pady=(50, 10))
            ctk.CTkLabel(self.auth_window, text="Device Locked. Please authenticate.", font=("Arial", 12), text_color="gray").pack(pady=(0, 30))
            
            self.user_entry = ctk.CTkEntry(self.auth_window, placeholder_text="Username", width=250, height=40)
            self.user_entry.pack(pady=10)
            self.pass_entry = ctk.CTkEntry(self.auth_window, placeholder_text="Password", show="*", width=250, height=40)
            self.pass_entry.pack(pady=10)
            self.pass_entry.bind("<Return>", lambda e: self.verify_login())
            
            ctk.CTkButton(self.auth_window, text="Access System", font=("Arial", 14, "bold"), width=250, height=45, fg_color="#2ecc71", command=self.verify_login).pack(pady=(20, 10))
            
            # 🆕 REPLACED "CREATE NEW" WITH "FORGOT / CHANGE PASSWORD"
            ctk.CTkButton(self.auth_window, text="⚙️ Forgot / Change Password", font=("Arial", 11), width=250, height=30, fg_color="transparent", border_width=1, command=self.show_password_manager).pack(pady=5)

        self.auth_error_lbl = ctk.CTkLabel(self.auth_window, text="", text_color="#c0392b", font=("Arial", 11))
        self.auth_error_lbl.pack()

    def center_window(self, win, width, height):
        win.update_idletasks()
        x = (win.winfo_screenwidth() // 2) - (width // 2)
        y = (win.winfo_screenheight() // 2) - (height // 2)
        win.geometry(f"+{x}+{y}")

    def register_user(self):
        user = self.new_user_entry.get().strip()
        pwd = self.new_pass_entry.get().strip()
        q1 = self.q1_combo.get()
        a1 = self.a1_entry.get().strip().lower() # Set to lowercase for forgiving auth later
        q2 = self.q2_combo.get()
        a2 = self.a2_entry.get().strip().lower()

        if not user or not pwd or not a1 or not a2:
            self.auth_error_lbl.configure(text="❌ All fields must be filled out.")
            return
        if q1 == q2:
            self.auth_error_lbl.configure(text="❌ Please select two different security questions.")
            return
            
        conn = get_db_connection()
        try:
            conn.execute("INSERT INTO users (username, password, q1, a1, q2, a2) VALUES (?, ?, ?, ?, ?, ?)", (user, pwd, q1, a1, q2, a2))
            conn.commit()
            self.active_user = user
            self.boot_os()
        except sqlite3.IntegrityError:
            self.auth_error_lbl.configure(text="❌ Username already exists.")
        finally:
            conn.close()

    def verify_login(self):
        user = self.user_entry.get().strip()
        pwd = self.pass_entry.get().strip()
        
        conn = get_db_connection()
        result = conn.execute("SELECT * FROM users WHERE username=? AND password=?", (user, pwd)).fetchone()
        conn.close()
        
        if result:
            self.active_user = user
            self.boot_os()
        else:
            self.auth_error_lbl.configure(text="❌ Invalid Credentials. Access Denied.")

    # --- 🚪 SESSION & PERSISTENCE MANAGEMENT ---
    def boot_os(self):
        """Intelligently boots the OS without forcing a rescan if data already exists."""
        self.auth_window.destroy()
        self.deiconify() 
        
        conn = get_db_connection()
        try:
            # Check if the database already has live files
            file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        except:
            file_count = 0
        finally:
            conn.close()
            
        if file_count == 0:
            self.log_to(self.omni_log, "🤖 Omni: First boot detected! Running initial deep scan...")
            self.start_scan()
        else:
            self.log_to(self.omni_log, f"🤖 Omni: Welcome back, {self.active_user}! Loading your live persistent workspace...")
            self.update_view()
            self.update_sidebar_stats()
            self.update_trash_view(silent=True)
            
            # 🚀 NEW: Automatically trigger the Duplicate Analyzer half a second after login
            self.after(500, self.trigger_lightning_dupes)
            self.trigger_ghost_sweeper()

    def logout_user(self):
        if messagebox.askyesno("Logout", "Are you sure you want to securely lock the system and log out?"):
            self.active_user = None
            self.withdraw() # Instantly hide the OS window to protect data
            self.show_login_panel()

    # --- 🔄 OTA AUTO-UPDATER ENGINE ---
    def check_updates(self):
        self.log_to(self.omni_log, "🔄 Omni: Pinging GitHub servers for the latest system architecture...")
        self.tabs.set("Omni Assistant")
        self.btn_update.configure(state="disabled", text="Checking...")
        threading.Thread(target=self._perform_update, daemon=True).start()

    def _perform_update(self):
        # ⚠️ Replace this URL with the exact "Raw" GitHub URL of your main.py file once you upload it
        update_url = "https://raw.githubusercontent.com/YOUR-USERNAME/YOUR-REPO-NAME/main/main.py"
        
        try:
            # For now, this is a dry-run simulation until your repo is live
            time.sleep(2) 
            self.after(0, lambda: self.log_to(self.omni_log, "✅ Omni: You are running the latest version! (OTA Framework is armed and ready)."))
            
            """ 
            # UNCOMMENT THIS BLOCK WHEN YOUR GITHUB IS LIVE:
            req = urllib.request.Request(update_url, headers={'Cache-Control': 'no-cache'})
            with urllib.request.urlopen(req) as response:
                latest_code = response.read().decode('utf-8')
                
            if "class ZenSpaceOmni" in latest_code:
                with open(__file__, 'w', encoding='utf-8') as f:
                    f.write(latest_code)
                
                self.after(0, lambda: messagebox.showinfo("System Update", "ZenSpace has successfully downloaded a new update! The OS will now reboot."))
                os.startfile(sys.executable, __file__) # Reboot the script
                self.quit_app()
            """
        except Exception as e:
            self.after(0, lambda: self.log_to(self.omni_log, f"⚠️ Update connection failed: {e}"))
        finally:
            self.after(0, lambda: self.btn_update.configure(state="normal", text="🔄 Check for Updates"))
    # --- ⚙️ PASSWORD MANAGER (OLD PASS + SECURITY QUESTIONS) ---
    def show_password_manager(self):
        self.auth_window.destroy()
        
        self.auth_window = ctk.CTkToplevel(self)
        self.auth_window.title("ZenSpace | Account Security")
        self.auth_window.geometry("450x650")
        self.auth_window.attributes('-topmost', True)
        self.auth_window.protocol("WM_DELETE_WINDOW", self.quit_app) 
        self.center_window(self.auth_window, 450, 650)

        ctk.CTkLabel(self.auth_window, text="⚙️ SECURITY", font=("Arial", 28, "bold")).pack(pady=(30, 10))
        
        self.tabs_pm = ctk.CTkTabview(self.auth_window)
        self.tabs_pm.pack(fill="both", expand=True, padx=20, pady=10)
        
        # TAB 1: OLD PASSWORD
        t1 = self.tabs_pm.add("Use Old Password")
        self.pm_u_entry1 = ctk.CTkEntry(t1, placeholder_text="Username", width=250, height=35)
        self.pm_u_entry1.pack(pady=10)
        self.pm_old_p_entry = ctk.CTkEntry(t1, placeholder_text="Old Password", show="*", width=250, height=35)
        self.pm_old_p_entry.pack(pady=10)
        self.pm_new_p_entry1 = ctk.CTkEntry(t1, placeholder_text="New Password", show="*", width=250, height=35)
        self.pm_new_p_entry1.pack(pady=10)
        ctk.CTkButton(t1, text="Update Password", fg_color="#3498db", font=("Arial", 12, "bold"), width=250, height=40, command=self.change_pass_old).pack(pady=20)
        
        # TAB 2: SECURITY QUESTIONS
        t2 = self.tabs_pm.add("Forgot Password")
        self.pm_u_entry2 = ctk.CTkEntry(t2, placeholder_text="Username", width=250, height=35)
        self.pm_u_entry2.pack(pady=(10, 5))
        ctk.CTkButton(t2, text="Fetch Questions", fg_color="#e67e22", font=("Arial", 12, "bold"), width=250, height=35, command=self.fetch_questions).pack(pady=5)
        
        self.q_frame = ctk.CTkFrame(t2, fg_color="transparent")
        self.q_frame.pack(fill="x", pady=5)
        
        self.lbl_q1 = ctk.CTkLabel(self.q_frame, text="Q1: ---", text_color="#E67E22", wraplength=250)
        self.lbl_q1.pack(pady=(5,0))
        self.pm_a1_entry = ctk.CTkEntry(self.q_frame, placeholder_text="Answer 1", width=250, height=35)
        self.pm_a1_entry.pack(pady=5)
        
        self.lbl_q2 = ctk.CTkLabel(self.q_frame, text="Q2: ---", text_color="#E67E22", wraplength=250)
        self.lbl_q2.pack(pady=(5,0))
        self.pm_a2_entry = ctk.CTkEntry(self.q_frame, placeholder_text="Answer 2", width=250, height=35)
        self.pm_a2_entry.pack(pady=5)
        
        self.pm_new_p_entry2 = ctk.CTkEntry(self.q_frame, placeholder_text="New Password", show="*", width=250, height=35)
        self.pm_new_p_entry2.pack(pady=15)
        
        self.btn_reset = ctk.CTkButton(self.q_frame, text="Reset Password", fg_color="#2ecc71", font=("Arial", 12, "bold"), width=250, height=40, state="disabled", command=self.change_pass_questions)
        self.btn_reset.pack(pady=5)

        ctk.CTkButton(self.auth_window, text="⬅️ Back to Login", font=("Arial", 11), width=250, height=30, fg_color="transparent", border_width=1, command=self.show_login_panel).pack(pady=(5, 20))
        
        self.pm_error_lbl = ctk.CTkLabel(self.auth_window, text="", text_color="#c0392b", font=("Arial", 11))
        self.pm_error_lbl.pack(pady=5)
        
    def fetch_questions(self):
        user = self.pm_u_entry2.get().strip()
        if not user:
            self.pm_error_lbl.configure(text="❌ Enter a username first.")
            return
            
        conn = get_db_connection()
        row = conn.execute("SELECT q1, q2 FROM users WHERE username=?", (user,)).fetchone()
        conn.close()
        
        if row and row[0] and row[1]:
            self.lbl_q1.configure(text=f"Q1: {row[0]}")
            self.lbl_q2.configure(text=f"Q2: {row[1]}")
            self.btn_reset.configure(state="normal")
            self.pm_error_lbl.configure(text="✅ Security questions loaded.", text_color="#2ecc71")
        else:
            self.pm_error_lbl.configure(text="❌ User not found or no questions set.", text_color="#c0392b")

    def change_pass_old(self):
        user = self.pm_u_entry1.get().strip()
        old_p = self.pm_old_p_entry.get().strip()
        new_p = self.pm_new_p_entry1.get().strip()
        
        if not user or not old_p or not new_p:
            self.pm_error_lbl.configure(text="❌ All fields are required.", text_color="#c0392b")
            return
            
        conn = get_db_connection()
        row = conn.execute("SELECT * FROM users WHERE username=? AND password=?", (user, old_p)).fetchone()
        if row:
            conn.execute("UPDATE users SET password=? WHERE username=?", (new_p, user))
            conn.commit()
            self.pm_error_lbl.configure(text="✅ Password updated successfully! Redirecting...", text_color="#2ecc71")
            self.after(1500, self.show_login_panel)
        else:
            self.pm_error_lbl.configure(text="❌ Invalid Username or Old Password.", text_color="#c0392b")
        conn.close()

    def change_pass_questions(self):
        user = self.pm_u_entry2.get().strip()
        a1 = self.pm_a1_entry.get().strip().lower()
        a2 = self.pm_a2_entry.get().strip().lower()
        new_p = self.pm_new_p_entry2.get().strip()
        
        if not user or not a1 or not a2 or not new_p:
            self.pm_error_lbl.configure(text="❌ All fields are required.", text_color="#c0392b")
            return
            
        conn = get_db_connection()
        row = conn.execute("SELECT a1, a2 FROM users WHERE username=?", (user,)).fetchone()
        
        if row and row[0] == a1 and row[1] == a2:
            conn.execute("UPDATE users SET password=? WHERE username=?", (new_p, user))
            conn.commit()
            self.pm_error_lbl.configure(text="✅ Password reset successfully! Redirecting...", text_color="#2ecc71")
            self.after(1500, self.show_login_panel)
        else:
            self.pm_error_lbl.configure(text="❌ Incorrect answers to security questions.", text_color="#c0392b")
        conn.close()

    # --- ACTION LEDGER & UNDO ENGINE UI ---
    def setup_history_tab(self):
        ctrl = ctk.CTkFrame(self.tab_history, fg_color="transparent")
        ctrl.pack(fill="x", pady=10, padx=10)
        
        ctk.CTkLabel(ctrl, text="Command History Ledger", font=("Arial", 20, "bold")).pack(side="left")
        
        self.btn_redo = ctk.CTkButton(ctrl, text="↪️ Redo Action", width=120, fg_color="#2980b9", font=("Arial", 12, "bold"), command=self.redo_action)
        self.btn_redo.pack(side="right", padx=5)
        
        self.btn_undo = ctk.CTkButton(ctrl, text="↩️ Undo Action", width=120, fg_color="#e67e22", font=("Arial", 12, "bold"), command=self.undo_action)
        self.btn_undo.pack(side="right", padx=5)
        
        tree_frame = ctk.CTkFrame(self.tab_history, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        scroll_y = ttk.Scrollbar(tree_frame, orient="vertical")
        scroll_y.pack(side="right", fill="y")

        self.history_tree = ttk.Treeview(tree_frame, columns=("Time", "Action", "Files", "Impact", "Details"), show='headings', yscrollcommand=scroll_y.set)
        
        self.history_tree.heading("Time", text="Timestamp")
        self.history_tree.column("Time", width=150, anchor="center")
        self.history_tree.heading("Action", text="Command")
        self.history_tree.column("Action", width=120, anchor="center")
        self.history_tree.heading("Files", text="Files Affected")
        self.history_tree.column("Files", width=100, anchor="center")
        self.history_tree.heading("Impact", text="Storage Impact")
        self.history_tree.column("Impact", width=120, anchor="center")
        self.history_tree.heading("Details", text="Execution Details")
        self.history_tree.column("Details", width=500, anchor="w", stretch=True)
        
        self.history_tree.pack(side="left", fill="both", expand=True)
        scroll_y.config(command=self.history_tree.yview)
        self.update_history_ui()

    def update_history_ui(self):
        for i in self.history_tree.get_children(): self.history_tree.delete(i)
        
        self.btn_undo.configure(state="normal" if self.undo_stack else "disabled")
        self.btn_redo.configure(state="normal" if self.redo_stack else "disabled")
        
        for record in reversed(self.undo_stack):
            dt = time.strftime('%H:%M:%S', time.localtime(record["timestamp"]))
            action = record["action"]
            count = record["count"]
            sz = f"{record['size']:.2f} MB"
            
            if action in ["ZIP", "PACK"]: impact = f"📦 Wrapped {sz}"
            elif action in ["RECYCLE", "DELETE"]: impact = f"🗑️ Freed {sz}"
            else: impact = f"🔄 Moved {sz}"
            
            details = record["details"]
            self.history_tree.insert("", "end", values=(dt, action, f"{count} files", impact, details))

    def push_undo(self, record):
        self.undo_stack.append(record)
        if len(self.undo_stack) > self.MAX_HISTORY:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        self.update_history_ui()

    def undo_action(self):
        if not self.undo_stack: return
        record = self.undo_stack.pop()
        action = record["action"]
        
        try:
            if action == "MOVE" or action == "RENAME":
                for old_p, new_p in record["moves"]:
                    if os.path.exists(new_p): shutil.move(new_p, old_p)
                self.log_to(self.omni_log, f"↩️ Undone: Reverted {record['count']} file {action.lower()}s.")
                
            elif action == "COPY":
                for old_p, new_p in record["moves"]:
                    if os.path.exists(new_p): os.remove(new_p)
                self.log_to(self.omni_log, f"↩️ Undone: Deleted copied files.")
                
            elif action == "ZIP":
                z_path = record["zip_path"]
                if z_path and os.path.exists(z_path): os.remove(z_path)
                self.log_to(self.omni_log, f"↩️ Undone: Deleted ZIP archive.")
                
            elif action == "PACK":
                z_path = record["zip_path"]
                if z_path and os.path.exists(z_path):
                    for old_p, zip_internal_name in record["moves"]:
                        os.makedirs(os.path.dirname(old_p), exist_ok=True)
                        with zipfile.ZipFile(z_path, 'r') as zf:
                            with open(old_p, 'wb') as f_out:
                                f_out.write(zf.read(zip_internal_name))
                    os.remove(z_path)
                self.log_to(self.omni_log, f"↩️ Undone: Unpacked ZIP and restored {record['count']} original files.")
                
            elif action in ["RECYCLE", "DELETE"]:
                messagebox.showinfo("OS Restriction", "Files sent to the Windows Recycle Bin must be restored natively.\n\nPlease go to the 'Zen Trash' tab, select the files, and click 'Native Restore Selected'.")
                self.undo_stack.append(record)
                return
                
            self.redo_stack.append(record)
            self.update_history_ui()
            self.silent_refresh()
            messagebox.showinfo("Undo Successful", f"Successfully reversed the {action} command.")
            
        except Exception as e:
            messagebox.showerror("Undo Failed", f"Could not complete undo: {e}\nFiles may have been modified manually since the action.")

    def redo_action(self):
        if not self.redo_stack: return
        record = self.redo_stack.pop()
        action = record["action"]
        
        try:
            if action == "MOVE" or action == "RENAME":
                for old_p, new_p in record["moves"]:
                    if os.path.exists(old_p): shutil.move(old_p, new_p)
            
            elif action == "COPY":
                for old_p, new_p in record["moves"]:
                    if os.path.exists(old_p): shutil.copy2(old_p, new_p)
                    
            elif action == "ZIP" or action == "PACK":
                z_path = record["zip_path"]
                with zipfile.ZipFile(z_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for old_p, zip_internal_name in record["moves"]:
                        if os.path.exists(old_p): 
                            zf.write(old_p, zip_internal_name)
                            if action == "PACK": os.remove(old_p)
                            
            elif action in ["RECYCLE", "DELETE"]:
                for old_p, _ in record["moves"]:
                    if os.path.exists(old_p):
                        if HAS_WIN_TRASH: send2trash(os.path.normpath(old_p))
                        else: os.remove(old_p)

            self.log_to(self.omni_log, f"↪️ Redone: Re-applied {action} command on {record['count']} files.")
            self.undo_stack.append(record)
            self.update_history_ui()
            self.silent_refresh()
            
        except Exception as e:
            messagebox.showerror("Redo Failed", f"Could not complete redo: {e}")

    # --- CATEGORY & EXTENSION DRILL-DOWN ---
    def show_extension_files(self, category, ext):
        if hasattr(self, 'ext_popup') and self.ext_popup.winfo_exists():
            self.ext_popup.destroy()
            
        self.ext_popup = ctk.CTkToplevel(self)
        ext_display = ext if ext else "[No Ext]"
        self.ext_popup.title(f"{ext_display} Files in {category}")
        self.ext_popup.geometry("900x600")
        
        # 🚀 CRITICAL UI FIX: Dynamic Hierarchy!
        # If the Breakdown menu is open, we tell the OS this new window is its child.
        # This guarantees it perfectly stacks ON TOP of the Breakdown menu!
        if hasattr(self, 'cat_popup') and self.cat_popup.winfo_exists():
            self.ext_popup.transient(self.cat_popup)
        else:
            self.ext_popup.transient(self)
            
        self.ext_popup.focus_force() # Steal focus so you can interact instantly
        
        self.ext_popup.update_idletasks()
        x = (self.ext_popup.winfo_screenwidth() // 2) - (900 // 2)
        y = (self.ext_popup.winfo_screenheight() // 2) - (600 // 2)
        self.ext_popup.geometry(f"+{x}+{y}")

        conn = get_db_connection()
        query_ext = ext if ext != "[No Ext]" else ""
        try: 
            rows = conn.execute("SELECT name, size, path, mtime FROM files WHERE cat=? AND ext=? ORDER BY size DESC", (category, query_ext)).fetchall()
        except: 
            rows = []
        finally: 
            conn.close()

        total_files = len(rows)
        total_size = sum(r[1] for r in rows)

        header_frame = ctk.CTkFrame(self.ext_popup, fg_color="transparent")
        header_frame.pack(fill="x", padx=15, pady=15)
        ctk.CTkLabel(header_frame, text=f"📄 {ext_display} Overview", font=("Arial", 20, "bold")).pack(side="left")
        ctk.CTkLabel(header_frame, text=f"{total_files} Files | {total_size:.2f} MB", font=("Arial", 16, "bold"), text_color="#E67E22").pack(side="right")

        tree_frame = ctk.CTkFrame(self.ext_popup, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=15, pady=5)
        scroll_y = ttk.Scrollbar(tree_frame, orient="vertical")
        scroll_y.pack(side="right", fill="y")

        ext_tree = ttk.Treeview(tree_frame, columns=("Name", "Size", "Date", "Path"), show='headings', yscrollcommand=scroll_y.set)
        ext_tree.heading("Name", text="File Name"); ext_tree.column("Name", width=300, anchor="w")
        ext_tree.heading("Size", text="Size"); ext_tree.column("Size", width=80, anchor="center")
        ext_tree.heading("Date", text="Modified"); ext_tree.column("Date", width=120, anchor="center")
        ext_tree.heading("Path", text="Location"); ext_tree.column("Path", width=350, anchor="w", stretch=True)
        ext_tree.pack(side="left", fill="both", expand=True)
        scroll_y.config(command=ext_tree.yview)

        for r in rows:
            dt = time.strftime('%d/%m/%y %H:%M', time.localtime(r[3]))
            ext_tree.insert("", "end", values=(r[0], f"{r[1]:.2f} MB", dt, r[2]))

        # --- 🛡️ CRASH-PROOF UI HANDLERS ---
        def on_ext_double_click(event):
            item = ext_tree.identify_row(event.y)
            if item: 
                path = ext_tree.item(item, "values")[3]
                self.open_native_file(path)

        def on_ext_right_click(event):
            item = ext_tree.identify_row(event.y)
            if item:
                ext_tree.selection_set(item)
                path = ext_tree.item(item, "values")[3]
                
                # Build the menu
                menu = tk.Menu(self.ext_popup, tearoff=0, bg="#2b2b2b", fg="white", font=("Arial", 11), activebackground="#3498db")
                menu.add_command(label="📄 Open File", command=lambda p=path: self.open_native_file(p))
                menu.add_command(label="📁 Open File Location", command=lambda p=path: self.open_file_location(p))
                menu.add_separator()
                menu.add_command(label="📋 Copy Path", command=lambda p=path: self.clipboard_copy(p))
                
                # 🚀 CRITICAL FIX: Use tk_popup instead of post() to prevent UI freezing!
                try:
                    menu.tk_popup(event.x_root, event.y_root)
                finally:
                    menu.grab_release()

        # Bind the handlers to the tree
        ext_tree.bind('<Double-1>', on_ext_double_click)
        ext_tree.bind('<Button-3>', on_ext_right_click)
        
        ctk.CTkLabel(self.ext_popup, text="💡 Double-click to open | Right-click for options", font=("Arial", 12), text_color="gray").pack(pady=10)
    
    def show_category_breakdown(self, category):
        # 🆕 Prevent duplicates from spawning
        if hasattr(self, 'cat_popup') and self.cat_popup.winfo_exists():
            self.cat_popup.destroy()
            
        self.cat_popup = ctk.CTkToplevel(self)
        self.cat_popup.title(f"{category} Details")
        self.cat_popup.geometry("450x500")
        
        # 🚀 CRITICAL UI FIX: Smooth layering instead of forced topmost
        self.cat_popup.transient(self) # Binds popup to the main OS window
        self.cat_popup.focus_force()   # Brings it to the front naturally
        
        ctk.CTkLabel(self.cat_popup, text=f"📂 {category} Breakdown", font=("Arial", 18, "bold")).pack(pady=10)
        
        conn = get_db_connection()
        try: rows = conn.execute("SELECT ext, COUNT(*), SUM(size) FROM files WHERE cat=? GROUP BY ext ORDER BY SUM(size) DESC", (category,)).fetchall()
        except: rows = []
        finally: conn.close()
        
        scroll_frame = ctk.CTkScrollableFrame(self.cat_popup)
        scroll_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        total_files = total_size = 0
        if not rows:
            ctk.CTkLabel(scroll_frame, text="No files found in this category.").pack(pady=20)
        else:
            for row in rows:
                ext = row[0] if row[0] else "[No Ext]"
                count = row[1]
                sz = row[2] or 0
                total_files += count
                total_size += sz
                
                row_f = ctk.CTkFrame(scroll_frame, fg_color="transparent", cursor="hand2")
                row_f.pack(fill="x", pady=2)
                lbl_ext = ctk.CTkLabel(row_f, text=f"Ext: {ext}", font=("Arial", 13, "bold"), width=100, anchor="w", cursor="hand2")
                lbl_ext.pack(side="left")
                lbl_cnt = ctk.CTkLabel(row_f, text=f"{count} files", font=("Arial", 12), text_color="gray", width=80, anchor="e", cursor="hand2")
                lbl_cnt.pack(side="left", padx=10)
                lbl_sz = ctk.CTkLabel(row_f, text=f"{sz:.2f} MB", font=("Arial", 12, "bold"), text_color="#3498db", width=80, anchor="e", cursor="hand2")
                lbl_sz.pack(side="right")
                
                handler = lambda e, c=category, ex=(row[0] if row[0] else "[No Ext]"): self.show_extension_files(c, ex)
                row_f.bind("<Button-1>", handler); lbl_ext.bind("<Button-1>", handler); lbl_cnt.bind("<Button-1>", handler); lbl_sz.bind("<Button-1>", handler)
                
        ctk.CTkLabel(self.cat_popup, text=f"Total: {total_files} files | {total_size:.2f} MB", font=("Arial", 13, "bold"), text_color="#E67E22").pack(pady=5)
        
        def view_files():
            self.tabs.set("Action Center")
            self.type_filter.set(category)
            self.update_view()
            self.cat_popup.destroy()
            
        ctk.CTkButton(self.cat_popup, text=f"👀 View All {category}", font=("Arial", 14, "bold"), height=40, command=view_files).pack(pady=10, padx=20, fill="x")
    def show_drive_analysis(self, event=None):
        if hasattr(self, 'drive_popup') and self.drive_popup.winfo_exists():
            self.drive_popup.destroy()
            
        self.drive_popup = ctk.CTkToplevel(self)
        self.drive_popup.title("ZenSpace | Advanced Drive Matrix")
        self.drive_popup.geometry("950x650")
        self.drive_popup.attributes('-topmost', True)

        self.drive_popup.update_idletasks()
        x = (self.drive_popup.winfo_screenwidth() // 2) - (950 // 2)
        y = (self.drive_popup.winfo_screenheight() // 2) - (650 // 2)
        self.drive_popup.geometry(f"+{x}+{y}")

        ctk.CTkLabel(self.drive_popup, text="🖥️ Detailed Hardware Matrix", font=("Arial", 24, "bold")).pack(pady=15)
        
        scroll_frame = ctk.CTkScrollableFrame(self.drive_popup, fg_color="transparent")
        scroll_frame.pack(fill="both", expand=True, padx=20, pady=10)
        
        conn = get_db_connection()
        drives = get_safe_drives()
        
        for d in drives:
            try:
                usage = shutil.disk_usage(d)
                total_gb = usage.total / (1024**3)
                used_gb = usage.used / (1024**3)
                free_gb = usage.free / (1024**3)
                perc = used_gb / total_gb if total_gb > 0 else 0
                
                # Create a sleek card for each drive
                card = ctk.CTkFrame(scroll_frame, corner_radius=10, border_width=1)
                card.pack(fill="x", pady=10, ipady=10, ipadx=10)
                
                header = ctk.CTkFrame(card, fg_color="transparent")
                header.pack(fill="x", padx=10, pady=5)
                
                ctk.CTkLabel(header, text=f"Local Disk ({d[:2]})", font=("Arial", 18, "bold"), text_color="#3498db").pack(side="left")
                ctk.CTkLabel(header, text=f"Capacity: {total_gb:.1f} GB | Free Space: {free_gb:.1f} GB", font=("Arial", 14), text_color="gray").pack(side="right")
                
                bar = ctk.CTkProgressBar(card, height=18, fg_color="#e0e0e0", progress_color="#e74c3c" if perc > 0.85 else "#2ecc71")
                bar.set(perc)
                bar.pack(fill="x", padx=10, pady=10)
                
                # Fetch exact file category breakdown for this specific drive
                drive_letter = d[:2]
                rows = conn.execute("SELECT cat, SUM(size), COUNT(*) FROM files WHERE path LIKE ? GROUP BY cat ORDER BY SUM(size) DESC", (f"{drive_letter}\\%",)).fetchall()
                
                if rows:
                    stats_frame = ctk.CTkFrame(card, fg_color="transparent")
                    stats_frame.pack(fill="x", padx=10, pady=5)
                    
                    # Create a neat grid layout for the stats
                    col = 0
                    row_idx = 0
                    for cat, sz, cnt in rows:
                        sz_str = f"{sz/1024:.2f} GB" if sz > 1024 else f"{sz:.1f} MB"
                        lbl = ctk.CTkLabel(stats_frame, text=f"• {cat}: {sz_str} ({cnt:,} files)", font=("Arial", 13))
                        lbl.grid(row=row_idx, column=col, sticky="w", padx=(0, 30), pady=4)
                        col += 1
                        if col > 3: # 4 columns per row
                            col = 0
                            row_idx += 1
            except Exception:
                pass
        conn.close()
        
        ctk.CTkButton(self.drive_popup, text="Close Matrix", font=("Arial", 13, "bold"), width=200, height=40, command=self.drive_popup.destroy).pack(pady=15)

    def create_stat_row(self, label, val, cat_name):
        f = ctk.CTkFrame(self.stats_frame, fg_color="transparent", cursor="hand2")
        f.pack(fill="x", pady=1)
        l = ctk.CTkLabel(f, text=label, text_color="gray", font=("Arial", 11), cursor="hand2")
        l.pack(side="left")
        v = ctk.CTkLabel(f, text=val, font=("Arial", 11, "bold"), cursor="hand2")
        v.pack(side="right")
        
        def on_click(e): self.show_category_breakdown(cat_name)
        f.bind("<Button-1>", on_click)
        l.bind("<Button-1>", on_click)
        v.bind("<Button-1>", on_click)
        return v

    def toggle_theme(self):
        if self.appearance_mode == "Dark":
            self.appearance_mode = "Light"
            ctk.set_appearance_mode("Light")
            self.style.configure("Treeview", background="#ecf0f1", foreground="black", fieldbackground="#ecf0f1")
            self.style.configure("Treeview.Heading", background="#bdc3c7", foreground="black")
            self.omni_log.configure(fg_color="#ecf0f1", text_color="black") 
        else:
            self.appearance_mode = "Dark"
            ctk.set_appearance_mode("Dark")
            self.style.configure("Treeview", background="#2b2b2b", foreground="white", fieldbackground="#2b2b2b")
            self.style.configure("Treeview.Heading", background="#1f1f1f", foreground="white")
            self.omni_log.configure(fg_color="#0D1117", text_color="white") 

    def track_api_usage(self):
        now = time.time()
        if now - self.minute_tracker_start >= 60:
            self.api_calls_this_minute = 0
            self.minute_tracker_start = now
            
        self.api_calls_this_minute += 1
        self.api_calls_today += 1
        
        rpm_left = max(0, self.MAX_RPM - self.api_calls_this_minute)
        rpd_left = max(0, self.MAX_RPD - self.api_calls_today)
        
        color_min = "#2ecc71" if rpm_left > 5 else "#e74c3c"
        color_day = "#2ecc71" if rpd_left > 100 else "#e74c3c"
        
        self.after(0, lambda: self.api_rpm_lbl.configure(text=f"Tasks Left (Min): {rpm_left}", text_color=color_min))
        self.after(0, lambda: self.api_rpd_lbl.configure(text=f"Tasks Left (Day): {rpd_left:,}", text_color=color_day))

    # --- UI COMPONENTS ---
    def setup_action_tab(self):
        ctrl = ctk.CTkFrame(self.tab_action, fg_color="transparent")
        ctrl.pack(fill="x", pady=5, padx=5)
        
        ctk.CTkButton(ctrl, text="Select All", width=80, font=("Arial", 12, "bold"), command=self.check_all).pack(side="left", padx=2)
        ctk.CTkButton(ctrl, text="Deselect", width=80, font=("Arial", 12, "bold"), fg_color="#34495E", command=self.uncheck_all).pack(side="left", padx=2)
        
        self.type_filter = ctk.CTkComboBox(ctrl, values=CATEGORIES, width=120, command=lambda _: self.update_view())
        self.type_filter.set("All Types")
        self.type_filter.pack(side="left", padx=5)

        self.sort_filter = ctk.CTkComboBox(ctrl, values=["None", "Date (New)", "Date (Old)", "Size (High)", "Size (Low)"], width=120, command=lambda _: self.update_view())
        self.sort_filter.set("None")
        self.sort_filter.pack(side="left", padx=5)

        self.search_bar = ctk.CTkEntry(ctrl, placeholder_text="Search active files...", width=250)
        self.search_bar.pack(side="right", padx=10)
        self.search_bar.bind("<KeyRelease>", lambda e: self.update_view())

        tree_frame = ctk.CTkFrame(self.tab_action, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=5, pady=5)
        
        scroll_y = ttk.Scrollbar(tree_frame, orient="vertical")
        scroll_y.pack(side="right", fill="y")

        self.tree = ttk.Treeview(tree_frame, columns=("Index", "Check", "Name", "Size", "Cat", "Date", "Path"), show='headings', yscrollcommand=scroll_y.set)
        
        col_widths = {"Index": 50, "Check": 50, "Name": 400, "Size": 100, "Cat": 120, "Date": 120, "Path": 500}
        col_anchors = {"Index": "center", "Check": "center", "Name": "w", "Size": "center", "Cat": "center", "Date": "center", "Path": "w"}
        
        for col in col_widths.keys(): 
            self.tree.heading(col, text=col)
            self.tree.column(col, width=col_widths[col], anchor=col_anchors[col], stretch=(col=="Path"))
            
        self.tree.pack(side="left", fill="both", expand=True)
        scroll_y.config(command=self.tree.yview)
        
        self.tree.bind('<ButtonRelease-1>', self.toggle_check)
        self.tree.bind('<Double-1>', self.on_double_click)
        self.tree.bind('<Button-3>', self.show_context_menu)
        
        self.action_bottom_frame = ctk.CTkFrame(self.tab_action, fg_color="transparent")
        self.action_bottom_frame.pack(fill="x", side="bottom", pady=5, padx=5)
        
        self.manual_action_frame = ctk.CTkFrame(self.action_bottom_frame, fg_color="transparent")
        self.manual_action_frame.pack(side="left", fill="x")
        
        ctk.CTkLabel(self.manual_action_frame, text="Manual Actions:", font=("Arial", 12, "bold"), text_color="gray").pack(side="left", padx=(0, 10))
        ctk.CTkButton(self.manual_action_frame, text="♻️ Trash", width=80, fg_color="#d35400", font=("Arial", 12, "bold"), command=lambda: self.trigger_manual_action("RECYCLE")).pack(side="left", padx=2)
        ctk.CTkButton(self.manual_action_frame, text="🔥 Delete", width=80, fg_color="#c0392b", font=("Arial", 12, "bold"), command=lambda: self.trigger_manual_action("DELETE")).pack(side="left", padx=2)
        ctk.CTkButton(self.manual_action_frame, text="📁 Move", width=80, fg_color="#27ae60", font=("Arial", 12, "bold"), command=lambda: self.trigger_manual_action("MOVE")).pack(side="left", padx=2)
        ctk.CTkButton(self.manual_action_frame, text="📦 Zip", width=80, fg_color="#8e44ad", font=("Arial", 12, "bold"), command=lambda: self.trigger_manual_action("ZIP")).pack(side="left", padx=2)

        self.confirm_action_frame = ctk.CTkFrame(self.action_bottom_frame, fg_color="transparent")
        self.btn_confirm_action = ctk.CTkButton(self.confirm_action_frame, text="✅ Confirm", font=("Arial", 13, "bold"), height=40, command=self.execute_verified_action)
        self.btn_cancel_action = ctk.CTkButton(self.confirm_action_frame, text="❌ Cancel", fg_color="#7f8c8d", font=("Arial", 13, "bold"), height=40, command=self.cancel_action)
        
        self.cancel_action()

    def setup_drive_tab(self):
        ctrl = ctk.CTkFrame(self.tab_drive, fg_color="transparent")
        ctrl.pack(fill="x", pady=10, padx=10)
        ctk.CTkButton(ctrl, text="⬆ Up", width=60, font=("Arial", 13, "bold"), command=self.go_up_dir).pack(side="left", padx=5)
        drives = get_safe_drives() 
        self.drive_selector = ctk.CTkComboBox(ctrl, values=drives, width=80, command=self.load_directory)
        self.drive_selector.pack(side="left", padx=5)
        self.drive_path_entry = ctk.CTkEntry(ctrl, placeholder_text="Path...")
        self.drive_path_entry.pack(side="left", fill="x", expand=True, padx=5)
        self.drive_path_entry.bind("<Return>", lambda e: self.load_directory(self.drive_path_entry.get()))
        ctk.CTkButton(ctrl, text="Go", width=60, font=("Arial", 13, "bold"), command=lambda: self.load_directory(self.drive_path_entry.get())).pack(side="left", padx=5)

        tree_frame = ctk.CTkFrame(self.tab_drive, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=10, pady=10)
        scroll_y = ttk.Scrollbar(tree_frame, orient="vertical")
        scroll_y.pack(side="right", fill="y")
        
        self.drive_tree = ttk.Treeview(tree_frame, columns=("Index", "Name", "Type", "Size", "Modified", "Path"), show='headings', yscrollcommand=scroll_y.set)
        self.drive_tree.heading("Index", text="#")
        self.drive_tree.column("Index", width=50, anchor="center")
        
        drive_widths = {"Name": 450, "Type": 120, "Size": 100, "Modified": 150, "Path": 0}
        for col, width in drive_widths.items():
            self.drive_tree.heading(col, text=col)
            self.drive_tree.column(col, width=width, anchor="w" if col == "Name" else "center", stretch=(col=="Name"))
            
        self.drive_tree.pack(side="left", fill="both", expand=True)
        scroll_y.config(command=self.drive_tree.yview)
        self.drive_tree.bind('<Double-1>', self.on_drive_double_click)
        self.drive_tree.bind('<Button-3>', self.show_context_menu)
        if drives: self.load_directory(drives[0])

    def setup_trash_tab(self):
        ctrl = ctk.CTkFrame(self.tab_trash, fg_color="transparent")
        ctrl.pack(fill="x", pady=5, padx=5)
        
        ctk.CTkButton(ctrl, text="Select All", width=80, font=("Arial", 12, "bold"), command=self.check_all_trash).pack(side="left", padx=2)
        ctk.CTkButton(ctrl, text="Deselect", width=80, font=("Arial", 12, "bold"), fg_color="#34495E", command=self.uncheck_all_trash).pack(side="left", padx=2)

        self.type_filter_trash = ctk.CTkComboBox(ctrl, values=CATEGORIES, width=120, command=lambda _: self.update_trash_view(silent=True))
        self.type_filter_trash.set("All Types")
        self.type_filter_trash.pack(side="left", padx=5)

        self.sort_filter_trash = ctk.CTkComboBox(ctrl, values=["None", "Deleted (New)", "Deleted (Old)", "Size (High)", "Size (Low)"], width=130, command=lambda _: self.update_trash_view(silent=True))
        self.sort_filter_trash.set("Deleted (New)")
        self.sort_filter_trash.pack(side="left", padx=5)

        self.search_bar_trash = ctk.CTkEntry(ctrl, placeholder_text="Search OS Recycle Bin...", width=250)
        self.search_bar_trash.pack(side="right", padx=10)
        self.search_bar_trash.bind("<KeyRelease>", lambda e: self.update_trash_view(silent=True))
        
        self.btn_sync_trash = ctk.CTkButton(ctrl, text="🔄 Sync OS", width=80, font=("Arial", 12, "bold"), fg_color="#8e44ad", command=lambda: self.update_trash_view(silent=False))
        self.btn_sync_trash.pack(side="right", padx=5)
        
        tree_frame = ctk.CTkFrame(self.tab_trash, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=5, pady=5)
        scroll_y = ttk.Scrollbar(tree_frame, orient="vertical")
        scroll_y.pack(side="right", fill="y")
        
        self.trash_tree = ttk.Treeview(tree_frame, columns=("Check", "Name", "Size", "Cat", "Original Path", "Deleted On", "TrashPath"), show='headings', yscrollcommand=scroll_y.set)
        self.trash_tree.heading("Check", text="✓")
        self.trash_tree.column("Check", width=50, anchor="center")
        
        trash_cols = {"Name": 350, "Size": 100, "Cat": 120, "Original Path": 450, "Deleted On": 150, "TrashPath": 0}
        for col, width in trash_cols.items():
            self.trash_tree.heading(col, text=col)
            self.trash_tree.column(col, width=width, anchor="w" if col in ["Name", "Original Path"] else "center", stretch=(col=="Original Path"))
            
        self.trash_tree.pack(side="left", fill="both", expand=True)
        scroll_y.config(command=self.trash_tree.yview)
        self.trash_tree.bind('<ButtonRelease-1>', self.toggle_trash_check)
        
        bottom_frame = ctk.CTkFrame(self.tab_trash, fg_color="transparent")
        bottom_frame.pack(fill="x", side="bottom", pady=10, padx=10)
        
        ctk.CTkButton(bottom_frame, text="☢️ Empty OS Recycle Bin", fg_color="#c0392b", font=("Arial", 13, "bold"), height=40, command=self.empty_zen_trash).pack(side="left", padx=5)
        
        self.btn_delete_trash = ctk.CTkButton(bottom_frame, text="🔥 Delete Selected", fg_color="#e67e22", font=("Arial", 13, "bold"), height=40, command=self.delete_selected_trash)
        self.btn_delete_trash.pack(side="right", padx=5)
        self.btn_restore_trash = ctk.CTkButton(bottom_frame, text="♻️ Native Restore Selected", fg_color="#27ae60", font=("Arial", 13, "bold"), height=40, command=self.restore_selected_trash)
        self.btn_restore_trash.pack(side="right", padx=5)

    def setup_omni_tab(self):
        self.omni_log = ctk.CTkTextbox(self.tab_omni, state="disabled", font=("Arial", 14), fg_color="#ecf0f1", text_color="black")
        self.omni_log.pack(fill="both", expand=True, padx=10, pady=10)
        input_frame = ctk.CTkFrame(self.tab_omni, fg_color="transparent")
        input_frame.pack(fill="x", padx=10, pady=10)
        
        self.omni_entry = ctk.CTkEntry(input_frame, placeholder_text="Ask Omni to move python files, organize, zip, or chat...", font=("Arial", 14), height=40)
        self.omni_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.omni_entry.bind("<Return>", lambda e: self.run_ai())
        
        self.btn_clear_chat = ctk.CTkButton(input_frame, text="🗑️ Clear Memory", fg_color="#c0392b", font=("Arial", 12, "bold"), height=40, width=100, command=self.clear_chat_history)
        self.btn_clear_chat.pack(side="right")
        self.btn_suggest = ctk.CTkButton(input_frame, text="✨ Analyze", fg_color="#8e44ad", font=("Arial", 12, "bold"), height=40, width=90, command=self.get_ai_suggestions)
        self.btn_suggest.pack(side="right", padx=(0, 10))

    # --- BACKGROUND SERVICE & TRAY ---
    def create_tray_image(self):
        image = Image.new('RGB', (64, 64), color = (41, 128, 185))
        dc = ImageDraw.Draw(image)
        dc.rectangle([16, 16, 48, 48], fill=(255, 255, 255))
        return image

    def minimize_to_tray(self):
        self.withdraw() 
        image = self.create_tray_image()
        menu = pystray.Menu(
            pystray.MenuItem('Open ZenSpace', self.restore_from_tray),
            pystray.MenuItem('Quit Completely', self.quit_app)
        )
        self.tray_icon = pystray.Icon("ZenSpace", image, "ZenSpace Omni", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def restore_from_tray(self, icon, item):
        self.tray_icon.stop()
        self.after(0, self.deiconify)
        self.after(0, self.update_view) 

    def quit_app(self, icon, item):
        self.radar_observer.stop()
        self.tray_icon.stop()
        self.destroy()

    # --- CORE LOGIC ---
    # --- CORE SCANNING LOGIC ---
    def start_scan(self):
        self.btn_scan.configure(state="disabled", text="Scanning...")
        self.tabs.set("Action Center")
        conn = get_db_connection()
        
        # 🛡️ THE HASH VAULT: Backup all duplicate calculations BEFORE wiping the table!
        conn.execute('CREATE TABLE IF NOT EXISTS hash_vault (path TEXT UNIQUE, deep_hash TEXT)')
        try: 
            conn.execute('INSERT OR REPLACE INTO hash_vault SELECT path, deep_hash FROM files WHERE deep_hash IS NOT NULL')
        except: 
            pass

        conn.execute('DROP TABLE IF EXISTS files')
        conn.execute('CREATE TABLE files (name TEXT, path TEXT UNIQUE, ext TEXT, size REAL, cat TEXT, mtime REAL, fast_hash TEXT, deep_hash TEXT)')
        conn.commit()
        conn.close()
        
        for i in self.tree.get_children(): self.tree.delete(i)
        
        self.is_scanning = True
        threading.Thread(target=self.run_scanner_thread, daemon=True).start()

    def scan_done(self):
        conn = get_db_connection()
        # 🛡️ RESTORE MEMORY: Instantly inject all the saved hashes back into the live system
        try:
            conn.execute('UPDATE files SET deep_hash = (SELECT deep_hash FROM hash_vault WHERE hash_vault.path = files.path)')
            conn.commit()
        except: pass
        finally: conn.close()

        self.current_sql_filter = "1=1"
        self.update_sidebar_stats()
        self.update_view() 
        self.btn_scan.configure(state="normal", text="🔍 Map Storage")
        self.status_lbl.configure(text="System Idle")
        self.is_scanning = False
        gc.collect() 
        self.after(500, self.trigger_lightning_dupes)

    def run_scanner_thread(self):
        scanner = LiveHybridScanner(lambda t: self.after(0, lambda: self.status_lbl.configure(text=t)))
        drives = get_safe_drives() 
        temp_dir = os.environ.get('TEMP')
        if temp_dir and os.path.exists(temp_dir) and temp_dir not in drives: drives.append(temp_dir)
        with ThreadPoolExecutor(max_workers=6) as executor: executor.map(scanner.scan_path, drives)
        self.after(0, self.scan_done)

    def silent_refresh(self):
        if not self.pending_action_type:
            checked_files = [i for i in self.tree.get_children() if self.tree.item(i, "values")[1] == "☑"]
            checked_trash = [i for i in self.trash_tree.get_children() if self.trash_tree.item(i, "values")[0] == "☑"]
            if not checked_files: self.update_view()
            if not checked_trash: self.update_trash_view(silent=True) 
            self.update_sidebar_stats()

    def update_sidebar_stats(self):
        try:
            total_bytes, free_bytes = 0, 0
            for d in get_safe_drives():
                usage = shutil.disk_usage(d)
                total_bytes += usage.total; free_bytes += usage.free
            total_gb = total_bytes / (1024**3); free_gb = free_bytes / (1024**3)
            if total_gb > 0:
                self.storage_bar.set((total_gb - free_gb) / total_gb)
                self.usage_lbl.configure(text=f"Used: {total_gb - free_gb:.1f} GB / {total_gb:.1f} GB")
                self.free_lbl.configure(text=f"Free: {free_gb:.1f} GB Left")
        except: pass

        conn = get_db_connection()
        try:
            total_n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            self.total_files_lbl.configure(text=f"Total Files Mapped: {total_n:,}")
            
            # 🆕 Fetch all 8 categories from the database at once (much faster)
            rows = conn.execute("SELECT cat, SUM(size), COUNT(*) FROM files GROUP BY cat").fetchall()
            stats_dict = {cat: (sz or 0, cnt or 0) for cat, sz, cnt in rows}
            
            # 🆕 Update every single label in the sidebar dynamically
            if hasattr(self, 'cat_labels'):
                for cat, lbl in self.cat_labels.items():
                    sz, cnt = stats_dict.get(cat, (0, 0))
                    # Auto-formats to GB if the size is over 1024 MB
                    sz_str = f"{sz/1024:.2f} GB" if sz > 1024 else f"{sz:.1f} MB"
                    lbl.configure(text=f"{sz_str} ({cnt:,})")
        except: pass
        conn.close()

    def update_view(self):
        for i in self.tree.get_children(): self.tree.delete(i)
        search_text = self.search_bar.get().lower().replace("'", "''")
        sel_type = self.type_filter.get()
        sort_mode = self.sort_filter.get()
        base_filter = self.current_sql_filter if self.current_sql_filter else "1=1"
        sql_query = f"SELECT * FROM (SELECT * FROM files WHERE {base_filter}) WHERE 1=1"
        if search_text: sql_query += f" AND (LOWER(name) LIKE '%{search_text}%' OR LOWER(path) LIKE '%{search_text}%')"
        if sel_type != "All Types": sql_query += f" AND cat = '{sel_type}'"
        if sort_mode == "Size (High)": sql_query += " ORDER BY size DESC"
        elif sort_mode == "Size (Low)": sql_query += " ORDER BY size ASC"
        elif sort_mode == "Date (New)": sql_query += " ORDER BY mtime DESC"
        elif sort_mode == "Date (Old)": sql_query += " ORDER BY mtime ASC"
        sql_query += " LIMIT 1000"

        conn = get_db_connection()
        try:
            for idx, r in enumerate(conn.execute(sql_query).fetchall(), 1):
                dt = time.strftime('%d/%m/%y', time.localtime(r[5]))
                
                # 🆕 Check RAM to see if this specific file was checked!
                check_state = "☑" if r[1] in self.checked_paths else "☐"
                
                self.tree.insert("", "end", values=(idx, check_state, r[0], f"{r[3]:.1f} MB", r[4], dt, r[1]))
        except: pass
        conn.close()

    def reset_filters(self):
        self.search_bar.delete(0, 'end')
        self.type_filter.set("All Types")
        self.sort_filter.set("None")
        self.current_sql_filter = "1=1"
        self.tabs.set("Action Center")
        self.update_view()
        self.search_bar_trash.delete(0, 'end')
        self.type_filter_trash.set("All Types")
        self.sort_filter_trash.set("None")
        self.current_trash_sql_filter = "1=1"
        self.update_trash_view(silent=False) 
        self.cancel_action()
        
        # 🚀 NEW: Trigger the Ghost Sweeper to double-check everything is accurate
        self.trigger_ghost_sweeper()

    def ask_date_filter(self):
        dialog = ctk.CTkInputDialog(text="Show files from the last X days:\n(Enter a number)", title="Filter by Date")
        res = dialog.get_input()
        if res:
            try:
                if int(res) > 0: self.apply_task(f"mtime > {time.time() - (int(res) * 86400)}")
            except: messagebox.showerror("Error", "Invalid number.")

    # --- ⚡ THE INSTANT BACKGROUND DUPLICATE ENGINE ---
    # --- ⚡ THE VISUAL & INSTANT DUPLICATE ENGINE ---
    
    # --- ⚡ THE ADVANCED VISUAL DUPLICATE ENGINE ---
    # --- ⚡ THE ADVANCED VISUAL DUPLICATE ENGINE ---
    def trigger_lightning_dupes(self):
        # 🆕 STATE 2: If dupes are already cached and waiting, just show them!
        if getattr(self, 'pending_dupe_groups', None):
            self.btn_dupes.configure(fg_color=["#3B8ED0", "#1F6AA5"]) # Reset to default blue
            self.prepare_duplicate_action(self.pending_dupe_groups, getattr(self, 'pending_dupe_elapsed', 0))
            self.pending_dupe_groups = None # Clear memory
            self.btn_dupes.configure(text="⚡ Lightning Dupes")
            return

        # 🆕 STATE 1: Start the analysis
        self.btn_dupes.configure(state="disabled", text="Analyzing...")
        
        self.dupe_popup = ctk.CTkToplevel(self)
        self.dupe_popup.title("⚡ Deep File Analysis")
        self.dupe_popup.geometry("500x250")
        self.dupe_popup.attributes('-topmost', False)
        self.dupe_popup.lower() # Pushes behind main window so it doesn't interrupt you
        self.dupe_popup.protocol("WM_DELETE_WINDOW", lambda: None) 
        
        self.dupe_popup.update_idletasks()
        x = (self.dupe_popup.winfo_screenwidth() // 2) - (500 // 2)
        y = (self.dupe_popup.winfo_screenheight() // 2) - (250 // 2)
        self.dupe_popup.geometry(f"+{x}+{y}")
        
        ctk.CTkLabel(self.dupe_popup, text="Scanning Storage Matrix...", font=("Arial", 18, "bold")).pack(pady=(20, 10))
        self.dupe_progress = ctk.CTkProgressBar(self.dupe_popup, width=400, height=15, fg_color="#1a1a1a", progress_color="#e67e22")
        self.dupe_progress.set(0)
        self.dupe_progress.pack(pady=10)
        
        self.dupe_status_lbl = ctk.CTkLabel(self.dupe_popup, text="Calculating suspicious files...", font=("Arial", 12))
        self.dupe_status_lbl.pack(pady=5)
        self.dupe_file_lbl = ctk.CTkLabel(self.dupe_popup, text="...", font=("Arial", 10), text_color="gray", wraplength=450)
        self.dupe_file_lbl.pack(pady=5)
        
        threading.Thread(target=self._run_lightning_dupes_thread, daemon=True).start()

    def _run_lightning_dupes_thread(self):
        t0 = time.time()
        c = get_db_connection()
        
        q = "SELECT path FROM files WHERE deep_hash IS NULL AND size > 0 AND size IN (SELECT size FROM files GROUP BY size HAVING COUNT(*) > 1)"
        pend = [r[0] for r in c.execute(q).fetchall()]
        tot = len(pend)
        last_ui = 0

        if tot > 0:
            for i, p in enumerate(pend):
                while self.ai_processing: time.sleep(0.5)
                
                now = time.time()
                if now - last_ui > 0.1:
                    pct = (i / tot)
                    sn = os.path.basename(p)
                    if len(sn) > 50: sn = sn[:47] + "..."
                    self.after(0, lambda pc=pct, c_id=i+1, t=tot, n=sn: self._update_dupe_ui(pc, c_id, t, n))
                    last_ui = now
                
                if not os.path.exists(p):
                    c.execute("DELETE FROM files WHERE path=?", (p,))
                    continue

                sh = get_smart_hash(p)
                c.execute("UPDATE files SET deep_hash=? WHERE path=?", (sh, p))
                if i % 10 == 0: c.commit()
            c.commit()

        self.after(0, lambda: self.dupe_status_lbl.configure(text="Finalizing matches..."))
        
        q2 = "SELECT path, deep_hash FROM files WHERE deep_hash IS NOT NULL AND deep_hash IN (SELECT deep_hash FROM files GROUP BY deep_hash HAVING COUNT(*) > 1)"
        rows = c.execute(q2).fetchall()
        c.close()

        grps = {}
        for p, dh in rows: grps.setdefault(dh, []).append(p)
        dupes = list(grps.values())
        elapsed = time.time() - t0

        self.after(0, self.dupe_popup.destroy)

        if not dupes:
            self.after(0, lambda: self.log_to(self.omni_log, f"🤖 Omni: Scan complete in {elapsed:.1f}s. Your system is completely free of identical files!"))
            self.after(0, lambda: self.btn_dupes.configure(state="normal", text="⚡ Lightning Dupes"))
            return

        cc = sum(len(g) - 1 for g in dupes)
        self.pending_dupe_groups = dupes
        self.pending_dupe_elapsed = elapsed

        self.after(0, lambda: self.log_to(self.omni_log, f"🤖 Omni: Finished analyzing! Found {cc} duplicate files hidden in your system. Click the orange 'View Dupes' button to review them."))
        self.after(0, lambda: self.btn_dupes.configure(state="normal", text=f"👀 View Dupes ({cc})", fg_color="#e67e22"))

    def _update_dupe_ui(self, percentage, current, total, filename):
        self.dupe_progress.set(percentage)
        self.dupe_status_lbl.configure(text=f"Deep Scanning: {current} / {total} files")
        self.dupe_file_lbl.configure(text=f"Reading: {filename}")

    """e copies side-by-side in the table. Originals are UNCHECKED to protect them. Click 'Confirm' to send the checked copies to the OS Recycle Bin.") # type: ignore # pyright: ignore[reportUndefinedVariable] # type: ignore"""
    def prepare_duplicate_action(self, duplicate_groups, elapsed):
        self.pending_action_type = "RECYCLE"
        self.pending_target_folder = None
        self.checked_paths.clear() # 🆕 Reset persistent memory
        
        all_paths, originals, copies_count = [], set(), 0
        
        for group in duplicate_groups:
            group.sort(key=len)
            originals.add(group[0])
            all_paths.extend(group)
            copies_count += (len(group) - 1)
            
            # 🆕 Memorize the copies so filters don't wipe them!
            for copy_path in group[1:]:
                self.checked_paths.add(copy_path)
            
        paths_sql = "('" + "','".join(p.replace("'", "''") for p in all_paths) + "')"
        self.current_sql_filter = f"path IN {paths_sql}"
        self.tabs.set("Action Center")
        self.sort_filter.set("Size (High)") 
        
        # 🆕 Because we updated the memory above, this single line 
        # instantly draws the list and perfectly checks the duplicate files!
        self.update_view()
        
        if hasattr(self, 'manual_action_frame'): self.manual_action_frame.pack_forget()
        if hasattr(self, 'confirm_action_frame'): self.confirm_action_frame.pack(side="right", fill="x")
        self.update_action_button()
        self.btn_confirm_action.pack(side="right", padx=5)
        self.btn_cancel_action.pack(side="right", padx=5)
        
        self.log_to(self.omni_log, f"🤖 Omni: Active scan complete in {elapsed:.2f}s! Found {copies_count} identical duplicates.")
        self.log_to(self.omni_log, f"⏸️ HUMAN AUTHENTICATION: I have shown BOTH the originals and the copies side-by-side in the table. Originals are UNCHECKED to protect them. Click 'Confirm' to send the checked copies to the OS Recycle Bin.")

    def start_silent_hunter(self):
        threading.Thread(target=self.silent_duplicate_hunter, daemon=True).start()

    def silent_duplicate_hunter(self):
        """Runs forever in the background, indexing new file hashes while the PC is idle."""
        while True:
            time.sleep(3) 
            if self.ai_processing or getattr(self, 'is_scanning', False): continue 
            conn = get_db_connection()
            try:
                unhashed = conn.execute("""SELECT path FROM files WHERE deep_hash IS NULL AND size > 0 AND size IN (SELECT size FROM files GROUP BY size HAVING COUNT(*) > 1) LIMIT 10""").fetchall()
                if unhashed:
                    for (path,) in unhashed:
                        if not os.path.exists(path):
                            conn.execute("DELETE FROM files WHERE path=?", (path,))
                            continue
                        # Use the new ultra-fast smart hash
                        smart_hash = get_smart_hash(path)
                        conn.execute("UPDATE files SET deep_hash=? WHERE path=?", (smart_hash, path))
                    conn.commit()
            except: pass
            finally: conn.close()
        

    # --- ADVANCED OS RECYCLE BIN SYNC ---
    def update_trash_view(self, silent=False):
        if not HAS_WIN_TRASH: return
        conn = get_db_connection()
        if not silent:
            try:
                conn.execute("DELETE FROM trash") 
                shell = win32com.client.Dispatch("Shell.Application")
                trash = shell.NameSpace(10)
                for item in trash.Items():
                    name = trash.GetDetailsOf(item, 0) or "Unknown"
                    orig_dir = trash.GetDetailsOf(item, 1)
                    full_orig = os.path.join(orig_dir, name) if orig_dir else name
                    try: sz_mb = os.path.getsize(item.Path) / (1024*1024)
                    except: sz_mb = 0.0
                    try: mtime = os.path.getctime(item.Path)
                    except: mtime = time.time()
                    conn.execute("INSERT INTO trash (name, trash_path, original_path, size, deleted_at, cat) VALUES (?,?,?,?,?,?)", (name, item.Path, full_orig, sz_mb, mtime, "Others"))
                conn.commit()
                gc.collect()
            except: pass

        for i in self.trash_tree.get_children(): self.trash_tree.delete(i)
        search_text = self.search_bar_trash.get().lower().replace("'", "''")
        sel_type = self.type_filter_trash.get()
        sort_mode = self.sort_filter_trash.get()
        base_filter = self.current_trash_sql_filter if hasattr(self, 'current_trash_sql_filter') and self.current_trash_sql_filter else "1=1"
        sql_query = f"SELECT name, size, original_path, deleted_at, trash_path, cat FROM (SELECT * FROM trash WHERE {base_filter}) WHERE 1=1"
        if search_text: sql_query += f" AND (LOWER(name) LIKE '%{search_text}%' OR LOWER(original_path) LIKE '%{search_text}%')"
        if sel_type != "All Types": sql_query += f" AND cat = '{sel_type}'"
        if sort_mode == "Size (High)": sql_query += " ORDER BY size DESC"
        elif sort_mode == "Size (Low)": sql_query += " ORDER BY size ASC"
        elif sort_mode == "Deleted (New)": sql_query += " ORDER BY deleted_at DESC"
        elif sort_mode == "Deleted (Old)": sql_query += " ORDER BY deleted_at ASC"
        sql_query += " LIMIT 1000" 

        try:
            for r in conn.execute(sql_query).fetchall():
                dt = time.strftime('%d/%m/%y %H:%M', time.localtime(r[3]))
                self.trash_tree.insert("", "end", values=("☐", r[0], f"{r[1]:.1f} MB", r[5], r[2], dt, r[4]))
        except: pass
        conn.close()

    def toggle_trash_check(self, event):
        if self.trash_tree.identify_region(event.x, event.y) == "cell" and self.trash_tree.identify_column(event.x) == '#1': 
            item = self.trash_tree.identify_row(event.y)
            vals = list(self.trash_tree.item(item, "values"))
            vals[0] = "☐" if vals[0] == "☑" else "☑" 
            self.trash_tree.item(item, values=vals)

    def check_all_trash(self):
        for i in self.trash_tree.get_children():
            v = list(self.trash_tree.item(i, 'values')); v[0] = "☑"
            self.trash_tree.item(i, values=v)

    def uncheck_all_trash(self):
        for i in self.trash_tree.get_children():
            v = list(self.trash_tree.item(i, 'values')); v[0] = "☐"
            self.trash_tree.item(i, values=v)

    def restore_selected_trash(self):
        if not HAS_WIN_TRASH: return
        items = [i for i in self.trash_tree.get_children() if self.trash_tree.item(i, "values")[0] == "☑"]
        if not items: return
        restored, total_tasks = 0, len(items)
        try:
            shell = win32com.client.Dispatch("Shell.Application")
            trash = shell.NameSpace(10)
            trash_items = trash.Items()
            for idx, i in enumerate(items):
                self.btn_restore_trash.configure(text=f"♻️ Restoring... {total_tasks - idx} left")
                self.update()
                target_path = self.trash_tree.item(i, "values")[6]
                for obj in trash_items:
                    if obj.Path == target_path:
                        try: obj.InvokeVerb("undelete"); restored += 1
                        except:
                            for verb in obj.Verbs():
                                name = verb.Name.replace("&", "").lower()
                                if "restore" in name or "undelete" in name:
                                    try: verb.DoIt(); restored += 1
                                    except: pass
                                    break
                        break
            gc.collect()
        except Exception as e: messagebox.showerror("Error", f"Failed to restore: {e}")
        self.btn_restore_trash.configure(text="♻️ Native Restore Selected") 
        messagebox.showinfo("Success", f"Natively Restored {restored} files!")
        self.update_view(); self.update_sidebar_stats(); self.update_trash_view(silent=False) 

    def delete_selected_trash(self):
        items = [i for i in self.trash_tree.get_children() if self.trash_tree.item(i, "values")[0] == "☑"]
        if not items: return
        if messagebox.askyesno("Confirm", "Permanently delete from OS Recycle Bin?"):
            total_tasks = len(items)
            for idx, i in enumerate(items):
                self.btn_delete_trash.configure(text=f"🔥 Deleting... {total_tasks - idx} left")
                self.update()
                trash_path = self.trash_tree.item(i, "values")[6]
                try:
                    os.remove(trash_path)
                    dir_name, base_name = os.path.dirname(trash_path), os.path.basename(trash_path)
                    if base_name.startswith("$R"):
                        i_path = os.path.join(dir_name, "$I" + base_name[2:])
                        if os.path.exists(i_path): os.remove(i_path)
                except: pass
            self.btn_delete_trash.configure(text="🔥 Delete Selected") 
            self.update_view(); self.update_sidebar_stats(); self.update_trash_view(silent=False)

    def empty_zen_trash(self):
        if not HAS_WIN_TRASH: return
        if messagebox.askyesno("Empty Recycle Bin", "Permanently empty ENTIRE Windows OS Recycle Bin?"):
            try: win32api.SHEmptyRecycleBin(0, None, 7)
            except: pass
            finally: self.update_view(); self.update_sidebar_stats(); self.update_trash_view(silent=False)

    # --- DRIVE TAB LOGIC ---
    def load_directory(self, path):
        if not os.path.exists(path): return
        self.current_drive_path = path
        self.drive_path_entry.delete(0, 'end')
        self.drive_path_entry.insert(0, path)
        for i in self.drive_tree.get_children(): self.drive_tree.delete(i)
        try:
            folders, files = [], []
            for item in os.scandir(path):
                try:
                    stat = item.stat()
                    mtime = time.strftime('%d/%m/%y %H:%M', time.localtime(stat.st_mtime))
                    if item.is_dir(): folders.append(("📁 " + item.name, "Folder", "", mtime, item.path))
                    else: files.append(("📄 " + item.name, os.path.splitext(item.name)[1].upper() or "File", f"{stat.st_size/1024/1024:.2f} MB", mtime, item.path))
                except: pass
            folders.sort(key=lambda x: x[0].lower()); files.sort(key=lambda x: x[0].lower())
            for idx, f in enumerate(folders + files, 1): 
                self.drive_tree.insert("", "end", values=(idx, f[0], f[1], f[2], f[3], f[4]))
        except PermissionError: 
            messagebox.showwarning("Access Denied", "System folder locked by Windows.")
            self.go_up_dir()

    def go_up_dir(self):
        parent = os.path.dirname(self.current_drive_path)
        if parent and parent != self.current_drive_path: self.load_directory(parent)

    def on_drive_double_click(self, event):
        item = self.drive_tree.identify_row(event.y)
        if item:
            vals = self.drive_tree.item(item, "values")
            if vals[2] == "Folder": self.load_directory(vals[5])
            else: self.open_native_file(vals[5])

    # --- FILE CHECKING FUNCTIONS ---
    
    # --- FILE CHECKING FUNCTIONS ---
    def toggle_check(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell" and self.tree.identify_column(event.x) == '#2': 
            item = self.tree.identify_row(event.y)
            vals = list(self.tree.item(item, "values"))
            path = vals[6]
            
            # 🆕 Toggle the visual AND the memory
            if vals[1] == "☑":
                vals[1] = "☐"
                self.checked_paths.discard(path)
            else:
                vals[1] = "☑"
                self.checked_paths.add(path)
                
            self.tree.item(item, values=vals)
            self.update_action_button()

    def check_all(self):
        for i in self.tree.get_children():
            v = list(self.tree.item(i, 'values'))
            v[1] = "☑"
            self.checked_paths.add(v[6]) # 🆕 Save to memory
            self.tree.item(i, values=v)
        self.update_action_button()

    def uncheck_all(self):
        self.checked_paths.clear() # 🆕 Wipe the memory entirely
        for i in self.tree.get_children():
            v = list(self.tree.item(i, 'values'))
            v[1] = "☐"
            self.tree.item(i, values=v)
        self.update_action_button()

    def get_checked_items(self):
        return [item for item in self.tree.get_children() if self.tree.item(item, "values")[1] == "☑"]

    def on_double_click(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell" and self.tree.identify_column(event.x) != '#2': 
            item = self.tree.identify_row(event.y)
            if item: self.open_native_file(self.tree.item(item, "values")[6])

    def show_context_menu(self, event):
        tree = event.widget
        item = tree.identify_row(event.y)
        if item:
            tree.selection_set(item)
            path = tree.item(item, "values")[-1]
            menu = tk.Menu(self, tearoff=0, bg="#2b2b2b", fg="white", font=("Arial", 11), activebackground="#3498db")
            menu.add_command(label="📄 Open File", command=lambda: self.open_native_file(path))
            menu.add_command(label="📁 Open File Location", command=lambda: self.open_file_location(path))
            menu.add_separator()
            menu.add_command(label="📋 Copy Path", command=lambda: self.clipboard_copy(path))
            menu.post(event.x_root, event.y_root)

    def open_native_file(self, path):
        """Safely opens any file using the default Windows application without freezing the UI."""
        if not os.path.exists(path): return
        
        # We put the slow OS request inside a sub-function
        def _open():
            try: 
                os.startfile(os.path.normpath(path))
            except Exception as e: 
                # Send the error back to the main UI thread safely
                self.after(0, lambda: messagebox.showerror("OS Error", f"Windows could not open this file:\n{e}"))
                
        # Launch it instantly in a background thread so the UI never freezes!
        threading.Thread(target=_open, daemon=True).start()

    def open_file_location(self, path):
        """Safely highlights the file inside the native Windows File Explorer without freezing."""
        if not os.path.exists(path): return
        
        def _explore():
            try:
                subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("OS Error", f"Could not launch File Explorer:\n{e}"))
                
        threading.Thread(target=_explore, daemon=True).start()

    def clipboard_copy(self, text):
        """Copies text to the system clipboard securely."""
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
    # --- AGENTIC & MANUAL ACTION SYSTEM ---
    def trigger_manual_action(self, action_type):
        checked = self.get_checked_items()
        if not checked:
            messagebox.showinfo("Select Files", "Please select files using the checkboxes first.")
            return
        self.pending_action_type = action_type
        if action_type in ["MOVE", "COPY"]:
            target = filedialog.askdirectory(title=f"Select Destination Folder for {action_type.title()}")
            if not target: self.pending_action_type = None; return
            self.pending_target_folder = target
        elif action_type in ["ZIP", "PACK"]:
            target = filedialog.asksaveasfilename(title=f"Save Archive As...", defaultextension=".zip", filetypes=[("Zip Archives", "*.zip")])
            if not target: self.pending_action_type = None; return
            self.pending_target_folder = target
        else: self.pending_target_folder = None
            
        if hasattr(self, 'manual_action_frame'): self.manual_action_frame.pack_forget()
        if hasattr(self, 'confirm_action_frame'): self.confirm_action_frame.pack(side="right", fill="x")
        self.update_action_button()
        self.btn_confirm_action.pack(side="right", padx=5)
        self.btn_cancel_action.pack(side="right", padx=5)

    def apply_task(self, sql):
        self.cancel_action(); self.current_sql_filter = sql; self.tabs.set("Action Center"); self.update_view()

    def apply_trash_task(self, sql):
        self.cancel_action(); self.current_trash_sql_filter = sql; self.tabs.set("Zen Trash"); self.update_trash_view(silent=True)

    def prepare_action(self, action_type, sql_where, target_folder=None):
        self.pending_action_type = action_type; self.pending_target_folder = target_folder; self.current_sql_filter = sql_where
        self.tabs.set("Action Center"); self.update_view(); self.check_all()
        checked = len(self.get_checked_items())
        if checked == 0:
            self.log_to(self.omni_log, "🤖 Omni: No active files matched that request!"); self.cancel_action(); return

        if hasattr(self, 'manual_action_frame'): self.manual_action_frame.pack_forget()
        if hasattr(self, 'confirm_action_frame'): self.confirm_action_frame.pack(side="right", fill="x")
        self.update_action_button()
        self.btn_confirm_action.pack(side="right", padx=5); self.btn_cancel_action.pack(side="right", padx=5)
        self.log_to(self.omni_log, f"⏸️ HUMAN AUTHENTICATION REQUIRED: I have staged {checked} files. Review them before confirming.")

    def prepare_trash_action(self, action_type, sql_where):
        self.current_trash_sql_filter = sql_where
        self.tabs.set("Zen Trash"); self.update_trash_view(silent=True); self.check_all_trash()
        checked = len([i for i in self.trash_tree.get_children() if self.trash_tree.item(i, "values")[0] == "☑"])
        if checked == 0: self.log_to(self.omni_log, "🤖 Omni: No files in the trash matched.")
        else: self.log_to(self.omni_log, f"⏸️ HUMAN AUTHENTICATION REQUIRED: Staged {checked} files in Zen Trash.")

    def update_action_button(self):
        if not self.pending_action_type: return
        count = len(self.get_checked_items())
        if self.pending_action_type == "MOVE": self.btn_confirm_action.configure(text=f"✅ Confirm Move ({count} Items)", fg_color="#27ae60")
        elif self.pending_action_type == "COPY": self.btn_confirm_action.configure(text=f"✅ Confirm Copy ({count} Items)", fg_color="#2980b9")
        elif self.pending_action_type == "ZIP": self.btn_confirm_action.configure(text=f"📦 Confirm Zip & Keep ({count} Items)", fg_color="#8e44ad")
        elif self.pending_action_type == "PACK": self.btn_confirm_action.configure(text=f"📦 Confirm Pack & Trash Originals ({count} Items)", fg_color="#9b59b6")
        elif self.pending_action_type == "RENAME": self.btn_confirm_action.configure(text=f"✨ Confirm Smart Rename ({count} Items)", fg_color="#f39c12")
        elif self.pending_action_type in ["RECYCLE", "DELETE"]: self.btn_confirm_action.configure(text=f"♻️ Send {count} Items to OS Recycle Bin", fg_color="#d35400")

    def cancel_action(self):
        if hasattr(self, 'btn_confirm_action'): self.btn_confirm_action.pack_forget()
        if hasattr(self, 'btn_cancel_action'): self.btn_cancel_action.pack_forget()
        if hasattr(self, 'confirm_action_frame'): self.confirm_action_frame.pack_forget()
        if hasattr(self, 'manual_action_frame'): self.manual_action_frame.pack(side="left", fill="x")
        self.pending_action_type = self.pending_target_folder = None; self.uncheck_all()

    def organize_specific_folder(self, folder_name):
        self.log_to(self.omni_log, f"🤖 Omni: Organizing folder '{folder_name}' by categories...")
        conn = get_db_connection()
        safe_folder = folder_name.replace("'", "''")
        rows = conn.execute(f"SELECT path, name, cat, size FROM files WHERE LOWER(path) LIKE '%\\{safe_folder.lower()}\\%'").fetchall()
        if not rows:
            self.log_to(self.omni_log, f"⚠️ Omni: Couldn't find files in a folder named '{folder_name}'.")
            conn.close(); return
            
        target_root = os.path.join(ZEN_PORTAL_DIR, f"{folder_name.title()}_Organized")
        moved, total_size_mb, move_log = 0, 0, []
        
        for path, name, cat, sz in rows:
            if not os.path.exists(path): continue
            cat_dir = os.path.join(target_root, cat)
            os.makedirs(cat_dir, exist_ok=True)
            new_path = os.path.join(cat_dir, name)
            base, ext = os.path.splitext(name)
            c = 1
            while os.path.exists(new_path):
                new_path = os.path.join(cat_dir, f"{base}_{c}{ext}"); c += 1
            try:
                shutil.move(path, new_path)
                conn.execute("UPDATE files SET path=? WHERE path=?", (new_path, path))
                move_log.append((path, new_path)); moved += 1; total_size_mb += sz
            except Exception as e: pass
            
        conn.commit(); conn.close()
        if moved > 0:
            self.push_undo({"action": "MOVE", "timestamp": time.time(), "count": moved, "size": total_size_mb, "moves": move_log, "zip_path": None, "details": f"Target: {folder_name.title()}_Organized (Smart Categorization)"})
            self.log_to(self.omni_log, f"✅ Success! Smart-Organized {moved} files from '{folder_name}'.")
            self.update_view(); self.update_sidebar_stats()
        else: self.log_to(self.omni_log, "🤖 Omni: No files were moved.")

    def execute_verified_action(self):
        checked = self.get_checked_items()
        if not checked: return
        total_tasks = len(checked)
        self.btn_cancel_action.pack_forget() 
        conn = get_db_connection()
        processed_count, total_size_mb, move_log = 0, 0, []
        action_performed = self.pending_action_type
        final_zip_path = target_dir = None
        
        try:
            if action_performed in ["MOVE", "COPY", "ZIP", "PACK"] and self.pending_target_folder:
                if ":" in self.pending_target_folder or self.pending_target_folder.startswith("\\") or self.pending_target_folder.startswith("/"): target_dir = self.pending_target_folder
                else: target_dir = os.path.join(ZEN_PORTAL_DIR, self.pending_target_folder)
                if action_performed not in ["ZIP", "PACK"]: os.makedirs(target_dir, exist_ok=True)

            if action_performed in ["ZIP", "PACK"]:
                zip_path = target_dir if target_dir and target_dir.lower().endswith('.zip') else f"{target_dir}.zip" if target_dir else os.path.join(ZEN_PORTAL_DIR, "ZenArchive.zip")
                final_zip_path = zip_path
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for idx, item in enumerate(checked):
                        self.btn_confirm_action.configure(text=f"📦 Packing... {total_tasks - idx} tasks left"); self.update()
                        old_path = self.tree.item(item)['values'][6]
                        sz = float(self.tree.item(item)['values'][3].replace(" MB", ""))
                        if os.path.exists(old_path): 
                            zf.write(old_path, os.path.basename(old_path))
                            move_log.append((old_path, os.path.basename(old_path))); processed_count += 1; total_size_mb += sz
                
                if action_performed == "PACK":
                    for idx, item in enumerate(checked):
                        self.btn_confirm_action.configure(text=f"🗑️ Trashing Originals... {total_tasks - idx} tasks left"); self.update()
                        old_path = self.tree.item(item)['values'][6]
                        try:
                            if HAS_WIN_TRASH: send2trash(os.path.normpath(old_path))
                            else: os.remove(old_path)
                            conn.execute("DELETE FROM files WHERE path=?", (old_path,))
                        except: pass

            elif action_performed == "RENAME":
                for idx, item in enumerate(checked):
                    self.btn_confirm_action.configure(text=f"✨ Renaming... {total_tasks - idx} left"); self.update()
                    old_path = self.tree.item(item)['values'][6]
                    sz = float(self.tree.item(item)['values'][3].replace(" MB", ""))
                    if not os.path.exists(old_path): continue
                    folder, old_name = os.path.dirname(old_path), os.path.basename(old_path)
                    base, ext = os.path.splitext(old_name)
                    clean_name = re.sub(r'\d{6,}', '', re.sub(r'[\_\-]', ' ', base)).strip().title() or "ZenFile"
                    new_path = os.path.join(folder, f"{clean_name}{ext}")
                    c = 1
                    while os.path.exists(new_path) and new_path != old_path:
                        new_path = os.path.join(folder, f"{clean_name} {c}{ext}"); c += 1
                    if new_path != old_path:
                        shutil.move(old_path, new_path)
                        conn.execute("UPDATE files SET path=?, name=? WHERE path=?", (new_path, os.path.basename(new_path), old_path))
                        move_log.append((old_path, new_path)); processed_count += 1; total_size_mb += sz

            elif action_performed in ["MOVE", "COPY", "RECYCLE", "DELETE"]:
                for idx, item in enumerate(checked):
                    self.btn_confirm_action.configure(text=f"⚙️ Processing... {total_tasks - idx} left"); self.update()
                    vals = self.tree.item(item)['values']
                    name, sz, old_path = vals[2], float(vals[3].replace(" MB", "")), vals[6] 
                    if not os.path.exists(old_path): continue
                    try:
                        if action_performed == "MOVE":
                            new_path = os.path.join(target_dir, name)
                            base, ext = os.path.splitext(name)
                            c = 1
                            while os.path.exists(new_path): new_path = os.path.join(target_dir, f"{base}_{c}{ext}"); c += 1
                            shutil.move(old_path, new_path)
                            conn.execute("UPDATE files SET path=? WHERE path=?", (new_path, old_path))
                            move_log.append((old_path, new_path))
                        elif action_performed == "COPY":
                            new_path = os.path.join(target_dir, name)
                            base, ext = os.path.splitext(name)
                            c = 1
                            while os.path.exists(new_path): new_path = os.path.join(target_dir, f"{base}_{c}{ext}"); c += 1
                            shutil.copy2(old_path, new_path)
                            move_log.append((old_path, new_path))
                        elif action_performed in ["RECYCLE", "DELETE"]:
                            if HAS_WIN_TRASH: send2trash(os.path.normpath(old_path))
                            else: os.remove(old_path)
                            conn.execute("DELETE FROM files WHERE path=?", (old_path,))
                            move_log.append((old_path, "OS_RECYCLE_BIN"))
                        processed_count += 1; total_size_mb += sz
                    except: pass
            
            self.push_undo({"action": action_performed, "timestamp": time.time(), "count": processed_count, "size": total_size_mb, "moves": move_log, "zip_path": final_zip_path, "details": f"Target: {self.pending_target_folder if self.pending_target_folder else 'System Operations'}"})
            conn.commit()
            action_past = {"MOVE":"Moved & Grouped", "COPY":"Copied", "ZIP":"Zipped", "PACK": "Packed", "RECYCLE":"Safely Trashed", "DELETE":"Safely Trashed", "RENAME":"Smart Renamed"}
            self.log_to(self.omni_log, f"✅ Execution Verified! {action_past.get(action_performed, 'Processed')} {processed_count} files.")
            self.cancel_action()
            self.update_view(); self.update_sidebar_stats(); self.update_trash_view(silent=False)
        except Exception as e: messagebox.showerror("Error", str(e))
        finally: conn.close()

    def triage_downloads(self):
        self.log_to(self.omni_log, "🤖 Omni: Triaging your Downloads folder into ZenSpace Portal...")
        dl_path = os.path.join(os.environ['USERPROFILE'], 'Downloads')
        target_root = os.path.join(ZEN_PORTAL_DIR, 'Zen_Triage')
        if not os.path.exists(dl_path): self.log_to(self.omni_log, "⚠️ Omni: Downloads folder not found."); return

        files_to_triage = [f for f in os.listdir(dl_path) if os.path.isfile(os.path.join(dl_path, f))]
        if not files_to_triage: self.log_to(self.omni_log, "🤖 Omni: Downloads folder is already clean!"); return

        moved, total_size_mb, move_log = 0, 0, []
        conn = get_db_connection()
        for idx, f in enumerate(files_to_triage):
            self.status_lbl.configure(text=f"Triaging Downloads... {len(files_to_triage) - idx} left"); self.update()
            p = os.path.join(dl_path, f)
            sz = os.path.getsize(p) / (1024*1024)
            ext = os.path.splitext(f)[1].lower()
            if ext in {'.mp4', '.mkv', '.mov', '.avi'}: cat = "Videos"
            elif ext in {'.jpg', '.png', '.jpeg', '.gif', '.webp'}: cat = "Photos"
            elif ext in {'.mp3', '.wav', '.aac', '.flac'}: cat = "Audio"
            elif ext in {'.pdf', '.docx', '.txt', '.xlsx', '.csv', '.pptx'}: cat = "Documents"
            elif ext in {'.zip', '.rar', '.7z', '.tar', '.gz'}: cat = "Archives"
            elif ext in {'.exe', '.msi'}: cat = "Apps"
            else: cat = "Others"

            cat_dir = os.path.join(target_root, cat)
            os.makedirs(cat_dir, exist_ok=True)
            new_path = os.path.join(cat_dir, f)
            base, ext = os.path.splitext(f)
            c = 1
            while os.path.exists(new_path): new_path = os.path.join(cat_dir, f"{base}_{c}{ext}"); c += 1
            try:
                shutil.move(p, new_path)
                conn.execute("UPDATE files SET path=? WHERE path=?", (new_path, p))
                move_log.append((p, new_path)); moved += 1; total_size_mb += sz
            except: pass
            
        conn.commit(); conn.close()
        self.status_lbl.configure(text="System Idle")
        self.push_undo({"action": "MOVE", "timestamp": time.time(), "count": moved, "size": total_size_mb, "moves": move_log, "zip_path": None, "details": "Target: Zen_Triage"})
        self.log_to(self.omni_log, f"✅ Success! Auto-Triaged {moved} files from Downloads.")
        self.update_view(); self.update_sidebar_stats()

    # --- CLOUD AI & MEMORY ---
    def load_chat_history(self):
        if os.path.exists(CHAT_HISTORY_FILE):
            try:
                with open(CHAT_HISTORY_FILE, "r") as f:
                    self.memory_buffer = json.load(f)
                    for line in self.memory_buffer:
                        if line.startswith("User:"): self.log_to(self.omni_log, f"👤: {line[6:]}", scroll=False)
                        elif line.startswith("Omni:"):
                            disp = re.sub(r'^(CHAT:|SQL:|SQL_TRASH:|ACTION_ORGANIZE:[^|]*|ACTION_MOVE:[^|]*\||ACTION_COPY:[^|]*\||ACTION_ZIP:[^|]*\||ACTION_PACK:[^|]*\||ACTION_RECYCLE:|ACTION_DELETE:|ACTION_RESTORE_TRASH:|ACTION_DELETE_TRASH:|ACTION_EMPTY_TRASH:|ACTION_DUPLICATES:|ACTION_JUNK:|ACTION_RENAME:|ACTION_TRIAGE:)\s*', '', line[6:]).strip()
                            self.log_to(self.omni_log, f"🤖 Omni: {disp}", scroll=False)
                self.omni_log.see("end")
            except: self.memory_buffer = []

    def save_chat_history(self):
        try:
            with open(CHAT_HISTORY_FILE, "w") as f: json.dump(self.memory_buffer, f)
        except: pass

    def clear_chat_history(self):
        if messagebox.askyesno("Confirm", "Wipe Omni's memory and chat history?"):
            self.memory_buffer.clear()
            if os.path.exists(CHAT_HISTORY_FILE): os.remove(CHAT_HISTORY_FILE)
            self.omni_log.configure(state="normal"); self.omni_log.delete("1.0", "end"); self.omni_log.configure(state="disabled")

    def log_to(self, box, new_text, scroll=True):
        box.configure(state="normal")
        content = box.get("1.0", "end-1c")
        lines = content.split('\n\n')
        if new_text.startswith("🤖") and len(lines) >= 2 and lines[-1].startswith("🤖"): lines[-1] = new_text 
        else: lines.append(new_text)
        box.delete("1.0", "end")
        box.insert("end", '\n\n'.join(lines).strip() + '\n\n')
        box.configure(state="disabled")
        if scroll: box.see("end")

    # --- 🛡️ THE TELEMETRY & API CONTROL SYSTEM ---
    def run_ai(self):
        if not gemini_model: messagebox.showerror("Error", "Missing API Key! Paste it into line 39 or your .env file."); return
        if self.ai_processing: return 
        msg = self.omni_entry.get()
        if not msg.strip(): return
        self.omni_entry.delete(0, 'end'); self.log_to(self.omni_log, f"👤: {msg}")
        self.ai_processing = True 
        threading.Thread(target=self.ai_worker, args=(msg,), daemon=True).start()

    def get_ai_suggestions(self):
        if self.ai_processing or not gemini_model: return 
        self.ai_processing = True
        threading.Thread(target=self._suggestion_worker, daemon=True).start()

    def _suggestion_worker(self):
        try:
            self.track_api_usage(); self.last_ai_call_time = time.time()
            self.after(0, lambda: self.log_to(self.omni_log, "🤖 Omni: Scanning deep system metrics..."))

            conn = get_db_connection()
            try:
                junk_sz = conn.execute("SELECT SUM(size) FROM files WHERE cat='System Junk'").fetchone()[0] or 0
                dl_count = conn.execute("SELECT COUNT(*) FROM files WHERE path LIKE '%Downloads%'").fetchone()[0] or 0
                old_sz = conn.execute(f"SELECT SUM(size) FROM files WHERE mtime < {time.time() - 15552000}").fetchone()[0] or 0 
                self.update_trash_view(silent=True)
                trash_sz = conn.execute("SELECT SUM(size) FROM trash").fetchone()[0] or 0
            except: junk_sz, dl_count, old_sz, trash_sz = 0, 0, 0, 0
            conn.close()

            prompt = f"""You are ZenSpace Omni, a proactive file management AI.
            System status: Junk: {junk_sz:.2f} MB, Rotting in Downloads: {dl_count}, Untouched 6 months: {old_sz/1024:.2f} GB, Recycle Bin: {trash_sz/1024:.2f} GB.
            Analyze this and provide ONE friendly proactive recommendation to optimize their system. 
            Tell them EXACTLY what to type to you to execute the action. Do NOT execute it.
            Start response with EXACTLY: "CHAT: 💡 "
            """
            
            global current_key_index, gemini_model
            max_retries, attempts, resp = len(API_KEYS), 0, ""
            while attempts < max_retries:
                if not API_KEYS[current_key_index] or API_KEYS[current_key_index].startswith("PASTE_"): attempts += 1; continue 
                try:
                    resp = gemini_model.generate_content(prompt).text.strip()
                    break 
                except Exception as e:
                    if "429" in str(e) or "quota" in str(e).lower():
                        attempts += 1
                        if attempts < max_retries:
                            self.after(0, lambda k=current_key_index+1: self.log_to(self.omni_log, f"⚠️ Key {k} exhausted. Rotating..."))
                            current_key_index = (current_key_index + 1) % len(API_KEYS)
                            gemini_model = init_ai()
                        else: raise e 
                    else: raise e 

            self.memory_buffer.append(f"Omni: {resp}"); self.save_chat_history()
            self.after(0, lambda: self.log_to(self.omni_log, f"🤖 Omni: {resp[5:].strip()}" if resp.startswith("CHAT:") else f"🤖 Omni: 💡 {resp}"))
            self.ai_processing = False
        except Exception as e: self.handle_api_error(str(e))

    def ai_worker(self, user_txt):
        try:
            self.track_api_usage()
            self.last_ai_call_time = time.time()
            self.after(0, lambda: self.log_to(self.omni_log, "🤖 Omni: Cognitive matrix analyzing..."))
            
            db_link = get_db_connection()
            try: 
                f_count = db_link.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                t_vol = db_link.execute("SELECT SUM(size) FROM files").fetchone()[0] or 0
                self.update_trash_view(silent=True)
            except: 
                f_count, t_vol = 0, 0
            db_link.close()
            
            now_ts = int(time.time())
            mem_span = "\n".join(self.memory_buffer[-12:]) 
            
            omni_brain = f"""You are ZenSpace Omni, an elite, highly adaptive file management Agentic AI. 
            STATUS: {f_count} active files consuming {t_vol/1024:.2f} GB. Unix Time: {now_ts}. 
            DATABASE SCHEMA: `files`: name, ext, size, cat, mtime, path.

            MACRO COMMANDS (Choose EXACTLY ONE per response):
            - CHAT: <message>
            - SQL: <condition>
            - SQL_TRASH: <condition>
            - ACTION_ORGANIZE: <FolderName>
            - ACTION_MOVE: <PathOrFolderName> | <condition>
            - ACTION_COPY: <PathOrFolderName> | <condition>
            - ACTION_ZIP: <PathOrArchiveName> | <condition>
            - ACTION_PACK: <PathOrArchiveName> | <condition>
            - ACTION_RENAME: <condition>
            - ACTION_RECYCLE: <condition>
            - ACTION_DUPLICATES:
            - ACTION_JUNK:
            - ACTION_TRIAGE:
            - ACTION_EMPTY_TRASH:
            - ACTION_DEEP_CLEAN:
            - ACTION_ANALYZE_DRIVE:
            - ACTION_ARCHIVE_OLD: <Days>
            - ACTION_REPORT:

            RULES: 
            1. Output NOTHING except the strict prefix and its argument.
            2. SQL conditions MUST use single quotes and omit 'WHERE' or 'SELECT'.
            3. Adapt to the user's phrasing based on the PAST MEMORY.

            TRAINING SHOTS:
            User: "show me all .jpg files" -> SQL: ext = '.jpg' OR ext = '.jpeg'
            User: "delete system junk" -> ACTION_RECYCLE: cat = 'System Junk'
            User: "zip my python files to CodeArchive" -> ACTION_ZIP: CodeArchive | ext = '.py'
            User: "give me a status update" -> ACTION_REPORT:
            User: "clean everything up" -> ACTION_DEEP_CLEAN:
            User: "archive stuff older than 30 days" -> ACTION_ARCHIVE_OLD: 30
            User: "show my drive matrix" -> ACTION_ANALYZE_DRIVE:

            PAST MEMORY:
            {mem_span}
            User: "{user_txt}"
            Response:"""

            global current_key_index, gemini_model
            max_tries = len(API_KEYS)
            tries = 0
            ai_resp = ""
            
            while tries < max_tries:
                if not API_KEYS[current_key_index] or API_KEYS[current_key_index].startswith("PASTE_"): 
                    tries += 1
                    continue 
                try:
                    ai_resp = gemini_model.generate_content(omni_brain).text.strip()
                    break 
                except Exception as err:
                    if "429" in str(err) or "quota" in str(err).lower():
                        tries += 1
                        if tries < max_tries:
                            current_key_index = (current_key_index + 1) % len(API_KEYS)
                            gemini_model = init_ai()
                        else: raise err 
                    else: raise err 

            def clean_query(raw_str):
                return re.sub(r'(?i)\s*(MB|GB|KB)\b', '', re.sub(r';$', '', re.sub(r'(?i)^WHERE\s+', '', re.sub(r'(?i)^(SELECT.*?WHERE)\s+', '', raw_str).strip()).strip()).strip()).strip()

            def wipe_slate():
                self.memory_buffer.clear()
                self.save_chat_history()
                self.omni_log.configure(state="normal")
                self.omni_log.delete("1.0", "end")
                self.omni_log.configure(state="disabled")

            is_exec = True
            
            if ai_resp.startswith("CHAT:"): 
                self.after(0, lambda: self.log_to(self.omni_log, f"🤖 Omni: {ai_resp[5:].strip()}"))
                self.memory_buffer.append(f"User: {user_txt}")
                self.memory_buffer.append(f"Omni: {ai_resp}")
                self.save_chat_history()
                is_exec = False 
                
            elif ai_resp.startswith("ACTION_DUPLICATES:"): self.after(0, self.trigger_lightning_dupes)
            elif ai_resp.startswith("ACTION_JUNK:"): self.after(0, lambda: self.apply_task("cat='System Junk'"))
            elif ai_resp.startswith("ACTION_TRIAGE:"): self.after(0, self.triage_downloads)
            elif ai_resp.startswith("ACTION_EMPTY_TRASH:"): self.after(0, self.empty_zen_trash)
            elif ai_resp.startswith("ACTION_ANALYZE_DRIVE:"): self.after(0, self.show_drive_analysis)
            
            elif ai_resp.startswith("ACTION_DEEP_CLEAN:"):
                self.after(0, self.empty_zen_trash)
                self.after(500, self.trigger_ghost_sweeper)
                self.after(1000, lambda: self.prepare_action("RECYCLE", "cat='System Junk'"))
                
            elif ai_resp.startswith("ACTION_ARCHIVE_OLD:"):
                days_old = int(re.search(r'\d+', ai_resp).group() or 30)
                cutoff = int(time.time()) - (days_old * 86400)
                self.after(0, lambda: self.prepare_action("PACK", f"mtime < {cutoff}", f"Archive_{days_old}_Days"))

            elif ai_resp.startswith("ACTION_REPORT:"):
                self.after(0, self.get_ai_suggestions)
                is_exec = False
                
            elif ai_resp.startswith("ACTION_ORGANIZE:"): 
                target_f = ai_resp.replace("ACTION_ORGANIZE:", "").strip()
                self.after(0, lambda f=target_f: self.organize_specific_folder(f))
                
            elif ai_resp.startswith("SQL:"):
                q_str = clean_query(ai_resp.replace("SQL:", "", 1).replace("```sql", "").replace("```", ""))
                if q_str: self.after(0, lambda sql=q_str: self.apply_task(sql))
                
            elif ai_resp.startswith("SQL_TRASH:"):
                q_str = clean_query(ai_resp.replace("SQL_TRASH:", "", 1).replace("```sql", "").replace("```", ""))
                if q_str: self.after(0, lambda sql=q_str: self.apply_trash_task(sql))
                
            elif ai_resp.startswith("ACTION_RESTORE_TRASH:") or ai_resp.startswith("ACTION_DELETE_TRASH:"):
                a_mode = "RESTORE" if ai_resp.startswith("ACTION_RESTORE_TRASH:") else "DELETE"
                q_str = clean_query(ai_resp.replace(f"ACTION_{a_mode}_TRASH:", "", 1).replace("```sql", "").replace("```", ""))
                if q_str: self.after(0, lambda a=a_mode, s=q_str: self.prepare_trash_action(a, s))
                
            elif ai_resp.startswith("ACTION_MOVE:") or ai_resp.startswith("ACTION_COPY:") or ai_resp.startswith("ACTION_ZIP:") or ai_resp.startswith("ACTION_PACK:"):
                try:
                    a_mode = "MOVE" if ai_resp.startswith("ACTION_MOVE:") else ("COPY" if ai_resp.startswith("ACTION_COPY:") else ("ZIP" if ai_resp.startswith("ACTION_ZIP:") else "PACK"))
                    slices = ai_resp.replace(f"ACTION_{a_mode}:", "", 1).split("|", 1)
                    q_str = clean_query(slices[1].strip().replace("```sql", "").replace("```", "")) if len(slices) > 1 else "1=1"
                    if q_str: self.after(0, lambda a=a_mode, f=slices[0].strip(), s=q_str: self.prepare_action(a, s, f))
                except Exception as err: 
                    self.after(0, lambda msg=str(err): self.log_to(self.omni_log, f"⚠️ Fault: {msg}"))
                    is_exec = False
                
            elif ai_resp.startswith("ACTION_RECYCLE:") or ai_resp.startswith("ACTION_DELETE:") or ai_resp.startswith("ACTION_RENAME:"):
                a_mode = "RECYCLE" if ai_resp.startswith("ACTION_RECYCLE:") else ("DELETE" if ai_resp.startswith("ACTION_DELETE:") else "RENAME")
                q_str = clean_query(ai_resp.replace(f"ACTION_{a_mode}:", "", 1).replace("```sql", "").replace("```", ""))
                if q_str: self.after(0, lambda a=a_mode, s=q_str: self.prepare_action(a, s))
                
            else: 
                self.after(0, lambda: self.log_to(self.omni_log, f"🤖: {ai_resp}"))
                is_exec = False
            
            if is_exec:
                self.after(0, wipe_slate)
                
            self.ai_processing = False
        except Exception as err: 
            self.handle_api_error(str(err))

    def handle_api_error(self, err_msg):
        if "429" in err_msg or "quota" in err_msg.lower():
            match = re.search(r'retry in (\d+)', err_msg)
            if match: self.after(0, lambda: self.start_live_cooldown(int(match.group(1)) + 1))
            elif "generate_content_free_tier_requests" in err_msg:
                self.after(0, lambda: self.log_to(self.omni_log, "⛔ CRITICAL RATE LIMIT: All keys exhausted.")); self.ai_processing = False
            else: self.after(0, lambda: self.start_live_cooldown(30)) 
        else: self.after(0, lambda e=err_msg: self.log_to(self.omni_log, f"⚠️ AI Error: {e}")); self.ai_processing = False

    def start_live_cooldown(self, wait_seconds):
        self.omni_entry.configure(state="disabled"); self.btn_suggest.configure(state="disabled")
        self.log_to(self.omni_log, f"⚠️ API Cooldown: {wait_seconds}s...")
        def countdown(t):
            if t > 0: self.omni_entry.configure(placeholder_text=f"⏳ Cooldown: {t}s..."); self.after(1000, countdown, t - 1)
            else:
                self.omni_entry.configure(state="normal", placeholder_text="Ask Omni..."); self.btn_suggest.configure(state="normal")
                self.ai_processing = False; self.log_to(self.omni_log, "✅ Cooldown complete!")
        countdown(wait_seconds)

if __name__ == "__main__":
    app = ZenSpaceOmni()
    app.mainloop()