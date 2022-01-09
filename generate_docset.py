from bs4 import BeautifulSoup
from tqdm import trange, tqdm
import json5

from typing import Any, Iterable
from pathlib import Path
import json
import re
import requests
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse


# Make sure to keep these updated for new versions of Dyalog. Both of these are
# used to patch and run the hlp.js to get better symbol help.
CURRENT_VERSION = "18.0"
HLP_JS_URL = "https://raw.githubusercontent.com/Dyalog/ride/2882e1441c39657a84ab4e6ba3aa932b0b719f33/src/hlp.js"

BASE_URL = "https://help.dyalog.com/latest"
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
    "GUI/Examples": "Guide",
    "anguage/Error Trapping": "Guide",
    # Sections.
    "MiscPages": "Section",
    "GUI/Miscellaneous": "Section",
    "GUI/SummaryTables": "Section",
    # Objects.
    "GUI/Objects": "Object",
    # These are all sub-pages of various objects.
    "GUI/ChildLists": "Object",
    "GUI/EventLists": "Object",
    "GUI/MethodLists": "Object",
    "GUI/MethodOrEventApplies": "Object",
    "GUI/ParentLists": "Object",
    "GUI/PropLists": "Object",
    "GUI/PropertyApplies": "Object",
    # Other.
    "GUI/Properties": "Property",
    "Language/Control Structures": "Statement",
    "Language/Errors": "Error",
    "Language/Primitive Operators": "Operator",
    "Language/System Commands": "Command",
    # This is basically only for the RIDE help.
    "Language/Symbols": "Notation",
    # TODO: This is actually a useless page.
    "index.html": "Guide",
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


def download_paths(paths: Iterable[str], desc: str) -> None:
    """
    Download all listed urls into the pages directory, skipping any already
    downloaded and showing progess.
    """
    for page in tqdm(list(paths), desc=desc):
        destination = TMP_DIR / Path(page).relative_to("/")
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
    # This makes it possible to use APL symbols etc in Dash.
    # We could scrape [1] but it's easier to just use the RIDE list directly.
    # [1]: https://help.dyalog.com/18.0/index.htm#Language/Introduction/Language%20Elements.htm

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


