from bs4 import BeautifulSoup
from pathlib import Path
from tqdm import trange, tqdm
from typing import Any, Iterable
import json
import json5
import re
import requests
import shutil
import sqlite3
import subprocess
import urllib.parse


CURRENT_VERSION = "18.0"
BASE_URL = "https://help.dyalog.com/latest"
HLP_JS_URL = "https://raw.githubusercontent.com/Dyalog/ride/master/src/hlp.js"
DOCSET_DIR = Path("Dyalog APL.docset")
RESOURCES_DIR = DOCSET_DIR / "Contents" / "Resources"
DOCUMENTS_DIR = RESOURCES_DIR / "Documents"
TMP_DIR = Path("tmp")

ENTRY_TYPES = {
    # Functions.
    "Language/I Beam Functions": "Function",
    "Language/Primitive Functions": "Function",
    "Language/System Functions": "Function",
    # Guides.
    "DotNet": "Guide",
    "InterfaceGuide": "Guide",
    "Language/APL Component Files": "Guide",
    "Language/Appendices/PCRE": "Guide",
    "Language/Defined Functions and Operators": "Guide",
    "Language/Introduction": "Guide",
    "Language/Object Oriented Programming": "Guide",
    "RelNotes": "Guide",
    "UNIX_IUG": "Guide",
    "UserGuide": "Guide",
    "anguage/Error Trapping": "Guide",
    # Sections.
    "MiscPages": "Section",
    "GUI/SummaryTables": "Section",
    # Other.
    "GUI/Objects": "Object",
    "GUI/Properties": "Property",
    "Language/Control Structures": "Statement",
    "Language/Errors": "Error",
    "Language/Primitive Operators": "Operator",
    "Language/Symbols": "Notation",  # This is basically the RIDE help stuff.
    "Language/System Commands": "Command",
}


def download_jsonp(path: str) -> Any:
    """
    Download and parse a jsonp file.
    """
    url = f"{BASE_URL}{path}"
    r = requests.get(url)
    r.raise_for_status()
    jsonp = re.search(r"define\((.*)\)", r.text)[1]
    return json5.loads(jsonp)


