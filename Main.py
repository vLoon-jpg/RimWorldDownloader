import os
import threading
import queue
import subprocess
import requests
import re
import shutil
import time
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup

# --- Configuration ---
APP_ID = "294100"  # RimWorld
STEAMCMD_PATH = r"C:\SteamCMD\steamcmd.exe"
WORKSHOP_PATH = r"C:\SteamCMD\steamapps\workshop\content\294100"
MODS_PATH = r"C:\GOG Games\RimWorld\Mods"
MAX_WORKERS = 5

# --- Globals ---
download_queue = queue.Queue()
stop_event = threading.Event()
active_downloads = {}
console_lock = threading.Lock()


def print_status(message=None):
    """Thread-safe status printing."""
    with console_lock:
        if message:
            print(f"\n>> {message}")

        active_list = [f"{name} ({wid})" for wid, name in active_downloads.items()]
        # Only show the status line if there are active downloads or items in queue
        if active_list or not download_queue.empty():
            print(f"   [STATUS] Active ({len(active_list)}): {', '.join(active_list)}")
            print(f"   [STATUS] In Queue: {download_queue.qsize()}")


def get_real_mod_name(workshop_id):
    """Fetches the actual title of the mod from Steam Workshop."""
    url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            title_div = soup.find("div", class_="workshopItemTitle")
            if title_div:
                return title_div.get_text().strip()
    except:
        pass
    return "UnknownMod"


def extract_id_from_url(url):
    match = re.search(r"id=(\d+)", url)
    if match: return match.group(1)
    return None


def scrape_ids_from_page(url):
    ids_found = set()
    if url.isdigit(): return [url]

    print(f"[SCRAPER] Fetching page info...")
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200: return []

        html = response.text
        soup = BeautifulSoup(html, "html.parser")

        # Collection items
        for item in soup.find_all("div", class_="collectionItem"):
            a_tag = item.find("a", href=True)
            if a_tag:
                found = extract_id_from_url(a_tag['href'])
                if found: ids_found.add(found)

        # General links
        for link in soup.find_all("a", href=True):
            href = link['href']
            if "filedetails" in href and "id=" in href:
                found = extract_id_from_url(href)
                if found: ids_found.add(found)

        # Self
        if not ids_found:
            self_id = extract_id_from_url(url)
            if self_id: ids_found.add(self_id)

    except Exception as e:
        print(f"[ERROR] Scraper failed: {e}")

    return list(ids_found)


def get_installed_mod_name(workshop_id):
    """
    Checks if mod is installed and returns its name from the folder name.
    Returns None if not installed.
    """
    if not os.path.isdir(MODS_PATH): return None

    for folder in os.listdir(MODS_PATH):
        # Folder format is typically ID_Name
        if folder.startswith(workshop_id + "_"):
            # Split on the first underscore to get the name part
            parts = folder.split('_', 1)
            if len(parts) > 1:
                return parts[1]  # Return the cached name (e.g. "Harmony")
            return "UnknownMod"

    return None


def run_steamcmd(workshop_id):
    cmd = [
        STEAMCMD_PATH,
        "+login", "anonymous",
        "+workshop_download_item", APP_ID, workshop_id,
        "+quit"
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def validate_download(src):
    """Checks if the download actually resulted in files."""
    if not os.path.isdir(src): return False
    if not os.listdir(src): return False
    return True


def install_mod(workshop_id, fallback_name="UnknownMod"):
    src = os.path.join(WORKSHOP_PATH, workshop_id)

    mod_name = fallback_name
    about = os.path.join(src, "About", "About.xml")

    if os.path.isfile(about):
        try:
            tree = ET.parse(about)
            root = tree.getroot()
            xml_name = root.findtext("name")
            if xml_name:
                mod_name = xml_name
        except Exception:
            pass

    mod_name = "".join(c for c in mod_name if c.isalnum() or c in " _-")
    folder_name = f"{workshop_id}_{mod_name}"
    dst = os.path.join(MODS_PATH, folder_name)

    if os.path.exists(dst):
        shutil.rmtree(dst)

    try:
        shutil.copytree(src, dst)
        return True, mod_name
    except Exception as e:
        print(f"Copy Error: {e}")
        return False, mod_name


def worker():
    while not stop_event.is_set():
        try:
            workshop_id = download_queue.get(timeout=1)
        except queue.Empty:
            continue

        # --- CHECK IF INSTALLED ---
        installed_name = get_installed_mod_name(workshop_id)
        if installed_name:
            print_status(f"The mod {installed_name} is already installed!")
            download_queue.task_done()
            continue
        # ---------------------------

        web_name = get_real_mod_name(workshop_id)

        active_downloads[workshop_id] = web_name
        print_status(f"Downloading {web_name}...")

        success = False
        final_name = web_name

        for attempt in range(2):
            run_steamcmd(workshop_id)

            src = os.path.join(WORKSHOP_PATH, workshop_id)
            if validate_download(src):
                installed, final_name = install_mod(workshop_id, fallback_name=web_name)
                if installed:
                    success = True
                    break

            if attempt == 0:
                print_status(f"Retry ({attempt + 1}/2) for {web_name}...")

        if workshop_id in active_downloads:
            del active_downloads[workshop_id]

        if success:
            print_status(f"INSTALLED: {final_name}")
        else:
            print_status(f"FAILED (Corrupt): {web_name}")

        download_queue.task_done()


def start_workers():
    current_threads = len([t for t in threading.enumerate() if t.daemon])
    if current_threads < MAX_WORKERS:
        for _ in range(MAX_WORKERS - current_threads):
            t = threading.Thread(target=worker, daemon=True)
            t.start()


def main():
    if not os.path.exists(STEAMCMD_PATH):
        print(f"Error: SteamCMD not found at {STEAMCMD_PATH}")
        return

    os.makedirs(MODS_PATH, exist_ok=True)
    start_workers()

    print("=====================================================")
    print(f"RimWorld Smart Downloader (Threads: {MAX_WORKERS})")
    print("=====================================================")

    while True:
        url = input("\nPASTE URL > ").strip()

        if url.lower() in ["exit", "stop"]:
            stop_event.set()
            break

        if not url: continue

        found_ids = scrape_ids_from_page(url)

        if found_ids:
            print(f"[INFO] Found {len(found_ids)} mods. Queueing...")
            for wid in found_ids:
                download_queue.put(wid)
        else:
            print("[INFO] No mod IDs found.")


if __name__ == "__main__":
    main()