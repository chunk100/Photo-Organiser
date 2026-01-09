#Version 8 - with EXIF orientation correction and expanded image format support

import os
import sqlite3
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk, ImageOps, ImageDraw
import io
import time
import shutil

# Add HEIC/HEIF support if available
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_SUPPORT = True
except ImportError:
    HEIF_SUPPORT = False
    print("pillow-heif not installed. HEIC/HEIF files will not be supported.")

class PhotoOrganizer:
    CONFIG_KEY_LAST_FOLDER = "last_folder"

    def __init__(self, root):
        self.root = root
        self.root.title("Photo Organizer")
        
        # 1. Define paths based strictly on script location
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.db_path = os.path.join(self.script_dir, "photo_data.db")
        
        # Initialize photo_root (will be set properly after DB check)
        self.photo_root = self.script_dir
        self.current_folder = self.script_dir
        
        self.fullscreen_images = []
        self.current_image_index = 0
        self.thumb_refs = {}
        self.thumbs = []
        
        # Flag to track if scan should be cancelled
        self.scan_cancelled = False

        self.setup_ui()
        
        # 2. Check/Create database
        if not self.check_and_setup_db():
            return  # Exit if user chose to quit
        
        # 3. Load the photo root from database (after migration is complete)
        stored_root = self.load_setting("photo_root_path")
        if stored_root and os.path.isdir(stored_root):
            self.photo_root = stored_root
            self.current_folder = stored_root
        
        # 4. Automatic startup - populate tree and cleanup, but skip automatic scan
        threading.Thread(target=self.cleanup_orphan_thumbnails, daemon=True).start()
        self.populate_tree(start_folder=self.photo_root)
        # Removed automatic scan on startup to avoid OneDrive downloads
        # Use "Rescan Current Folder" button to manually scan when needed

    def get_supported_image_extensions(self):
        """Returns a tuple of supported image extensions"""
        extensions = [
            # JPEG formats
            ".jpg", ".jpeg", ".jpe", ".jfif",
            # PNG
            ".png",
            # GIF
            ".gif",
            # TIFF
            ".tif", ".tiff",
            # BMP
            ".bmp", ".dib",
            # WebP
            ".webp",
            # ICO
            ".ico",
            # PPM/PGM/PBM
            ".ppm", ".pgm", ".pbm", ".pnm"
        ]
        
        # Add HEIC/HEIF if supported
        if HEIF_SUPPORT:
            extensions.extend([".heic", ".heif"])
        
        return tuple(extensions)

    def check_and_setup_db(self):
        """Strictly manages DB location and handles path migration if moved."""
        if os.path.exists(self.db_path):
            self.init_db()
            
            # 1. Load the stored root
            raw_stored_root = self.load_setting("photo_root_path")
            
            if raw_stored_root:
                # 2. Normalize both paths to standardise slashes for comparison
                stored_root = os.path.normpath(raw_stored_root)
                current_root = os.path.normpath(self.script_dir)
                
                # 3. Now the comparison will work regardless of / or \
                if stored_root != current_root:
                    if self.prompt_for_path_update(stored_root, current_root):
                        self.relocate_database_paths(stored_root, current_root)
                    else:
                        self.root.destroy()
                        return False
            return True
            
        # If DB doesn't exist, prompt the user to create one
        result = messagebox.askyesno(
            "Database Not Found",
            f"No database found in:\n{self.script_dir}\n\n"
            "Create a new database here?",
            icon='question'
        )
        
        if result:
            self.init_db()
            self.save_setting("photo_root_path", self.script_dir)
            self.save_setting(self.CONFIG_KEY_LAST_FOLDER, self.script_dir)
            return True
        else:
            self.root.destroy()
            return False

    def prompt_for_path_update(self, old_root, new_root):
        """Asks the user if they want to update the database to the new location."""
        msg = (
            f"Location Mismatch Detected!\n\n"
            f"Database expects: {old_root}\n"
            f"Current location: {new_root}\n\n"
            "This usually happens if you move the folder or change drive letters.\n"
            "Would you like to update all database records to this new path?"
        )
        return messagebox.askyesno("Update Database Paths?", msg, icon='warning')

    def relocate_database_paths(self, old_root, new_root):
        """Standardizes and updates all file paths in the 'photos' table."""
        old_root = os.path.normpath(old_root)
        new_root = os.path.normpath(new_root)

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 1. Update the 'path' column (Full path to every photo)
            cursor.execute("""
                UPDATE photos 
                SET path = ? || SUBSTR(path, LENGTH(?) + 1)
                WHERE path LIKE ? || '%'
            """, (new_root, old_root, old_root))

            # 2. Update the 'folder' column (Used for the navigation tree)
            cursor.execute("""
                UPDATE photos 
                SET folder = ? || SUBSTR(folder, LENGTH(?) + 1)
                WHERE folder LIKE ? || '%'
            """, (new_root, old_root, old_root))
            
            changes = conn.total_changes
            conn.commit()
            conn.close()
            
            # 3. Update settings to ensure they match the new location
            self.save_setting("photo_root_path", new_root)
            self.save_setting(self.CONFIG_KEY_LAST_FOLDER, new_root)
            self.current_folder = new_root
            
            messagebox.showinfo("Success", 
                f"Database updated successfully!\n\n"
                f"Fixed {changes} records to use the new path:\n{new_root}")
            
        except Exception as e:
            messagebox.showerror("Relocation Error", 
                f"Failed to update database paths: {str(e)}")
            
    def setup_ui(self):
        # Start in maximized mode
        self.root.state('zoomed')  # For Windows
        # For cross-platform compatibility, also try:
        try:
            self.root.attributes('-zoomed', True)  # For Linux
        except:
            pass
        
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)
        
        tools_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Tools", menu=tools_menu)
        tools_menu.add_command(label="Copy Starred Photos", command=self.open_copy_starred_window)
        tools_menu.add_command(label="Move Files by Year and Month", command=self.open_move_by_year_window)
        tools_menu.add_command(label="Copy Video Files", command=self.open_copy_videos_window)
        tools_menu.add_command(label="Find Duplicate Photos", command=self.open_duplicate_finder_window)
        
        paned = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        left_frame = ttk.Frame(paned, width=250)
        paned.add(left_frame, weight=1)

        self.tree = ttk.Treeview(left_frame)
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)

        self.tree_scroll = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=self.tree_scroll.set)

        self.tree.bind("<<TreeviewOpen>>", self.expand_node)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        center_frame = ttk.Frame(paned)
        paned.add(center_frame, weight=4)

        self.canvas = tk.Canvas(center_frame)
        self.v_scroll = ttk.Scrollbar(center_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        self.v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        self.canvas.configure(yscrollcommand=self.v_scroll.set)

        self.thumbs_frame = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.thumbs_frame, anchor="nw")
        self.thumbs_frame.bind("<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        bottom_frame = ttk.Frame(self.root)
        bottom_frame.pack(fill=tk.X)
        self.rescan_button = ttk.Button(bottom_frame, text="Rescan Current Folder", command=self.rescan_current_folder)
        self.rescan_button.pack(side=tk.LEFT, padx=5, pady=5)
        self.cancel_scan_button = ttk.Button(bottom_frame, text="Cancel Scan", command=self.cancel_scan, state='disabled')
        self.cancel_scan_button.pack(side=tk.LEFT, padx=5, pady=5)

        self.status_frame = ttk.Frame(self.root)
        self.status_frame.pack(fill=tk.X, side=tk.BOTTOM)
        self.status_scroll = ttk.Scrollbar(self.status_frame, orient=tk.HORIZONTAL)
        self.status_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_bar = tk.Text(self.status_frame, height=1, wrap='none', xscrollcommand=self.status_scroll.set)
        self.status_bar.pack(fill=tk.X, side=tk.TOP)
        self.status_scroll.config(command=self.status_bar.xview)
        self.status_bar.configure(state='disabled')

    def cancel_scan(self):
        """Cancel the current scan operation"""
        self.scan_cancelled = True
        self.set_status("Cancelling scan...")
        self.cancel_scan_button.config(state='disabled')

    def rescan_current_folder(self):
        # Rescan only the currently selected folder and its subfolders
        if self.current_folder and os.path.isdir(self.current_folder):
            self.set_status(f"Starting rescan of {self.current_folder}...")
            # Reset the cancel flag when starting a new scan
            self.scan_cancelled = False
            # Enable the cancel button
            self.cancel_scan_button.config(state='normal')
            # Disable the rescan button to prevent multiple scans
            self.rescan_button.config(state='disabled')
            threading.Thread(target=self.scan_and_store_thumbnails_with_stats, args=(self.current_folder,), daemon=True).start()
        else:
            messagebox.showwarning("No Folder Selected", "Please select a folder in the tree view to rescan.")

    def set_status(self, message):
        def _update():
            try:
                self.status_bar.configure(state='normal')
                self.status_bar.delete('1.0', tk.END)
                self.status_bar.insert(tk.END, message)
                self.status_bar.see(tk.END)
                self.status_bar.configure(state='disabled')
            except tk.TclError:
                pass
        self.root.after(0, _update)

    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS photos (
                path TEXT PRIMARY KEY,
                folder TEXT,
                tags TEXT,
                starred INTEGER DEFAULT 0,
                thumbnail BLOB,
                label TEXT,
                file_hash TEXT
            )
        """)
        cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_photos_folder ON photos(folder)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_photos_hash ON photos(file_hash)")
        conn.commit()
        conn.close()

    def save_setting(self, key, value):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()

    def load_setting(self, key):
        if not self.db_path or not os.path.exists(self.db_path):
            return None
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None

    def populate_tree(self, start_folder=None):
        for i in self.tree.get_children():
            self.tree.delete(i)

        if start_folder and os.path.isdir(start_folder):
            parent = self.tree.insert("", "end", text=start_folder, open=True, values=[start_folder])
            self.insert_subdirs(parent, start_folder)
        else:
            drive = os.path.abspath(os.sep)
            parent = self.tree.insert("", "end", text=drive, open=True, values=[drive])
            self.insert_subdirs(parent, drive)

    def insert_subdirs(self, parent, path):
        try:
            for name in os.listdir(path):
                full = os.path.join(path, name)
                if os.path.isdir(full):
                    node = self.tree.insert(parent, "end", text=name, values=[full])
                    self.tree.insert(node, "end", text="dummy")
        except PermissionError:
            pass

    def expand_node(self, event):
        node = self.tree.focus()
        children = self.tree.get_children(node)
        if len(children) == 1 and self.tree.item(children[0], "text") == "dummy":
            self.tree.delete(children[0])
            path = self.tree.item(node, "values")[0]
            self.insert_subdirs(node, path)

    def on_tree_select(self, event):
        node = self.tree.focus()
        if not node:
            return
        path = self.tree.item(node, "values")[0]
        self.current_folder = path
        
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM photos WHERE folder=?", (path,))
        count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM photos WHERE folder=? AND starred=1", (path,))
        starred_count = cur.fetchone()[0]
        conn.close()
        
        self.set_status(f"Selected: {path} | Photos: {count} | Starred: {starred_count}")
        
        threading.Thread(target=self.load_thumbnails_from_db, args=(path,), daemon=True).start()

    def scan_and_store_thumbnails_with_stats(self, folder):
        """Scan and store thumbnails with summary statistics at the end"""
        import hashlib
        
        self.set_status(f"Counting files in {folder}...")
        
        image_extensions = self.get_supported_image_extensions()

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        total = 0
        for _root, _dirs, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(image_extensions):
                    total += 1

        processed = 0
        new_files = 0
        orientations_corrected = 0
        errors = 0
        
        for root_dir, dirs, files in os.walk(folder):
            # Check if scan was cancelled
            if self.scan_cancelled:
                self.set_status(f"Scan cancelled. Processed {processed}/{total} files before cancellation.")
                conn.close()
                # Re-enable buttons
                self.root.after(0, lambda: self.rescan_button.config(state='normal'))
                self.root.after(0, lambda: self.cancel_scan_button.config(state='disabled'))
                return
                
            for f in files:
                # Check if scan was cancelled
                if self.scan_cancelled:
                    self.set_status(f"Scan cancelled. Processed {processed}/{total} files before cancellation.")
                    conn.close()
                    # Re-enable buttons
                    self.root.after(0, lambda: self.rescan_button.config(state='normal'))
                    self.root.after(0, lambda: self.cancel_scan_button.config(state='disabled'))
                    return
                    
                if f.lower().endswith(image_extensions):
                    full_path = os.path.join(root_dir, f)
                    
                    # Update status with progress
                    self.set_status(f"Scanning ({processed}/{total}): {full_path}")

                    cur.execute("SELECT path FROM photos WHERE path=?", (full_path,))
                    if cur.fetchone() is None:
                        try:
                            # Calculate MD5 hash
                            hash_md5 = hashlib.md5()
                            with open(full_path, "rb") as f_hash:
                                for chunk in iter(lambda: f_hash.read(4096), b""):
                                    hash_md5.update(chunk)
                            file_hash = hash_md5.hexdigest()
                            
                            img = Image.open(full_path)
                            
                            # Check if image has orientation tag that needs correction
                            try:
                                exif = img.getexif()
                                if exif and 274 in exif and exif[274] != 1:
                                    orientations_corrected += 1
                            except:
                                pass
                            
                            # Apply EXIF orientation correction
                            img = ImageOps.exif_transpose(img)
                            
                            img_copy = img.copy()
                            img_copy.thumbnail((150, 150))
                            with io.BytesIO() as output:
                                img_copy.save(output, format='PNG')
                                thumb_blob = output.getvalue()
                            cur.execute(
                                "INSERT OR REPLACE INTO photos (path, folder, thumbnail, file_hash) VALUES (?, ?, ?, ?)",
                                (full_path, root_dir, thumb_blob, file_hash)
                            )
                            new_files += 1
                        except Exception as e:
                            errors += 1
                            print(f"Error processing {full_path}: {e}")
                    
                    processed += 1

        conn.commit()

        # Only do cleanup if scan wasn't cancelled
        if not self.scan_cancelled:
            self.set_status("Running cleanup and database optimization...")
            deleted = self.cleanup_orphan_thumbnails()
            self.vacuum_db()
            
            conn.close()
            
            # Build summary status message
            status_msg = f"Rescan complete: {new_files} new files"
            if orientations_corrected > 0:
                status_msg += f" | {orientations_corrected} orientations corrected"
            if errors > 0:
                status_msg += f" | {errors} errors"
            if deleted > 0:
                status_msg += f" | {deleted} removed"
            
            self.set_status(status_msg)
            
            # Reload thumbnails for current folder
            self.root.after(0, lambda: self.load_thumbnails_from_db(folder))
        else:
            conn.close()
        
        # Re-enable buttons
        self.root.after(0, lambda: self.rescan_button.config(state='normal'))
        self.root.after(0, lambda: self.cancel_scan_button.config(state='disabled'))

    def scan_and_store_thumbnails(self, folder):
        self.set_status(f"Scanning and storing thumbnails: {folder}...")
        
        import hashlib
        image_extensions = self.get_supported_image_extensions()

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        total = 0
        for _root, _dirs, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(image_extensions):
                    total += 1

        processed = 0
        orientations_corrected = 0
        
        for root_dir, dirs, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(image_extensions):
                    full_path = os.path.join(root_dir, f)
                    self.set_status(f"Scanning ({processed}/{total}): {full_path}")

                    cur.execute("SELECT path FROM photos WHERE path=?", (full_path,))
                    if cur.fetchone() is None:
                        try:
                            # Calculate MD5 hash
                            hash_md5 = hashlib.md5()
                            with open(full_path, "rb") as f_hash:
                                for chunk in iter(lambda: f_hash.read(4096), b""):
                                    hash_md5.update(chunk)
                            file_hash = hash_md5.hexdigest()
                            
                            img = Image.open(full_path)
                            
                            # Check if image has orientation tag that needs correction
                            try:
                                exif = img.getexif()
                                if exif and 274 in exif and exif[274] != 1:
                                    orientations_corrected += 1
                            except:
                                pass
                            
                            # Apply EXIF orientation correction (this is very fast)
                            img = ImageOps.exif_transpose(img)
                            
                            img_copy = img.copy()
                            img_copy.thumbnail((150, 150))
                            with io.BytesIO() as output:
                                img_copy.save(output, format='PNG')
                                thumb_blob = output.getvalue()
                            cur.execute(
                                "INSERT OR REPLACE INTO photos (path, folder, thumbnail, file_hash) VALUES (?, ?, ?, ?)",
                                (full_path, root_dir, thumb_blob, file_hash)
                            )
                        except Exception as e:
                            print(f"Error processing {full_path}: {e}")
                    processed += 1

        conn.commit()

        def _cleanup_and_vacuum():
            deleted = self.cleanup_orphan_thumbnails()
            self.set_status(f"Cleanup complete. Removed {deleted} missing entries. Running VACUUM (this may take a while)...")
            self.vacuum_db()

        threading.Thread(target=_cleanup_and_vacuum, daemon=True).start()
        conn.close()
        
        # Enhanced completion message with orientation info
        completion_msg = f"Scanning complete. Processed {processed} files in {folder}"
        if orientations_corrected > 0:
            completion_msg += f" | Corrected {orientations_corrected} orientations"
        
        self.set_status(completion_msg)
        
        # Show a popup notification if any orientations were corrected
        if orientations_corrected > 0:
            self.root.after(0, lambda: messagebox.showinfo(
                "Scan Complete",
                f"Scan finished!\n\n"
                f"Total files processed: {processed}\n"
                f"Orientations corrected: {orientations_corrected}\n\n"
                f"Images with incorrect EXIF orientation have been rotated to display correctly."
            ))
        
        self.root.after(0, lambda: self.load_thumbnails_from_db(folder))

    def load_thumbnails_from_db(self, folder):
        self.set_status(f"Loading thumbnails from database: {folder}...")
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT path, thumbnail, starred FROM photos WHERE folder=?", (folder,))
        rows = cur.fetchall()
        conn.close()

        self.fullscreen_images = [path for path, _, _ in rows]
        self.current_image_index = 0

        images = []
        self.thumb_refs = {}
        self.thumbs = []

        for path, thumb_blob, starred in rows:
            try:
                img = Image.open(io.BytesIO(thumb_blob))
                # No need to apply exif_transpose here - already corrected when stored

                if starred:
                    draw = ImageDraw.Draw(img)
                    star_big = [(10,2),(14,10),(22,10),(16,15),
                                (18,23),(10,18),(2,23),(4,15),
                                (0,10),(8,10)]
                    draw.polygon(star_big, fill="yellow")

                images.append((path, img))
            except Exception:
                pass

        self.root.after(0, lambda: self.display_thumbnails_from_db(images))

    def display_thumbnails_from_db(self, images):
        for widget in self.thumbs_frame.winfo_children():
            widget.destroy()

        row = col = 0
        self.thumbs = []

        for full_path, img in images:
            try:
                tkimg = ImageTk.PhotoImage(img)
                self.thumbs.append(tkimg)

                lbl = ttk.Label(self.thumbs_frame, image=tkimg)
                lbl.grid(row=row, column=col, padx=5, pady=5)
                lbl.bind("<Double-1>", lambda e, idx=len(self.thumbs)-1: self.open_image_window(idx))
                self.thumb_refs[full_path] = lbl

                col += 1
                if col == 6:
                    col = 0
                    row += 1

            except Exception:
                pass

        self.set_status(f"Loaded {len(images)} thumbnails from {self.current_folder}")

    def open_image_window(self, index):
        self.current_image_index = index
        self.img_win = tk.Toplevel(self.root)
        self.img_win.title(os.path.basename(self.fullscreen_images[index]))
        
        # Maximize the image viewer window
        self.img_win.state('zoomed')  # For Windows
        # For cross-platform compatibility, also try:
        try:
            self.img_win.attributes('-zoomed', True)  # For Linux
        except:
            pass
        
        self.img_win.configure(background='black')

        self.label_var = tk.StringVar()
        self.label_display = tk.Label(self.img_win, textvariable=self.label_var,
                                      fg="black", bg="white", anchor="w")
        self.label_display.pack(fill=tk.X, side=tk.TOP)
        self.current_label_text = ""

        self.img_label = tk.Label(self.img_win, bg='black')
        self.img_label.pack(expand=True, fill=tk.BOTH)

        self.img_win.bind('<Left>', lambda e: self.show_prev_image_window())
        self.img_win.bind('<Right>', lambda e: self.show_next_image_window())
        self.img_win.bind('<Escape>', lambda e: self.img_win.destroy())
        self.img_win.bind('<space>', lambda e: self.toggle_favorite())
        self.img_win.bind("<Key>", self.on_keypress)

        self.show_image_window()

    def show_image_window(self):
        path = self.fullscreen_images[self.current_image_index]
        img = Image.open(path)
        
        # Apply EXIF orientation correction
        img = ImageOps.exif_transpose(img)
        
        self.img_win.update_idletasks()
        win_width = max(100, self.img_win.winfo_width())
        win_height = max(100, self.img_win.winfo_height())
        img = ImageOps.contain(img, (win_width, win_height))

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT starred, label FROM photos WHERE path=?", (path,))
        row = cur.fetchone()
        conn.close()

        starred = row[0] if row else 0
        label = row[1] if row and row[1] else ""
        self.current_label_text = label
        self.label_var.set(label)

        if starred:
            draw = ImageDraw.Draw(img)
            star_big = [(20,5),(30,25),(50,25),(35,38),
                        (40,60),(20,47),(0,60),(5,38),
                        (-10,25),(10,25)]
            draw.polygon(star_big, fill="yellow")

        self.tk_img_window = ImageTk.PhotoImage(img)
        self.img_label.configure(image=self.tk_img_window)

    def on_keypress(self, event):
        if event.keysym in ("Left", "Right", "Escape", "space"):
            return
        if event.keysym == "Return":
            self.save_label_for_current_image(self.current_label_text)
            self.set_status(f"Saved label: {self.current_label_text}")
            return
        if event.keysym == "BackSpace":
            self.current_label_text = self.current_label_text[:-1]
            self.label_var.set(self.current_label_text)
            return
        if event.char.isprintable():
            self.current_label_text += event.char
            self.label_var.set(self.current_label_text)

    def save_label_for_current_image(self, label):
        path = self.fullscreen_images[self.current_image_index]
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("UPDATE photos SET label=? WHERE path=?", (label, path))
        conn.commit()
        conn.close()

    def toggle_favorite(self):
        path = self.fullscreen_images[self.current_image_index]
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT starred FROM photos WHERE path=?", (path,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return

        new_value = 0 if row[0] else 1
        cur.execute("UPDATE photos SET starred=? WHERE path=?", (new_value, path))
        conn.commit()
        conn.close()

        lbl = self.thumb_refs.get(path)
        try:
            idx = self.fullscreen_images.index(path)
        except ValueError:
            idx = None

        if lbl:
            base = Image.open(path)
            
            # Apply EXIF orientation correction
            base = ImageOps.exif_transpose(base)
            
            base.thumbnail((150,150))

            if new_value:
                draw = ImageDraw.Draw(base)
                star = [(10,2),(14,10),(22,10),(16,15),
                        (18,23),(10,18),(2,23),(4,15),
                        (0,10),(8,10)]
                draw.polygon(star, fill="yellow")

            tkimg = ImageTk.PhotoImage(base)
            lbl.configure(image=tkimg)
            if idx is not None and 0 <= idx < len(self.thumbs):
                self.thumbs[idx] = tkimg

        self.show_image_window()
        self.set_status(f"{'Favorited' if new_value else 'Unfavorited'} {path}")

    def open_copy_starred_window(self):
        copy_win = tk.Toplevel(self.root)
        copy_win.title("Copy Starred Photos")
        copy_win.geometry("600x400")
        copy_win.transient(self.root)
        
        source_frame = ttk.LabelFrame(copy_win, text="Source Folder", padding=10)
        source_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.copy_source_var = tk.StringVar()
        if self.current_folder and os.path.isdir(self.current_folder):
            self.copy_source_var.set(self.current_folder)
        ttk.Entry(source_frame, textvariable=self.copy_source_var, state='readonly').pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,5))
        ttk.Button(source_frame, text="Browse...", command=self.select_copy_source).pack(side=tk.RIGHT)
        
        dest_frame = ttk.LabelFrame(copy_win, text="Destination Folder", padding=10)
        dest_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.copy_dest_var = tk.StringVar()
        ttk.Entry(dest_frame, textvariable=self.copy_dest_var, state='readonly').pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,5))
        ttk.Button(dest_frame, text="Browse...", command=self.select_copy_dest).pack(side=tk.RIGHT)
        
        options_frame = ttk.LabelFrame(copy_win, text="Options", padding=10)
        options_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.preserve_structure_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options_frame, text="Preserve folder structure", 
                       variable=self.preserve_structure_var).pack(anchor=tk.W)
        
        stats_frame = ttk.LabelFrame(copy_win, text="Progress", padding=10)
        stats_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        self.copy_progress_var = tk.StringVar(value="Ready to copy...")
        ttk.Label(stats_frame, textvariable=self.copy_progress_var, wraplength=550).pack(anchor=tk.W, pady=5)
        
        self.copy_stats_text = tk.Text(stats_frame, height=8, state='disabled', wrap='word')
        self.copy_stats_text.pack(fill=tk.BOTH, expand=True)
        
        button_frame = ttk.Frame(copy_win)
        button_frame.pack(fill=tk.X, padx=10, pady=10)
        
        self.copy_button = ttk.Button(button_frame, text="Start Copy", command=self.start_copy_starred)
        self.copy_button.pack(side=tk.LEFT, padx=5)
        
        ttk.Button(button_frame, text="Close", command=copy_win.destroy).pack(side=tk.RIGHT, padx=5)
        
        self.copy_window = copy_win

    def select_copy_source(self):
        folder = filedialog.askdirectory(title="Select Source Folder (with starred photos)")
        if folder:
            self.copy_source_var.set(folder)

    def select_copy_dest(self):
        folder = filedialog.askdirectory(title="Select Destination Folder")
        if folder:
            self.copy_dest_var.set(folder)

    def start_copy_starred(self):
        source = self.copy_source_var.get()
        dest = self.copy_dest_var.get()
        
        if not source:
            messagebox.showerror("Error", "Please select a source folder")
            return
        if not dest:
            messagebox.showerror("Error", "Please select a destination folder")
            return
        if not os.path.isdir(source):
            messagebox.showerror("Error", "Source folder does not exist")
            return
        if not os.path.isdir(dest):
            messagebox.showerror("Error", "Destination folder does not exist")
            return
            
        self.copy_button.config(state='disabled')
        
        threading.Thread(
            target=self.copy_starred_photos, 
            args=(source, dest, self.preserve_structure_var.get()),
            daemon=True
        ).start()

    def copy_starred_photos(self, source_folder, dest_folder, preserve_structure):
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            
            cur.execute("""
                SELECT path FROM photos 
                WHERE starred = 1 AND path LIKE ?
            """, (source_folder + '%',))
            
            starred_photos = [row[0] for row in cur.fetchall()]
            conn.close()
            
            total = len(starred_photos)
            
            if total == 0:
                self.update_copy_progress(f"No starred photos found in {source_folder}")
                self.update_copy_stats(f"Total starred photos found: 0\n")
                self.root.after(0, lambda: self.copy_button.config(state='normal'))
                return
            
            self.update_copy_progress(f"Found {total} starred photos. Starting copy...")
            self.update_copy_stats(f"Total starred photos found: {total}\n\n")
            
            copied = 0
            skipped = 0
            errors = 0
            
            for i, photo_path in enumerate(starred_photos, 1):
                if not os.path.exists(photo_path):
                    skipped += 1
                    self.update_copy_progress(f"Processing {i}/{total}: File not found, skipping...")
                    continue
                
                try:
                    if preserve_structure:
                        rel_path = os.path.relpath(photo_path, source_folder)
                        dest_path = os.path.join(dest_folder, rel_path)
                        dest_dir = os.path.dirname(dest_path)
                        
                        os.makedirs(dest_dir, exist_ok=True)
                    else:
                        filename = os.path.basename(photo_path)
                        dest_path = os.path.join(dest_folder, filename)
                        
                        if os.path.exists(dest_path):
                            base, ext = os.path.splitext(filename)
                            counter = 1
                            while os.path.exists(dest_path):
                                dest_path = os.path.join(dest_folder, f"{base}_{counter}{ext}")
                                counter += 1
                    
                    self.update_copy_progress(f"Copying {i}/{total}: {os.path.basename(photo_path)}")
                    shutil.copy2(photo_path, dest_path)
                    copied += 1
                    
                except Exception as e:
                    errors += 1
                    self.update_copy_stats(f"Error copying {photo_path}: {str(e)}\n")
            
            summary = f"\n{'='*50}\n"
            summary += f"COPY COMPLETE\n"
            summary += f"{'='*50}\n"
            summary += f"Total starred photos: {total}\n"
            summary += f"Successfully copied: {copied}\n"
            summary += f"Skipped (missing): {skipped}\n"
            summary += f"Errors: {errors}\n"
            summary += f"Destination: {dest_folder}\n"
            
            self.update_copy_progress(f"Complete! Copied {copied}/{total} photos")
            self.update_copy_stats(summary)
            
            self.root.after(0, lambda: self.copy_button.config(state='normal'))
            
            self.root.after(0, lambda: messagebox.showinfo(
                "Copy Complete", 
                f"Successfully copied {copied} out of {total} starred photos.\n\n"
                f"Skipped: {skipped}\nErrors: {errors}"
            ))
            
        except Exception as e:
            self.update_copy_progress(f"Error: {str(e)}")
            self.update_copy_stats(f"\nFatal error: {str(e)}\n")
            self.root.after(0, lambda: self.copy_button.config(state='normal'))
            self.root.after(0, lambda: messagebox.showerror("Error", f"Copy failed: {str(e)}"))

    def update_copy_progress(self, message):
        def _update():
            try:
                self.copy_progress_var.set(message)
            except:
                pass
        self.root.after(0, _update)

    def update_copy_stats(self, message):
        def _update():
            try:
                self.copy_stats_text.config(state='normal')
                self.copy_stats_text.insert(tk.END, message)
                self.copy_stats_text.see(tk.END)
                self.copy_stats_text.config(state='disabled')
            except:
                pass
        self.root.after(0, _update)

    def open_move_by_year_window(self):
        year_win = tk.Toplevel(self.root)
        year_win.title("Move Files by Year and Month")
        year_win.geometry("650x500")
        year_win.transient(self.root)
        
        source_frame = ttk.LabelFrame(year_win, text="Source Folder (will scan subfolders)", padding=10)
        source_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.year_source_var = tk.StringVar()
        if self.current_folder and os.path.isdir(self.current_folder):
            self.year_source_var.set(self.current_folder)
        ttk.Entry(source_frame, textvariable=self.year_source_var, state='readonly').pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,5))
        ttk.Button(source_frame, text="Browse...", command=self.select_year_source).pack(side=tk.RIGHT)
        
        year_frame = ttk.LabelFrame(year_win, text="Select Year", padding=10)
        year_frame.pack(fill=tk.X, padx=10, pady=5)
        
        year_inner = ttk.Frame(year_frame)
        year_inner.pack(fill=tk.X)
        
        ttk.Label(year_inner, text="Year:").pack(side=tk.LEFT, padx=(0,5))
        self.year_var = tk.StringVar(value="2024")
        year_spinbox = ttk.Spinbox(year_inner, from_=1990, to=2030, textvariable=self.year_var, width=10)
        year_spinbox.pack(side=tk.LEFT, padx=(0,10))
        
        ttk.Button(year_inner, text="Scan for Files", command=self.scan_photos_by_year).pack(side=tk.LEFT, padx=5)
        
        results_frame = ttk.LabelFrame(year_win, text="Files Found", padding=10)
        results_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        results_scroll = ttk.Scrollbar(results_frame)
        results_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.year_results_text = tk.Text(results_frame, height=10, state='disabled', 
                                         yscrollcommand=results_scroll.set)
        self.year_results_text.pack(fill=tk.BOTH, expand=True)
        results_scroll.config(command=self.year_results_text.yview)
        
        dest_frame = ttk.LabelFrame(year_win, text="Destination Folder", padding=10)
        dest_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.year_dest_var = tk.StringVar()
        ttk.Entry(dest_frame, textvariable=self.year_dest_var, state='readonly').pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,5))
        ttk.Button(dest_frame, text="Browse...", command=self.select_year_dest).pack(side=tk.RIGHT)
        
        button_frame = ttk.Frame(year_win)
        button_frame.pack(fill=tk.X, padx=10, pady=10)
        
        self.year_move_button = ttk.Button(button_frame, text="Move Files", 
                                           command=self.move_photos_by_year, state='disabled')
        self.year_move_button.pack(side=tk.LEFT, padx=5)
        
        ttk.Button(button_frame, text="Close", command=year_win.destroy).pack(side=tk.RIGHT, padx=5)
        
        self.year_window = year_win
        self.year_photos_found = []

    def select_year_source(self):
        folder = filedialog.askdirectory(title="Select Source Folder to Scan")
        if folder:
            self.year_source_var.set(folder)

    def select_year_dest(self):
        folder = filedialog.askdirectory(title="Select Destination Folder")
        if folder:
            self.year_dest_var.set(folder)

    def scan_photos_by_year(self):
        source = self.year_source_var.get()
        year = self.year_var.get()
        
        if not source:
            messagebox.showerror("Error", "Please select a source folder")
            return
        if not os.path.isdir(source):
            messagebox.showerror("Error", "Source folder does not exist")
            return
        
        self.year_results_text.config(state='normal')
        self.year_results_text.delete('1.0', tk.END)
        self.year_results_text.insert(tk.END, f"Scanning for files from year {year}...\n\n")
        self.year_results_text.config(state='disabled')
        self.year_photos_found = []
        self.year_move_button.config(state='disabled')
        
        threading.Thread(target=self._scan_year_thread, args=(source, year), daemon=True).start()

    def _scan_year_thread(self, source_folder, target_year):
        from PIL.ExifTags import TAGS
        import datetime
        
        image_extensions = self.get_supported_image_extensions()
        files_found = []
        
        try:
            for root_dir, dirs, files in os.walk(source_folder):
                for f in files:
                    full_path = os.path.join(root_dir, f)
                    
                    try:
                        file_year = None
                        file_month = None
                        
                        # Try EXIF for image files
                        if f.lower().endswith(image_extensions):
                            try:
                                img = Image.open(full_path)
                                exif_data = img._getexif()
                                
                                if exif_data:
                                    for tag_id, value in exif_data.items():
                                        tag = TAGS.get(tag_id, tag_id)
                                        if tag == "DateTimeOriginal" or tag == "DateTime":
                                            try:
                                                date_str = str(value).split()[0]
                                                parts = date_str.split(':')
                                                file_year = parts[0]
                                                file_month = parts[1]
                                                break
                                            except:
                                                pass
                            except:
                                pass
                        
                        # Fallback to file modification date for all files
                        if not file_year:
                            mod_time = os.path.getmtime(full_path)
                            dt = datetime.datetime.fromtimestamp(mod_time)
                            file_year = str(dt.year)
                            file_month = f"{dt.month:02d}"
                        
                        if file_year == target_year:
                            files_found.append((full_path, file_month))
                            month_name = datetime.datetime.strptime(file_month, "%m").strftime("%B")
                            self._update_year_results(f"✓ {full_path} ({month_name})\n")
                        
                    except Exception as e:
                        pass
            
            self.year_photos_found = files_found
            summary = f"\n{'='*60}\n"
            summary += f"SCAN COMPLETE\n"
            summary += f"Found {len(files_found)} files from year {target_year}\n"
            summary += f"{'='*60}\n"
            self._update_year_results(summary)
            
            if files_found:
                self.root.after(0, lambda: self.year_move_button.config(state='normal'))
            
        except Exception as e:
            self._update_year_results(f"\nError during scan: {str(e)}\n")

    def _update_year_results(self, message):
        def _update():
            try:
                self.year_results_text.config(state='normal')
                self.year_results_text.insert(tk.END, message)
                self.year_results_text.see(tk.END)
                self.year_results_text.config(state='disabled')
            except:
                pass
        self.root.after(0, _update)

    def move_photos_by_year(self):
        dest = self.year_dest_var.get()
        
        if not dest:
            messagebox.showerror("Error", "Please select a destination folder")
            return
        if not os.path.isdir(dest):
            messagebox.showerror("Error", "Destination folder does not exist")
            return
        if not self.year_photos_found:
            messagebox.showerror("Error", "No files to move. Please scan first.")
            return
        
        confirm1 = messagebox.askyesno(
            "Confirm Move - Step 1 of 2", 
            f"⚠️ WARNING ⚠️\n\n"
            f"You are about to MOVE {len(self.year_photos_found)} files.\n\n"
            f"Files will be organized into month folders within:\n"
            f"{dest}\n\n"
            f"Example: {dest}\\January\\, {dest}\\February\\, etc.\n\n"
            f"Do you want to continue?",
            icon='warning'
        )
        
        if not confirm1:
            return
        
        result = messagebox.askyesno(
            "Confirm Move - Step 2 of 2", 
            f"Are you absolutely sure?\n\n"
            f"Moving {len(self.year_photos_found)} files to month folders in:\n{dest}\n\n"
            f"This action cannot be easily undone.",
            icon='warning'
        )
        
        if not result:
            return
        
        self.year_move_button.config(state='disabled')
        
        threading.Thread(target=self._move_year_photos_thread, 
                        args=(self.year_photos_found, dest), daemon=True).start()

    def _move_year_photos_thread(self, file_list, dest_folder):
        import datetime
        moved = 0
        errors = 0
        
        self._update_year_results(f"\n\nStarting move to {dest_folder}...\n")
        
        for file_path, month_num in file_list:
            try:
                if not os.path.exists(file_path):
                    self._update_year_results(f"✗ File not found: {file_path}\n")
                    errors += 1
                    continue
                
                month_name = datetime.datetime.strptime(month_num, "%m").strftime("%B")
                month_dir = os.path.join(dest_folder, month_name)
                os.makedirs(month_dir, exist_ok=True)
                
                filename = os.path.basename(file_path)
                dest_path = os.path.join(month_dir, filename)
                
                if os.path.exists(dest_path):
                    base, ext = os.path.splitext(filename)
                    counter = 1
                    while os.path.exists(dest_path):
                        dest_path = os.path.join(month_dir, f"{base}_{counter}{ext}")
                        counter += 1
                
                shutil.move(file_path, dest_path)
                moved += 1
                self._update_year_results(f"→ Moved to {month_name}: {filename}\n")
                
            except Exception as e:
                self._update_year_results(f"✗ Error moving {file_path}: {str(e)}\n")
                errors += 1
        
        summary = f"\n{'='*60}\n"
        summary += f"MOVE COMPLETE\n"
        summary += f"Successfully moved: {moved}\n"
        summary += f"Errors: {errors}\n"
        summary += f"{'='*60}\n"
        self._update_year_results(summary)
        
        self.root.after(0, lambda: messagebox.showinfo(
            "Move Complete", 
            f"Successfully moved {moved} files into month folders.\nErrors: {errors}"
        ))

    def cleanup_orphan_thumbnails(self):
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("SELECT path FROM photos")
            rows = cur.fetchall()
            missing = [p for (p,) in rows if not os.path.exists(p)]

            deleted = 0
            if missing:
                for p in missing:
                    cur.execute("DELETE FROM photos WHERE path=?", (p,))
                    deleted += 1
                conn.commit()
            conn.close()
            return deleted
        except Exception as e:
            print("Error during cleanup_orphan_thumbnails:", e)
            return 0

    def vacuum_db(self):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("VACUUM")
            conn.close()
            self.set_status("VACUUM complete.")
        except Exception as e:
            print("Error during VACUUM:", e)
            self.set_status("VACUUM failed: " + str(e))

    def show_prev_image_window(self):
        if not self.fullscreen_images:
            return
        self.current_image_index = (self.current_image_index - 1) % len(self.fullscreen_images)
        self.show_image_window()

    def show_next_image_window(self):
        if not self.fullscreen_images:
            return
        self.current_image_index = (self.current_image_index + 1) % len(self.fullscreen_images)
        self.show_image_window()

    def open_copy_videos_window(self):
        video_win = tk.Toplevel(self.root)
        video_win.title("Copy Video Files")
        video_win.geometry("650x500")
        video_win.transient(self.root)
        
        source_frame = ttk.LabelFrame(video_win, text="Source Folder (will scan subfolders)", padding=10)
        source_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.video_source_var = tk.StringVar()
        if self.current_folder and os.path.isdir(self.current_folder):
            self.video_source_var.set(self.current_folder)
        ttk.Entry(source_frame, textvariable=self.video_source_var, state='readonly').pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,5))
        ttk.Button(source_frame, text="Browse...", command=self.select_video_source).pack(side=tk.RIGHT)
        
        scan_frame = ttk.Frame(video_win)
        scan_frame.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Button(scan_frame, text="Scan for Video Files", command=self.scan_video_files).pack(side=tk.LEFT, padx=5)
        
        results_frame = ttk.LabelFrame(video_win, text="Video Files Found", padding=10)
        results_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        results_scroll = ttk.Scrollbar(results_frame)
        results_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.video_results_text = tk.Text(results_frame, height=10, state='disabled', 
                                         yscrollcommand=results_scroll.set)
        self.video_results_text.pack(fill=tk.BOTH, expand=True)
        results_scroll.config(command=self.video_results_text.yview)
        
        dest_frame = ttk.LabelFrame(video_win, text="Destination Folder", padding=10)
        dest_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.video_dest_var = tk.StringVar()
        ttk.Entry(dest_frame, textvariable=self.video_dest_var, state='readonly').pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,5))
        ttk.Button(dest_frame, text="Browse...", command=self.select_video_dest).pack(side=tk.RIGHT)
        
        button_frame = ttk.Frame(video_win)
        button_frame.pack(fill=tk.X, padx=10, pady=10)
        
        self.video_copy_button = ttk.Button(button_frame, text="Copy Videos", 
                                           command=self.copy_video_files, state='disabled')
        self.video_copy_button.pack(side=tk.LEFT, padx=5)
        
        ttk.Button(button_frame, text="Close", command=video_win.destroy).pack(side=tk.RIGHT, padx=5)
        
        self.video_window = video_win
        self.video_files_found = []

    def select_video_source(self):
        folder = filedialog.askdirectory(title="Select Source Folder to Scan")
        if folder:
            self.video_source_var.set(folder)

    def select_video_dest(self):
        folder = filedialog.askdirectory(title="Select Destination Folder")
        if folder:
            self.video_dest_var.set(folder)

    def scan_video_files(self):
        source = self.video_source_var.get()
        
        if not source:
            messagebox.showerror("Error", "Please select a source folder")
            return
        if not os.path.isdir(source):
            messagebox.showerror("Error", "Source folder does not exist")
            return
        
        self.video_results_text.config(state='normal')
        self.video_results_text.delete('1.0', tk.END)
        self.video_results_text.insert(tk.END, f"Scanning for video files...\n\n")
        self.video_results_text.config(state='disabled')
        self.video_files_found = []
        self.video_copy_button.config(state='disabled')
        
        threading.Thread(target=self._scan_videos_thread, args=(source,), daemon=True).start()

    def _scan_videos_thread(self, source_folder):
        video_extensions = (
            # Common formats
            '.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv', '.m4v', '.mpg', '.mpeg', 
            '.3gp', '.webm', '.ogv',
            # Additional MPEG variants
            '.m2v', '.m2ts', '.mts', '.ts',
            # QuickTime
            '.qt',
            # RealMedia
            '.rm', '.rmvb',
            # DivX/Xvid
            '.divx',
            # VOB (DVD)
            '.vob',
            # Apple formats
            '.m4p',
            # Other formats
            '.asf', '.f4v', '.f4p', '.f4a', '.f4b'
        )
        videos_found = []
        
        try:
            for root_dir, dirs, files in os.walk(source_folder):
                for f in files:
                    if f.lower().endswith(video_extensions):
                        full_path = os.path.join(root_dir, f)
                        
                        try:
                            file_size = os.path.getsize(full_path)
                            size_mb = file_size / (1024 * 1024)
                            
                            videos_found.append(full_path)
                            self._update_video_results(f"✓ {full_path} ({size_mb:.2f} MB)\n")
                            
                        except Exception as e:
                            pass
            
            self.video_files_found = videos_found
            
            total_size = sum(os.path.getsize(f) for f in videos_found if os.path.exists(f))
            total_size_gb = total_size / (1024 * 1024 * 1024)
            
            summary = f"\n{'='*60}\n"
            summary += f"SCAN COMPLETE\n"
            summary += f"Found {len(videos_found)} video files\n"
            summary += f"Total size: {total_size_gb:.2f} GB\n"
            summary += f"{'='*60}\n"
            self._update_video_results(summary)
            
            if videos_found:
                self.root.after(0, lambda: self.video_copy_button.config(state='normal'))
            
        except Exception as e:
            self._update_video_results(f"\nError during scan: {str(e)}\n")

    def _update_video_results(self, message):
        def _update():
            try:
                self.video_results_text.config(state='normal')
                self.video_results_text.insert(tk.END, message)
                self.video_results_text.see(tk.END)
                self.video_results_text.config(state='disabled')
            except:
                pass
        self.root.after(0, _update)

    def copy_video_files(self):
        dest = self.video_dest_var.get()
        
        if not dest:
            messagebox.showerror("Error", "Please select a destination folder")
            return
        if not os.path.isdir(dest):
            messagebox.showerror("Error", "Destination folder does not exist")
            return
        if not self.video_files_found:
            messagebox.showerror("Error", "No video files to copy. Please scan first.")
            return
        
        total_size = sum(os.path.getsize(f) for f in self.video_files_found if os.path.exists(f))
        total_size_gb = total_size / (1024 * 1024 * 1024)
        
        result = messagebox.askyesno(
            "Confirm Copy", 
            f"Copy {len(self.video_files_found)} video files to:\n{dest}\n\n"
            f"Total size: {total_size_gb:.2f} GB\n\n"
            f"This may take some time. Continue?",
            icon='question'
        )
        
        if not result:
            return
        
        self.video_copy_button.config(state='disabled')
        
        threading.Thread(target=self._copy_videos_thread, 
                        args=(self.video_files_found, dest), daemon=True).start()

    def _copy_videos_thread(self, video_list, dest_folder):
        copied = 0
        errors = 0
        total_copied_size = 0
        
        self._update_video_results(f"\n\nStarting copy to {dest_folder}...\n")
        
        for video_path in video_list:
            try:
                if not os.path.exists(video_path):
                    self._update_video_results(f"✗ File not found: {video_path}\n")
                    errors += 1
                    continue
                
                filename = os.path.basename(video_path)
                dest_path = os.path.join(dest_folder, filename)
                
                if os.path.exists(dest_path):
                    base, ext = os.path.splitext(filename)
                    counter = 1
                    while os.path.exists(dest_path):
                        dest_path = os.path.join(dest_folder, f"{base}_{counter}{ext}")
                        counter += 1
                
                file_size = os.path.getsize(video_path)
                size_mb = file_size / (1024 * 1024)
                
                shutil.copy2(video_path, dest_path)
                copied += 1
                total_copied_size += file_size
                self._update_video_results(f"→ Copied: {filename} ({size_mb:.2f} MB)\n")
                
            except Exception as e:
                self._update_video_results(f"✗ Error copying {video_path}: {str(e)}\n")
                errors += 1
        
        total_gb = total_copied_size / (1024 * 1024 * 1024)
        
        summary = f"\n{'='*60}\n"
        summary += f"COPY COMPLETE\n"
        summary += f"Successfully copied: {copied} files\n"
        summary += f"Total copied: {total_gb:.2f} GB\n"
        summary += f"Errors: {errors}\n"
        summary += f"{'='*60}\n"
        self._update_video_results(summary)
        
        self.root.after(0, lambda: messagebox.showinfo(
            "Copy Complete", 
            f"Successfully copied {copied} video files ({total_gb:.2f} GB).\nErrors: {errors}"
        ))
        
        self.root.after(0, lambda: self.video_copy_button.config(state='normal'))

    def open_duplicate_finder_window(self):
        """Opens window to find and manage duplicate photos"""
        dup_win = tk.Toplevel(self.root)
        dup_win.title("Find Duplicate Photos")
        dup_win.geometry("1000x750")
        dup_win.transient(self.root)
        
        # Mode selection
        mode_frame = ttk.LabelFrame(dup_win, text="Detection Mode", padding=10)
        mode_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.dup_mode_var = tk.StringVar(value="compare")
        ttk.Radiobutton(mode_frame, text="Compare two folders (find duplicates between new and existing photos)", 
                       variable=self.dup_mode_var, value="compare", 
                       command=self.update_duplicate_mode).pack(anchor=tk.W, pady=2)
        ttk.Radiobutton(mode_frame, text="Find duplicates within a single folder", 
                       variable=self.dup_mode_var, value="single", 
                       command=self.update_duplicate_mode).pack(anchor=tk.W, pady=2)
        
        # Container for folder selection (so we can manage packing order)
        self.folder_container = ttk.Frame(dup_win)
        self.folder_container.pack(fill=tk.X, padx=10, pady=5)
        
        # New photos folder
        self.new_frame = ttk.LabelFrame(self.folder_container, text="New Photos Folder (to check for duplicates)", padding=10)
        
        self.dup_new_var = tk.StringVar()
        ttk.Entry(self.new_frame, textvariable=self.dup_new_var, state='readonly').pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,5))
        ttk.Button(self.new_frame, text="Browse...", command=self.select_dup_new).pack(side=tk.RIGHT)
        
        # Existing photos folder
        self.existing_frame = ttk.LabelFrame(self.folder_container, text="Existing Photos Folder (your current library)", padding=10)
        
        self.dup_existing_var = tk.StringVar()
        if self.photo_root and os.path.isdir(self.photo_root):
            self.dup_existing_var.set(self.photo_root)
        ttk.Entry(self.existing_frame, textvariable=self.dup_existing_var, state='readonly').pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,5))
        ttk.Button(self.existing_frame, text="Browse...", command=self.select_dup_existing).pack(side=tk.RIGHT)
        
        # Single folder (for single mode)
        self.single_frame = ttk.LabelFrame(self.folder_container, text="Folder to Scan for Duplicates", padding=10)
        
        self.dup_single_var = tk.StringVar()
        if self.current_folder and os.path.isdir(self.current_folder):
            self.dup_single_var.set(self.current_folder)
        ttk.Entry(self.single_frame, textvariable=self.dup_single_var, state='readonly').pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,5))
        ttk.Button(self.single_frame, text="Browse...", command=self.select_dup_single).pack(side=tk.RIGHT)
        
        # Scan button
        scan_frame = ttk.Frame(dup_win)
        scan_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.dup_scan_button = ttk.Button(scan_frame, text="Scan for Duplicates", command=self.scan_for_duplicates)
        self.dup_scan_button.pack(side=tk.LEFT, padx=5)
        
        self.dup_status_var = tk.StringVar(value="Select folders and click 'Scan for Duplicates'")
        ttk.Label(scan_frame, textvariable=self.dup_status_var).pack(side=tk.LEFT, padx=10)
        
        # Results frame with scrollbar
        results_frame = ttk.LabelFrame(dup_win, text="Duplicates Found", padding=10)
        results_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # Canvas and scrollbar for results
        canvas = tk.Canvas(results_frame)
        scrollbar = ttk.Scrollbar(results_frame, orient=tk.VERTICAL, command=canvas.yview)
        self.dup_results_frame = ttk.Frame(canvas)
        
        canvas.create_window((0, 0), window=self.dup_results_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        self.dup_results_frame.bind("<Configure>", 
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        
        # Bottom buttons
        button_frame = ttk.Frame(dup_win)
        button_frame.pack(fill=tk.X, padx=10, pady=10)
        
        self.dup_delete_button = ttk.Button(button_frame, text="Delete Selected Duplicates", 
                                           command=self.delete_selected_duplicates, state='disabled')
        self.dup_delete_button.pack(side=tk.LEFT, padx=5)
        
        ttk.Button(button_frame, text="Close", command=dup_win.destroy).pack(side=tk.RIGHT, padx=5)
        
        self.dup_window = dup_win
        self.dup_checkboxes = []  # Store checkbox variables and paths
        
        # Set initial mode
        self.update_duplicate_mode()
    
    def update_duplicate_mode(self):
        """Update UI based on selected duplicate detection mode"""
        mode = self.dup_mode_var.get()
        
        # Hide all frames first
        self.new_frame.pack_forget()
        self.existing_frame.pack_forget()
        self.single_frame.pack_forget()
        
        if mode == "compare":
            # Show two folder selection
            self.new_frame.pack(fill=tk.X, pady=(0,5))
            self.existing_frame.pack(fill=tk.X, pady=(0,5))
        else:  # single
            # Show single folder selection
            self.single_frame.pack(fill=tk.X, pady=(0,5))
    
    def select_dup_single(self):
        folder = filedialog.askdirectory(title="Select Folder to Scan for Duplicates")
        if folder:
            self.dup_single_var.set(folder)
    
    def select_dup_new(self):
        folder = filedialog.askdirectory(title="Select New Photos Folder")
        if folder:
            self.dup_new_var.set(folder)
    
    def select_dup_existing(self):
        folder = filedialog.askdirectory(title="Select Existing Photos Folder")
        if folder:
            self.dup_existing_var.set(folder)
    
    def scan_for_duplicates(self):
        mode = self.dup_mode_var.get()
        
        if mode == "compare":
            new_folder = self.dup_new_var.get()
            existing_folder = self.dup_existing_var.get()
            
            if not new_folder or not existing_folder:
                messagebox.showerror("Error", "Please select both folders")
                return
            if not os.path.isdir(new_folder):
                messagebox.showerror("Error", "New photos folder does not exist")
                return
            if not os.path.isdir(existing_folder):
                messagebox.showerror("Error", "Existing photos folder does not exist")
                return
            
            self.dup_scan_button.config(state='disabled')
            self.dup_delete_button.config(state='disabled')
            self.dup_status_var.set("Scanning for duplicates...")
            
            threading.Thread(target=self._scan_duplicates_compare, 
                            args=(new_folder, existing_folder), daemon=True).start()
        
        else:  # single mode
            single_folder = self.dup_single_var.get()
            
            if not single_folder:
                messagebox.showerror("Error", "Please select a folder")
                return
            if not os.path.isdir(single_folder):
                messagebox.showerror("Error", "Folder does not exist")
                return
            
            self.dup_scan_button.config(state='disabled')
            self.dup_delete_button.config(state='disabled')
            self.dup_status_var.set("Scanning for duplicates within folder...")
            
            threading.Thread(target=self._scan_duplicates_single, 
                            args=(single_folder,), daemon=True).start()
    
    def _scan_duplicates_compare(self, new_folder, existing_folder):
        """Scan for duplicate photos using file hash comparison from database"""
        import datetime
        
        # Normalize paths to use system separators
        new_folder = os.path.normpath(new_folder)
        existing_folder = os.path.normpath(existing_folder)
        
        # Build hash dictionary from database for existing photos
        self.root.after(0, lambda: self.dup_status_var.set("Loading existing photo hashes from database..."))
        
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        
        # Get all hashes and paths for photos in existing folder
        cur.execute("""
            SELECT file_hash, path, thumbnail FROM photos 
            WHERE folder LIKE ? AND file_hash IS NOT NULL
        """, (existing_folder + '%',))
        
        existing_hashes = {}
        existing_thumbnails = {}
        for file_hash, path, thumbnail in cur.fetchall():
            if file_hash:
                existing_hashes[file_hash] = path
                if thumbnail:
                    existing_thumbnails[path] = thumbnail
        
        if len(existing_hashes) == 0:
            cur.execute("SELECT DISTINCT folder FROM photos LIMIT 5")
            sample_folders = [row[0] for row in cur.fetchall()]
            error_msg = f"No scanned photos found in existing folder:\n{existing_folder}\n\n"
            error_msg += f"Sample folders in database:\n"
            error_msg += "\n".join(sample_folders) if sample_folders else "(No folders found)"
            error_msg += "\n\nPlease scan this folder first."
            
            self.root.after(0, lambda: self.dup_status_var.set("No photos found in existing folder"))
            self.root.after(0, lambda: messagebox.showwarning("No Photos Found", error_msg))
            self.root.after(0, lambda: self.dup_scan_button.config(state='normal'))
            conn.close()
            return
        
        self.root.after(0, lambda: self.dup_status_var.set(f"Loaded {len(existing_hashes)} hashes from existing folder"))
        
        # Check new photos against existing - get file info from database
        self.root.after(0, lambda: self.dup_status_var.set("Checking new photos for duplicates..."))
        duplicates = []
        
        # Get path, hash, and thumbnail for new photos
        cur.execute("""
            SELECT path, file_hash, thumbnail FROM photos 
            WHERE folder LIKE ? AND file_hash IS NOT NULL
        """, (new_folder + '%',))
        
        new_photos = cur.fetchall()
        
        if len(new_photos) == 0:
            cur.execute("SELECT DISTINCT folder FROM photos LIMIT 5")
            sample_folders = [row[0] for row in cur.fetchall()]
            error_msg = f"No scanned photos found in new folder:\n{new_folder}\n\n"
            error_msg += f"Sample folders in database:\n"
            error_msg += "\n".join(sample_folders) if sample_folders else "(No folders found)"
            error_msg += "\n\nPlease scan this folder first."
            
            self.root.after(0, lambda: self.dup_status_var.set("No photos found in new folder"))
            self.root.after(0, lambda: messagebox.showwarning("No Photos Found", error_msg))
            self.root.after(0, lambda: self.dup_scan_button.config(state='normal'))
            conn.close()
            return
        
        self.root.after(0, lambda: self.dup_status_var.set(f"Checking {len(new_photos)} photos for duplicates..."))
        
        # Build list of duplicate hashes first (fast) and store thumbnails
        duplicate_hashes = {}
        new_thumbnails = {}
        
        for path, file_hash, thumbnail in new_photos:
            if thumbnail:
                new_thumbnails[path] = thumbnail
            if file_hash and file_hash in existing_hashes:
                duplicate_hashes[path] = (file_hash, existing_hashes[file_hash])
        
        # Get file info from database where possible
        self.root.after(0, lambda: self.dup_status_var.set(f"Getting file info for {len(duplicate_hashes)} duplicates..."))
        
        processed = 0
        for new_path, (file_hash, existing_path) in duplicate_hashes.items():
            processed += 1
            self.root.after(0, lambda p=processed, t=len(duplicate_hashes): 
                           self.dup_status_var.set(f"Getting file info ({p}/{t})..."))
            
            try:
                # Try to get info quickly from os.stat
                try:
                    new_stat = os.stat(new_path)
                    new_size = new_stat.st_size
                    new_modified = datetime.datetime.fromtimestamp(new_stat.st_mtime)
                except:
                    new_size = 0
                    new_modified = datetime.datetime.now()
                
                try:
                    existing_stat = os.stat(existing_path)
                    existing_size = existing_stat.st_size
                    existing_modified = datetime.datetime.fromtimestamp(existing_stat.st_mtime)
                except:
                    existing_size = 0
                    existing_modified = datetime.datetime.now()
                
                new_info = {
                    'path': new_path,
                    'size': new_size,
                    'modified': new_modified,
                    'size_mb': new_size / (1024 * 1024) if new_size > 0 else 0,
                    'thumbnail': new_thumbnails.get(new_path)
                }
                
                existing_info = {
                    'path': existing_path,
                    'size': existing_size,
                    'modified': existing_modified,
                    'size_mb': existing_size / (1024 * 1024) if existing_size > 0 else 0,
                    'thumbnail': existing_thumbnails.get(existing_path)
                }
                
                duplicates.append({
                    'new': new_info,
                    'existing': existing_info
                })
            except Exception as e:
                print(f"Error getting file info: {e}")
        
        conn.close()
        
        # Display results
        self.root.after(0, lambda: self.dup_status_var.set(f"Loading thumbnails for {len(duplicates)} duplicate pairs..."))
        self.root.after(0, lambda: self._display_duplicates(duplicates))
    
    def _scan_duplicates_single(self, folder):
        """Scan for duplicate photos within a single folder"""
        import datetime
        
        # Normalize path to use system separators
        folder = os.path.normpath(folder)
        
        self.root.after(0, lambda: self.dup_status_var.set("Loading hashes from database..."))
        
        # Get all photos in folder from database with their hashes
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        
        cur.execute("""
            SELECT path, file_hash, thumbnail FROM photos 
            WHERE folder LIKE ? AND file_hash IS NOT NULL
        """, (folder + '%',))
        
        # Build hash dictionary: hash -> list of (path, thumbnail) tuples
        hash_dict = {}
        for path, file_hash, thumbnail in cur.fetchall():
            if file_hash:
                if file_hash not in hash_dict:
                    hash_dict[file_hash] = []
                hash_dict[file_hash].append((path, thumbnail))
        
        conn.close()
        
        self.root.after(0, lambda: self.dup_status_var.set(f"Loaded {len(hash_dict)} unique hashes from database"))
        
        # Find duplicates (hashes with more than one file) - in memory, very fast
        self.root.after(0, lambda: self.dup_status_var.set("Identifying duplicates..."))
        duplicate_groups = []
        
        for file_hash, path_thumb_list in hash_dict.items():
            if len(path_thumb_list) > 1:
                # Multiple files with same hash = duplicates
                duplicate_groups.append(path_thumb_list)
        
        # Now get file info only for duplicates
        total_duplicates = sum(len(group) for group in duplicate_groups)
        self.root.after(0, lambda: self.dup_status_var.set(f"Getting file info for {total_duplicates} duplicate photos..."))
        
        duplicates = []
        processed = 0
        
        for file_hash, path_thumb_list in hash_dict.items():
            if len(path_thumb_list) > 1:
                # Multiple files with same hash = duplicates
                # Treat first as "existing", rest as "new" (to delete)
                existing_path, existing_thumbnail = path_thumb_list[0]
            
                processed += 1
                self.root.after(0, lambda p=processed, t=total_duplicates: 
                               self.dup_status_var.set(f"Getting file info ({p}/{t})..."))
            
                try:
                    existing_stat = os.stat(existing_path)
                    existing_info = {
                        'path': existing_path,
                        'size': existing_stat.st_size,
                        'modified': datetime.datetime.fromtimestamp(existing_stat.st_mtime),
                        'size_mb': existing_stat.st_size / (1024 * 1024),
                        'thumbnail': existing_thumbnail
                    }
                except:
                    existing_info = {
                        'path': existing_path,
                        'size': 0,
                        'modified': datetime.datetime.now(),
                        'size_mb': 0,
                        'thumbnail': existing_thumbnail
                    }
            
                for dup_path, dup_thumbnail in path_thumb_list[1:]:
                    processed += 1
                    self.root.after(0, lambda p=processed, t=total_duplicates: 
                                   self.dup_status_var.set(f"Getting file info ({p}/{t})..."))
                
                    try:
                        dup_stat = os.stat(dup_path)
                        dup_info = {
                            'path': dup_path,
                            'size': dup_stat.st_size,
                            'modified': datetime.datetime.fromtimestamp(dup_stat.st_mtime),
                            'size_mb': dup_stat.st_size / (1024 * 1024),
                            'thumbnail': dup_thumbnail
                        }
                    except:
                        dup_info = {
                            'path': dup_path,
                            'size': 0,
                            'modified': datetime.datetime.now(),
                            'size_mb': 0,
                            'thumbnail': dup_thumbnail
                        }
                
                    if existing_info and dup_info:
                        duplicates.append({
                            'new': dup_info,
                            'existing': existing_info
                        })
        
        # Display results
        self.root.after(0, lambda: self.dup_status_var.set(f"Loading thumbnails for {len(duplicates)} duplicate pairs..."))
        self.root.after(0, lambda: self._display_duplicates(duplicates))
    
    def _display_duplicates(self, duplicates):
        """Display duplicate photos in the results frame"""
        # Clear previous results
        for widget in self.dup_results_frame.winfo_children():
            widget.destroy()
        
        self.dup_checkboxes = []
        
        if not duplicates:
            self.dup_status_var.set("No duplicates found!")
            ttk.Label(self.dup_results_frame, text="No duplicate photos were found.", 
                     font=('Arial', 12)).pack(pady=20)
            self.dup_scan_button.config(state='normal')
            return
        
        self.dup_status_var.set(f"Found {len(duplicates)} duplicate(s)")
        
        # Determine mode for header labels
        mode = self.dup_mode_var.get()
        
        # Header
        header_frame = ttk.Frame(self.dup_results_frame)
        header_frame.pack(fill=tk.X, pady=(0, 10))
        
        if mode == "single":
            ttk.Label(header_frame, text="Delete?", font=('Arial', 10, 'bold')).grid(row=0, column=0, padx=5)
            ttk.Label(header_frame, text="Photo 1", font=('Arial', 10, 'bold')).grid(row=0, column=1, padx=20)
            ttk.Label(header_frame, text="", font=('Arial', 10, 'bold')).grid(row=0, column=2, padx=20)
            ttk.Label(header_frame, text="Delete?", font=('Arial', 10, 'bold')).grid(row=0, column=3, padx=5)
            ttk.Label(header_frame, text="Photo 2", font=('Arial', 10, 'bold')).grid(row=0, column=4, padx=20)
        else:
            ttk.Label(header_frame, text="Delete?", font=('Arial', 10, 'bold')).grid(row=0, column=0, padx=5)
            ttk.Label(header_frame, text="New Photo", font=('Arial', 10, 'bold')).grid(row=0, column=1, padx=20)
            ttk.Label(header_frame, text="", font=('Arial', 10, 'bold')).grid(row=0, column=2, padx=20)
            ttk.Label(header_frame, text="Existing Photo", font=('Arial', 10, 'bold')).grid(row=0, column=3, padx=20)
        
        ttk.Separator(self.dup_results_frame, orient='horizontal').pack(fill=tk.X, pady=5)
        
        # Display each duplicate pair
        for idx, dup in enumerate(duplicates):
            if mode == "single":
                self._create_duplicate_row_single(idx, dup)
            else:
                self._create_duplicate_row_compare(idx, dup)
        
        self.dup_scan_button.config(state='normal')
        self.dup_delete_button.config(state='normal')
    
    def _create_duplicate_row_compare(self, idx, dup):
        """Create a row showing a duplicate pair for compare mode"""
        row_frame = ttk.Frame(self.dup_results_frame)
        row_frame.pack(fill=tk.X, pady=10, padx=5)
        
        # Checkbox for deletion (new photo side only)
        delete_var = tk.BooleanVar(value=True)  # Default to checked
        checkbox = ttk.Checkbutton(row_frame, variable=delete_var)
        checkbox.grid(row=0, column=0, padx=5, sticky='n')
        
        self.dup_checkboxes.append({
            'var': delete_var,
            'path': dup['new']['path']
        })
        
        # New photo info
        new_frame = ttk.Frame(row_frame, relief='solid', borderwidth=1)
        new_frame.grid(row=0, column=1, padx=10, sticky='nsew')
        
        # Try to load thumbnail
        try:
            # Use thumbnail from database if available
            if dup['new'].get('thumbnail'):
                img = Image.open(io.BytesIO(dup['new']['thumbnail']))
            else:
                # Fallback to loading from file
                img = Image.open(dup['new']['path'])
                img = ImageOps.exif_transpose(img)
                img.thumbnail((150, 150))
            
            photo = ImageTk.PhotoImage(img)
            lbl = ttk.Label(new_frame, image=photo)
            lbl.image = photo  # Keep reference
            lbl.pack(pady=5)
        except:
            ttk.Label(new_frame, text="[Image]", width=20).pack(pady=5)
        
        ttk.Label(new_frame, text=f"File: {os.path.basename(dup['new']['path'])}", 
                 wraplength=200).pack()
        ttk.Label(new_frame, text=f"Size: {dup['new']['size_mb']:.2f} MB").pack()
        ttk.Label(new_frame, text=f"Modified: {dup['new']['modified'].strftime('%Y-%m-%d %H:%M')}").pack(pady=(0,5))
        
        # Arrow/equals sign
        ttk.Label(row_frame, text="=", font=('Arial', 20)).grid(row=0, column=2, padx=10)
        
        # Existing photo info (no checkbox)
        existing_frame = ttk.Frame(row_frame, relief='solid', borderwidth=1)
        existing_frame.grid(row=0, column=3, padx=10, sticky='nsew')
        
        # Try to load thumbnail
        try:
            # Use thumbnail from database if available
            if dup['existing'].get('thumbnail'):
                img = Image.open(io.BytesIO(dup['existing']['thumbnail']))
            else:
                # Fallback to loading from file
                img = Image.open(dup['existing']['path'])
                img = ImageOps.exif_transpose(img)
                img.thumbnail((150, 150))
            
            photo = ImageTk.PhotoImage(img)
            lbl = ttk.Label(existing_frame, image=photo)
            lbl.image = photo  # Keep reference
            lbl.pack(pady=5)
        except:
            ttk.Label(existing_frame, text="[Image]", width=20).pack(pady=5)
        
        ttk.Label(existing_frame, text=f"File: {os.path.basename(dup['existing']['path'])}", 
                 wraplength=200).pack()
        ttk.Label(existing_frame, text=f"Size: {dup['existing']['size_mb']:.2f} MB").pack()
        ttk.Label(existing_frame, text=f"Modified: {dup['existing']['modified'].strftime('%Y-%m-%d %H:%M')}").pack(pady=(0,5))
        
        # Separator
        ttk.Separator(self.dup_results_frame, orient='horizontal').pack(fill=tk.X, pady=10)
    
    def _create_duplicate_row_single(self, idx, dup):
        """Create a row showing a duplicate pair for single folder mode with checkboxes on both sides"""
        row_frame = ttk.Frame(self.dup_results_frame)
        row_frame.pack(fill=tk.X, pady=10, padx=5)
        
        # Radio button variable to track which photo to keep (shared between both photos)
        keep_var = tk.StringVar(value="existing")  # Default keep first photo
        
        # Checkbox and Photo 1 (existing/first)
        delete_existing_var = tk.BooleanVar(value=False)  # Default NOT checked
        
        # Link radio button to checkbox
        def update_existing_checkbox():
            delete_existing_var.set(keep_var.get() == "new")
        
        checkbox1_frame = ttk.Frame(row_frame)
        checkbox1_frame.grid(row=0, column=0, padx=5, sticky='n')
        
        ttk.Radiobutton(checkbox1_frame, variable=keep_var, value="existing", 
                       command=update_existing_checkbox).pack()
        checkbox1 = ttk.Checkbutton(checkbox1_frame, variable=delete_existing_var, state='disabled')
        checkbox1.pack()
        
        # Photo 1 info
        photo1_frame = ttk.Frame(row_frame, relief='solid', borderwidth=1)
        photo1_frame.grid(row=0, column=1, padx=10, sticky='nsew')
        
        # Try to load thumbnail
        try:
            # Use thumbnail from database if available
            if dup['existing'].get('thumbnail'):
                img = Image.open(io.BytesIO(dup['existing']['thumbnail']))
            else:
                # Fallback to loading from file
                img = Image.open(dup['existing']['path'])
                img = ImageOps.exif_transpose(img)
                img.thumbnail((150, 150))
            
            photo = ImageTk.PhotoImage(img)
            lbl = ttk.Label(photo1_frame, image=photo)
            lbl.image = photo
            lbl.pack(pady=5)
        except:
            ttk.Label(photo1_frame, text="[Image]", width=20).pack(pady=5)
        
        ttk.Label(photo1_frame, text=f"File: {os.path.basename(dup['existing']['path'])}", 
                 wraplength=200).pack()
        ttk.Label(photo1_frame, text=f"Size: {dup['existing']['size_mb']:.2f} MB").pack()
        ttk.Label(photo1_frame, text=f"Modified: {dup['existing']['modified'].strftime('%Y-%m-%d %H:%M')}").pack()
        
        # Show path with right-aligned text
        path_text = dup['existing']['path']
        if len(path_text) > 40:
            path_text = "..." + path_text[-40:]
        ttk.Label(photo1_frame, text=f"Path: {path_text}", 
                 wraplength=200, font=('Arial', 8), anchor='e').pack(pady=(5,5))
        
        # Arrow/equals sign
        ttk.Label(row_frame, text="=", font=('Arial', 20)).grid(row=0, column=2, padx=10)
        
        # Checkbox and Photo 2 (new/second)
        delete_new_var = tk.BooleanVar(value=True)  # Default checked
        
        # Link radio button to checkbox
        def update_new_checkbox():
            delete_new_var.set(keep_var.get() == "existing")
        
        checkbox2_frame = ttk.Frame(row_frame)
        checkbox2_frame.grid(row=0, column=3, padx=5, sticky='n')
        
        ttk.Radiobutton(checkbox2_frame, variable=keep_var, value="new", 
                       command=update_new_checkbox).pack()
        checkbox2 = ttk.Checkbutton(checkbox2_frame, variable=delete_new_var, state='disabled')
        checkbox2.pack()
        
        # Photo 2 info
        photo2_frame = ttk.Frame(row_frame, relief='solid', borderwidth=1)
        photo2_frame.grid(row=0, column=4, padx=10, sticky='nsew')
        
        # Try to load thumbnail
        try:
            # Use thumbnail from database if available
            if dup['new'].get('thumbnail'):
                img = Image.open(io.BytesIO(dup['new']['thumbnail']))
            else:
                # Fallback to loading from file
                img = Image.open(dup['new']['path'])
                img = ImageOps.exif_transpose(img)
                img.thumbnail((150, 150))
            
            photo = ImageTk.PhotoImage(img)
            lbl = ttk.Label(photo2_frame, image=photo)
            lbl.image = photo
            lbl.pack(pady=5)
        except:
            ttk.Label(photo2_frame, text="[Image]", width=20).pack(pady=5)
        
        ttk.Label(photo2_frame, text=f"File: {os.path.basename(dup['new']['path'])}", 
                 wraplength=200).pack()
        ttk.Label(photo2_frame, text=f"Size: {dup['new']['size_mb']:.2f} MB").pack()
        ttk.Label(photo2_frame, text=f"Modified: {dup['new']['modified'].strftime('%Y-%m-%d %H:%M')}").pack()
        
        # Show path with right-aligned text
        path_text = dup['new']['path']
        if len(path_text) > 40:
            path_text = "..." + path_text[-40:]
        ttk.Label(photo2_frame, text=f"Path: {path_text}", 
                 wraplength=200, font=('Arial', 8), anchor='e').pack(pady=(5,5))
        
        # Store which photo to delete based on radio button selection
        self.dup_checkboxes.append({
            'var': delete_existing_var,
            'path': dup['existing']['path'],
            'keep_var': keep_var,
            'keep_value': 'new'  # Delete this if 'new' is selected
        })
        
        self.dup_checkboxes.append({
            'var': delete_new_var,
            'path': dup['new']['path'],
            'keep_var': keep_var,
            'keep_value': 'existing'  # Delete this if 'existing' is selected
        })
        
        # Initialize checkbox states
        update_existing_checkbox()
        update_new_checkbox()
        
        # Separator
        ttk.Separator(self.dup_results_frame, orient='horizontal').pack(fill=tk.X, pady=10)
    
    def delete_selected_duplicates(self):
        """Delete all checked duplicate photos after confirmation"""
        to_delete = []
        
        for item in self.dup_checkboxes:
            # Check if this is from single folder mode (has keep_var)
            if 'keep_var' in item:
                # Delete if the keep_var is set to the OTHER photo
                if item['keep_var'].get() == item['keep_value']:
                    to_delete.append(item['path'])
            else:
                # Compare mode - use checkbox directly
                if item['var'].get():
                    to_delete.append(item['path'])
        
        if not to_delete:
            messagebox.showinfo("No Selection", "No photos selected for deletion.")
            return
        
        # Confirmation dialog
        result = messagebox.askyesno(
            "Confirm Deletion",
            f"⚠️ WARNING ⚠️\n\n"
            f"You are about to permanently delete {len(to_delete)} photo(s).\n\n"
            f"This action CANNOT be undone!\n\n"
            f"Are you sure you want to continue?",
            icon='warning'
        )
        
        if not result:
            return
        
        # Second confirmation
        result2 = messagebox.askyesno(
            "Final Confirmation",
            f"This is your final warning.\n\n"
            f"Delete {len(to_delete)} duplicate photo(s) permanently?",
            icon='warning'
        )
        
        if not result2:
            return
        
        # Collect unique directories that contain deleted files
        affected_folders = set()
        for path in to_delete:
            folder = os.path.dirname(path)
            affected_folders.add(folder)
        
        # Delete files
        deleted = 0
        errors = 0
        
        for path in to_delete:
            try:
                os.remove(path)
                deleted += 1
            except Exception as e:
                errors += 1
                print(f"Error deleting {path}: {e}")
        
        messagebox.showinfo(
            "Deletion Complete",
            f"Deleted {deleted} photo(s) successfully.\n"
            f"Errors: {errors}\n\n"
            f"Database will be updated to remove deleted photos."
        )
        
        # Close the duplicate finder window
        self.dup_window.destroy()
        
        # Rescan affected folders if any files were deleted
        if deleted > 0:
            self.set_status(f"Updating database after deleting {deleted} duplicate(s)...")
            threading.Thread(target=self._rescan_after_deletion, 
                           args=(affected_folders,), daemon=True).start()
    
    def _rescan_after_deletion(self, affected_folders):
        """Rescan folders after deletion to update the database"""
        # Run cleanup to remove deleted photos from database
        deleted_entries = self.cleanup_orphan_thumbnails()
        
        # Update status with results
        self.set_status(f"Database updated: removed {deleted_entries} deleted photo(s) from database")
        
        # If current folder is affected, reload thumbnails
        if self.current_folder in affected_folders:
            self.root.after(0, lambda: self.load_thumbnails_from_db(self.current_folder))


if __name__ == "__main__":
    root = tk.Tk()
    app = PhotoOrganizer(root)
    root.mainloop()