def download_urls(urls: Iterable[str], *, desc: str, dest_dir: Path | str) -> None:
    """
    Download all listed urls into the pages directory, skipping any already
    downloaded and showing progess.
    """
    for page in tqdm(list(urls), desc=desc):
        destination = Path(dest_dir) / Path(page).relative_to("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            continue
        url = f"{BASE_URL}{page}"
        r = requests.get(url)
        with open(destination, "wb") as fd:
            for chunk in r.iter_content(chunk_size=128):
                fd.write(chunk)


def scrape_ride_help() -> dict[str, str]:
    """
    Get the symbols used for RIDE F1-help functionality in the most hacky way
    you can imagine.
    """
    path = TMP_DIR / "hlp.js"
    if not path.exists():
        r = requests.get(HLP_JS_URL)
        r.raise_for_status()
        patched = (
            "D={aboutDetails: () => ''}\n"
            + r.text
            + f"D.InitHelp('{CURRENT_VERSION}')\n;console.log(JSON.stringify(D.hlp));"
        )
        path.write_text(patched)
    raw_help = json.loads(subprocess.check_output(["node", str(path)]))
    # Filter out all the stuff that doesn't lead to the docs.
    r = {}
    for title, url in raw_help.items():
        if "#" not in url:
            continue
        r[title] = "/Content/" + url.split("#")[1]
    return r


def scrape_entries() -> None:
    """
    Blindly download all html pages we need.
    """
    # Download Table of Contents tree.
    if not (TMP_DIR / "toc.json").exists():
        toc = download_jsonp("/Data/Tocs/Dyalog.js")
        with open(TMP_DIR / "toc.json", "w") as fd:
            json.dump(toc, fd)

    # Download Table of Contents chunks.
    if not (TMP_DIR / "chunks.json").exists():
        chunks = [
            download_jsonp(f"/Data/Tocs/{toc['prefix']}{i}.js")
            for i in trange(7, desc="Downloading chunks")
        ]
        with open(TMP_DIR / "chunks.json", "w") as fd:
            json.dump(chunks, fd)

    # Extract pages to download.
    with open(TMP_DIR / "chunks.json") as fd:
        chunks = json.load(fd)
    chunks = json.loads((TMP_DIR / "chunks.json").read_text())
    pages = [k for x in chunks for k in x.keys()]
    pages.remove("___")  # This lists topics with no pages.

    # Note RIDE F1-help pages are not exposed in the tree. We could scrape
    # https://help.dyalog.com/18.0/index.htm#Language/Introduction/Language%20Elements.htm
    # but it's easier to just use the RIDE list directly.
    pages.extend(scrape_ride_help().values())

    # Download every page.
    download_urls(pages, desc="Downloading topics", dest_dir=TMP_DIR / "pages")


def reconstruct_url(html_file: Path, rel_url: str) -> str:
    """
    Given a html file from the pages directory and a relative URL, figure out
    the absolute URL.
    """
    # We get the .parent to get the url directory, to remove .. from this we
    # resolve it. To use relative_to we need to resolve the pages directory too.
    return "/" + str(
        (html_file.parent / rel_url)
        .resolve()
        .relative_to((TMP_DIR / "pages/").resolve())
    )


def copy_sanitize_entries() -> None:
    """
    Sanitize the downloaded html pages, copy them into the docset, and download
    any remaining assets.
    """
    images = set()
    stylesheets = set()
    for html in tqdm(list(TMP_DIR.rglob("*.htm")), desc="Sanitizing html"):
        with open(html, "r") as fd:
            soup = BeautifulSoup(fd, "html.parser")

        # Remove the "Open topic with navigation" link and breadcrumbs.
        for cls in ["MCWebHelpFramesetLinkTop", "breadcrumbs"]:
            for el in soup(class_=cls):
                el.extract()

        # Remove all script tags.
        del soup.body["onload"]
        for script in soup("script"):
            script.extract()

        # Path all the links to point to new .html pages (instead of .htm).
        for link in soup("a"):
            if link.has_attr("href"):
                link["href"] = link["href"].replace(".htm", ".html")

        # Add Dash anchors.
        for section in soup("h4"):
            if section.string:
                # Lots of heading end with a colon like "Examples:", looks bad.
                name = urllib.parse.quote(str(section.string).removesuffix(":"))
                anchor = (
                    f"<a name='//apple_ref/cpp/Section/{name}' class='dashAnchor'></a>"
                )
                section.insert_before(BeautifulSoup(anchor, "html.parser"))

        # Extract stylesheets.
        for link in soup("link"):
            assert link["rel"][0] == "stylesheet"
            stylesheets.add(reconstruct_url(html, link["href"]))

        # Extract images.
        for img in soup("img"):
            images.add(reconstruct_url(html, img["src"]))

        # Save our changes. Use .html instead of .htm because otherwise Dash
        # will not recognise title tags correctly.
        destination = DOCUMENTS_DIR / html.relative_to(TMP_DIR / "pages")
        destination.parent.mkdir(exist_ok=True, parents=True)
        destination.with_suffix(".html").write_text(str(soup))

    # Download to the data dir then just copy it over. Let's use delete the docset.
    download_urls(
        images | stylesheets, desc="Downloading assets", dest_dir=TMP_DIR / "pages"
    )
    for path in images | stylesheets:
        rel_path = Path(path).relative_to("/")
        (DOCUMENTS_DIR / rel_path).parent.mkdir(exist_ok=True, parents=True)
        shutil.copyfile(TMP_DIR / "pages" / rel_path, DOCUMENTS_DIR / rel_path)


def get_entry_type(path: Path | str, title: str) -> str:
    """
    Get the Dash entry type given a path, handling a few special cases.
    """
    path = str(path)
    if "Content/GUI/MethodOrEvents" in path:
        return "Event" if " Event" in title else "Method"
    if "UserGuide/Installation and Configuration/Configuration Parameters" in path:
        return "Setting"
    return next(v for k, v in ENTRY_TYPES.items() if k in path)


def generate_docset_index() -> None:
    """
    Generate the docset index database.
    """
    conn = sqlite3.connect(RESOURCES_DIR / "docSet.dsidx")
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS searchIndex;")
    cur.execute(
        "CREATE TABLE searchIndex(id INTEGER PRIMARY KEY, name TEXT, type TEXT, path TEXT);"
    )
    cur.execute("CREATE UNIQUE INDEX anchor ON searchIndex (name, type, path);")

    # Add the RIDE help. This makes it possible to use APL symbols etc in Dash.
    ride_help = scrape_ride_help()
    for title, path in ride_help.items():
        # Note all the .htm files are not .html. Account for that.
        path = path.replace(".htm", ".html")
        cur.execute(
            "INSERT OR IGNORE INTO searchIndex(name, type, path) VALUES (?, ?, ?)",
            (title, "Notation", path),
        )

    # Add every entry into the search index.
    for path in tqdm(list(DOCUMENTS_DIR.rglob("*.html")), desc="Creating the index"):
        # I could do this in copy_sanitize_entries but this way the SQL stuff
        # stays separate.
        title = re.search(r"<title>([^<]*)</title>", path.read_text())[1]
        cur.execute(
            "INSERT OR IGNORE INTO searchIndex(name, type, path) VALUES (?, ?, ?)",
            (title, get_entry_type(path, title), str(path.relative_to(DOCUMENTS_DIR))),
        )

    # Important.
    conn.commit()
    conn.close()


if __name__ == "__main__":
    if TMP_DIR.exists():
        print(
            "Note the tmp/ directory already exists. "
            "The docset might contain stale entries. "
            "Remove it if a clean docset is required."
        )
    DOCUMENTS_DIR.mkdir(exist_ok=True, parents=True)
    TMP_DIR.mkdir(exist_ok=True)
    # This is split into phases to limit spamming the help.dyalog.com servers.
    scrape_entries()
    copy_sanitize_entries()
    generate_docset_index()
    shutil.copyfile("res/Info.plist", DOCSET_DIR / "Contents" / "Info.plist")
    shutil.copyfile("res/icon.png", DOCSET_DIR / "icon.png")
