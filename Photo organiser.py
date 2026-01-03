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

        self.setup_ui()
        
        # 2. Check/Create database
        if not self.check_and_setup_db():
            return  # Exit if user chose to quit
        
        # 3. Load the photo root from database (after migration is complete)
        stored_root = self.load_setting("photo_root_path")
        if stored_root and os.path.isdir(stored_root):
            self.photo_root = stored_root
            self.current_folder = stored_root
        
        # 4. Automatic startup from the photo root directory
        threading.Thread(target=self.cleanup_orphan_thumbnails, daemon=True).start()
        self.populate_tree(start_folder=self.photo_root)
        threading.Thread(target=self.scan_and_store_thumbnails, args=(self.photo_root,), daemon=True).start()

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
        ttk.Button(bottom_frame, text="Rescan Current Folder", command=self.rescan_current_folder).pack(side=tk.LEFT, padx=5, pady=5)

        self.status_frame = ttk.Frame(self.root)
        self.status_frame.pack(fill=tk.X, side=tk.BOTTOM)
        self.status_scroll = ttk.Scrollbar(self.status_frame, orient=tk.HORIZONTAL)
        self.status_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_bar = tk.Text(self.status_frame, height=1, wrap='none', xscrollcommand=self.status_scroll.set)
        self.status_bar.pack(fill=tk.X, side=tk.TOP)
        self.status_scroll.config(command=self.status_bar.xview)
        self.status_bar.configure(state='disabled')

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
                label TEXT
            )
        """)
        cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_photos_folder ON photos(folder)")
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

    def rescan_current_folder(self):
        # Rescan only the currently selected folder and its subfolders
        if self.current_folder and os.path.isdir(self.current_folder):
            self.set_status(f"Starting rescan of {self.current_folder}...")
            threading.Thread(target=self.scan_and_store_thumbnails_with_stats, args=(self.current_folder,), daemon=True).start()
        else:
            messagebox.showwarning("No Folder Selected", "Please select a folder in the tree view to rescan.")


    def scan_and_store_thumbnails_with_stats(self, folder):
        """Scan and store thumbnails with summary statistics at the end"""
        image_extensions = self.get_supported_image_extensions()

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        total = 0
        for _root, _dirs, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(image_extensions):
                    total += 1

        new_files = 0
        orientations_corrected = 0
        errors = 0
        
        for root_dir, dirs, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(image_extensions):
                    full_path = os.path.join(root_dir, f)

                    cur.execute("SELECT path FROM photos WHERE path=?", (full_path,))
                    if cur.fetchone() is None:
                        try:
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
                                "INSERT OR REPLACE INTO photos (path, folder, thumbnail) VALUES (?, ?, ?)",
                                (full_path, root_dir, thumb_blob)
                            )
                            new_files += 1
                        except Exception as e:
                            errors += 1
                            print(f"Error processing {full_path}: {e}")

        conn.commit()

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

    def scan_and_store_thumbnails(self, folder):
        self.set_status(f"Scanning and storing thumbnails: {folder}...")
        
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
                                "INSERT OR REPLACE INTO photos (path, folder, thumbnail) VALUES (?, ?, ?)",
                                (full_path, root_dir, thumb_blob)
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


if __name__ == "__main__":
    root = tk.Tk()
    app = PhotoOrganizer(root)
    root.mainloop()