def scrape_help_toc() -> set[str]:
    """
    Get the pages exposed in the help.dyalog.com Table of Contents.
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
    pages = {k for x in chunks for k in x.keys()}
    pages.remove("___")  # This lists topics with no pages.
    return pages


def reconstruct_url(html_file: Path, rel_url: str) -> str:
    """
    Given a html file from the pages directory and a relative URL, figure out
    the absolute URL.
    """
    # We get the .parent to get the url directory. We resolve it to to remove '..'.
    # We also need to resolve tmp to make relalive_to work.
    return "/" + str(
        (html_file.parent / rel_url).resolve().relative_to(TMP_DIR.resolve())
    )


def get_entry_type(path: Path | str, title: str) -> str:
    """
    Get the Dash entry type given a path, handling a few special cases.
    """
    path = str(path)
    if "Content/GUI/MethodOrEvents" in path:
        return "Event" if " Event" in title else "Method"
    if "UserGuide/Installation and Configuration/Configuration Parameters" in path:
        return "Setting"
    # Crashes if no entry type is found.
    return next(v for k, v in ENTRY_TYPES.items() if k in path)


def sanitize_html(soup: BeautifulSoup) -> None:
    """
    Process the html to make it ready for Dash.
    """
    # Remove the "Open topic with navigation" link and breadcrumbs.
    for cls in ["MCWebHelpFramesetLinkTop", "breadcrumbs"]:
        for el in soup(class_=cls):
            el.extract()

    # Remove all script tags.
    del soup.body["onload"]
    for script in soup("script"):
        script.extract()

    # Patch all relative links to point to new .html pages (instead of .htm).
    for link in soup("a", href=has_relative_href):
        link["href"] = link["href"].replace(".htm", ".html")

    # Add Dash anchors.
    for section in soup("h4"):
        if section.string:
            # Lots of heading end with a colon like "Examples:", looks bad.
            # We use safe="" to make sure a slash can't appear in the name.
            name = urllib.parse.quote(str(section.string).removesuffix(":"), safe="")
            anchor = f"<a name='//apple_ref/cpp/Section/{name}' class='dashAnchor'></a>"
            section.insert_before(BeautifulSoup(anchor, "html.parser"))


def has_relative_href(href: str) -> bool:
    return (
        href
        and not urllib.parse.urlparse(href).netloc
        and not href.startswith("javascript:")
    )


class DocSet:
    assets: set[str]
    linked_pages: set[str]
    connection: sqlite3.Connection

    def __init__(self, pages: Iterable[str]) -> None:
        self.assets = set(pages)
        self.linked_pages = set()
        self.connection = sqlite3.connect(RESOURCES_DIR / "docSet.dsidx")
        self.connection.execute("DROP TABLE IF EXISTS searchIndex;")
        self.connection.execute(
            "CREATE TABLE searchIndex(id INTEGER PRIMARY KEY, name TEXT, type TEXT, path TEXT);"
        )
        self.connection.execute(
            "CREATE UNIQUE INDEX anchor ON searchIndex(name, type, path);"
        )

    def add_index(self, title: str, path: Path | str):
        # Make sure the path is html, absolute (to Documents/), and exists.
        path = Path("/", path).with_suffix(".html")
        assert (DOCUMENTS_DIR / path.relative_to("/")).exists()
        self.connection.execute(
            "INSERT OR IGNORE INTO searchIndex(name, type, path) VALUES (?, ?, ?)",
            (title, get_entry_type(path, title), str(path)),
        )

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()

    def process_pages(self, pages: Iterable[str]) -> None:
        for page in tqdm(list(pages), desc="Processing pages"):
            path = TMP_DIR / Path(page).relative_to("/")
            if path.is_dir():
                continue  # These sneak through.
            with open(path, "r") as fd:
                soup = BeautifulSoup(fd, "html.parser")

            # Extract links.
            for link in soup("a", href=has_relative_href):
                # TODO: This and reconstruct_url probably can be cleaner
                link_dest = reconstruct_url(path, link["href"]).split("#")[0]
                self.linked_pages.add(str(link_dest))

            # Extract other assets.
            for link in soup("link", rel="stylesheet"):
                self.assets.add(reconstruct_url(path, link["href"]))
            for img in soup("img"):
                self.assets.add(reconstruct_url(path, img["src"]))

            # Sanitize and save our changes. Use .html instead of .htm because
            # otherwise Dash will not recognise title tags correctly.
            sanitize_html(soup)
            destination = DOCUMENTS_DIR / path.relative_to(TMP_DIR)
            destination.parent.mkdir(exist_ok=True, parents=True)
            destination.with_suffix(".html").write_text(str(soup))
            self.add_index(soup.title.string, destination.relative_to(DOCUMENTS_DIR))


def main() -> None:
    if TMP_DIR.exists():
        print(
            "Note the tmp/ directory already exists. "
            "The docset might contain stale entries. "
            "Remove it if a clean docset is required.",
            file=sys.stderr,
        )

    TMP_DIR.mkdir(exist_ok=True)
    DOCUMENTS_DIR.mkdir(exist_ok=True, parents=True)
    shutil.copyfile("res/Info.plist", DOCSET_DIR / "Contents" / "Info.plist")
    shutil.copyfile("res/icon.png", DOCSET_DIR / "icon.png")

    # Scrape the initial bunch of pages.
    ride_help = scrape_ride_help()
    docset = DocSet(scrape_help_toc() | set(ride_help.values()))
    for title, path in ride_help.items():
        docset.add_index(title, path)

    # Keep downloading pages until there is nothing new in linked_pages.
    processed_pages = set()
    while missing_pages := docset.linked_pages - processed_pages:
        download_paths(missing_pages, "Downloading pages")
        docset.process_pages(missing_pages)
        processed_pages |= missing_pages

    # Download missing assets.
    download_paths(docset.assets, "Downloading assets")
    for path in docset.assets:
        rel_path = Path(path).relative_to("/")
        (DOCUMENTS_DIR / rel_path).parent.mkdir(exist_ok=True, parents=True)
        shutil.copyfile(TMP_DIR / rel_path, DOCUMENTS_DIR / rel_path)

    # Important, closes commits the index database.
    docset.close()


if __name__ == "__main__":
    main()
